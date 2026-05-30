"""
local_compat_server.py
Servidor local (Windows) para buscar compatibilidade OEM no Mercado Livre via Playwright.
Roda em localhost:5001. Execute: python local_compat_server.py

Fluxo:
  Frontend → Railway (checa Supabase) → sem dados → chama localhost:5001
  localhost:5001 → Playwright → ML → IA extrai compatibilidade → salva Supabase → retorna
"""

import json, re, os, time, traceback
import requests
from flask import Flask, request, jsonify, Response
from playwright.sync_api import sync_playwright

app = Flask(__name__)

# ─── Configuração ─────────────────────────────────────────────────────────────

# CRM Supabase — tabela oem_compatibilidades com RLS pública (sem auth necessária)
_CRM_HOST = "uthsiihzpsgarargegcw.supabase.co"
_CRM_ANON = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InV0aHNpaWh6cHNnYXJhcmdlZ2N3Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzkxMTAyMzcsImV4cCI6MjA5NDY4NjIzN30.vLsk56k7VUROClBmyg4NJFXHtpTGmr1f0Xl_dARbtZE"

_GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
_CLAUDE_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
print(f"[DEBUG] Claude key carregada: {bool(_CLAUDE_KEY)}")
print(f"[DEBUG] Tamanho chave Claude: {len(_CLAUDE_KEY)}")


def _crm_headers():
    return {
        "apikey": _CRM_ANON,
        "Authorization": f"Bearer {_CRM_ANON}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }


# ─── Playwright: raspar ML ────────────────────────────────────────────────────

def _raspar_ml(oem: str) -> list:
    """Usa Playwright (Windows local) para raspar títulos do ML."""
    oem_enc = requests.utils.quote(oem)
    url = f"https://lista.mercadolivre.com.br/acessorios-veiculos/{oem_enc}"
    print(f"[ML] OEM pesquisado: {oem}")
    print(f"[ML] URL: {url}")

    anuncios = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"]
        )
        ctx = browser.new_context(
            locale="pt-BR",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            extra_http_headers={"Accept-Language": "pt-BR,pt;q=0.9"}
        )
        page = ctx.new_page()
        page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")

        # Abre ML primeiro para cookies
        page.goto("https://www.mercadolivre.com.br/", timeout=15000, wait_until="domcontentloaded")
        page.wait_for_timeout(800)

        resp = page.goto(url, timeout=30000, wait_until="domcontentloaded")
        print(f"[ML] Status HTTP: {resp.status if resp else 'N/A'}")
        try:
            page.wait_for_selector(
                "li.ui-search-layout__item, .ui-search-result__wrapper, .poly-card",
                timeout=12000
            )
        except Exception:
            pass
        page.wait_for_timeout(2000)
        html = page.content()
        print(f"[ML] HTML recebido: {len(html)} caracteres")

        anuncios = page.evaluate("""() => {
            const res = [];
            const items = document.querySelectorAll(
                'li.ui-search-layout__item, .ui-search-result__wrapper, .poly-card'
            );
            for (let i = 0; i < Math.min(items.length, 30); i++) {
                const t = items[i].querySelector(
                    '.ui-search-item__title, .poly-component__title, h2'
                );
                const a = items[i].querySelector('a[href*="mercadolivre"]');
                const p = items[i].querySelector('.andes-money-amount__fraction');
                const titulo = t ? t.textContent.trim() : '';
                if (titulo.length > 8)
                    res.push({
                        titulo,
                        link: a ? a.href.split('?')[0] : '',
                        preco: p ? p.textContent.trim() : ''
                    });
            }
            return res;
        }""")

        browser.close()

    print(f"[ML] Anúncios encontrados: {len(anuncios)}")
    for i, a in enumerate(anuncios[:30], 1):
        print(f"[TITULO {i}] {a['titulo']}")
    return anuncios


# ─── IA: extrair compatibilidade dos títulos ─────────────────────────────────

_MARCAS_RE = [
    r"citroen|citro[eë]n", r"renault", r"peugeot", r"toyota", r"honda",
    r"hyundai", r"kia", r"nissan", r"mitsubishi", r"fiat", r"volkswagen|vw\b",
    r"chevrolet|gm\b", r"ford", r"jeep", r"ram\b", r"dodge",
    r"bmw", r"mercedes|mb\b", r"audi", r"volvo", r"suzuki",
    r"chery", r"byd", r"jac", r"caoa\s*chery"
]

_MARCAS_NORM = {
    "citroen": "Citroen", "citroën": "Citroen",
    "renault": "Renault", "peugeot": "Peugeot",
    "toyota": "Toyota", "honda": "Honda",
    "hyundai": "Hyundai", "kia": "Kia",
    "nissan": "Nissan", "mitsubishi": "Mitsubishi",
    "fiat": "Fiat", "volkswagen": "VW", "vw": "VW",
    "chevrolet": "Chevrolet", "gm": "Chevrolet",
    "ford": "Ford", "jeep": "Jeep", "ram": "Ram", "dodge": "Dodge",
    "bmw": "BMW", "mercedes": "Mercedes", "mb": "Mercedes",
    "audi": "Audi", "volvo": "Volvo", "suzuki": "Suzuki",
    "chery": "Chery", "byd": "BYD", "jac": "JAC", "caoa chery": "Chery"
}

def _extrair_regex_titulos(anuncios: list) -> list:
    """Extração por regex dos títulos — não depende de IA."""
    resultado = []
    for a in anuncios:
        titulo = a.get("titulo", "")
        titulo_lower = titulo.lower()

        # Detecta marca
        marca_raw = None
        for pat in _MARCAS_RE:
            m = re.search(pat, titulo_lower, re.IGNORECASE)
            if m:
                marca_raw = m.group(0).lower().strip()
                break
        if not marca_raw:
            continue

        marca = _MARCAS_NORM.get(marca_raw, marca_raw.title())

        # Extrai modelo: palavra(s) maiúscula(s) após a marca
        modelo = None
        m_modelo = re.search(
            r'\b' + re.escape(marca_raw) + r'\b\s+([A-Z][A-Za-z0-9\-]+(?:\s+[A-Z][A-Za-z0-9\-]+)?)',
            titulo, re.IGNORECASE
        )
        if m_modelo:
            modelo = m_modelo.group(1).strip()

        # Extrai anos — formatos: "2022 A 2025", "2022-2025", "2022/2025", "2022 2025"
        anos = re.findall(r'\b(20\d{2})\b', titulo)
        ano_inicial = int(min(anos)) if anos else None
        ano_final = int(max(anos)) if anos else None

        # Extrai motor: 1.0, 1.6T, 2.0 etc.
        m_motor = re.search(r'\b(\d+[.,]\d\s*(?:T|Turbo)?)\b', titulo, re.IGNORECASE)
        motor = m_motor.group(1).replace(",", ".").strip() if m_motor else None

        resultado.append({
            "titulo_original": titulo,
            "marca": marca,
            "modelo": modelo,
            "motor": motor,
            "cambio": None,
            "ano_inicial": ano_inicial,
            "ano_final": ano_final,
        })

    print(f"[REGEX] {len(resultado)} compatibilidades extraídas de {len(anuncios)} títulos")
    return resultado


def _extrair_compat_ia(oem: str, anuncios: list) -> list:
    """Usa IA para extrair marca/modelo/motor/anos dos títulos — retorna lista de compatibilidades."""
    print(f"[IA] _extrair_compat_ia chamada com {len(anuncios)} anúncios")
    if not anuncios:
        return []

    def _build_prompt(lote, offset):
        titulos_txt = "\n".join([f"{offset+i+1}. {a['titulo']}" for i, a in enumerate(lote)])
        return f"""Esses são títulos de anúncios do Mercado Livre Brasil encontrados pesquisando pelo código OEM: {oem}

Títulos:
{titulos_txt}

Os resultados são de uma busca por código de peça automotiva. Extraia de cada título informações de compatibilidade veicular:
- marca: marca do veículo compatível (Renault, Nissan, Toyota, Honda, Fiat, VW, Ford, Chevrolet, Hyundai, Kia, Mitsubishi, etc.) ou null
- modelo: modelo do veículo (Duster, Kicks, Corolla, Civic, Uno, Gol, Onix, HB20, etc.) ou null
- motor: cilindrada (1.0, 1.3, 1.6, 2.0, 1.5 Turbo, etc.) ou null
- cambio: Manual ou Automatico se mencionado, senão null
- ano_inicial: primeiro ano do range (4 dígitos) ou null
- ano_final: último ano do range (4 dígitos) ou null

Inclua todos os títulos que mencionam marca/modelo de veículo. Ignore os que não têm info de veículo.
NÃO invente dados. Se marca/modelo não está no título, coloque null.
RETORNE APENAS JSON array:
[{{"indice":{offset+1},"marca":"Toyota","modelo":"C-HR","motor":"2.0","cambio":null,"ano_inicial":2017,"ano_final":2023}}]"""

    def _gemini(p):
        for modelo in ["gemini-2.5-flash", "gemini-2.0-flash"]:
            try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent?key={_GEMINI_KEY}"
                r = requests.post(url, json={
                    "contents": [{"parts": [{"text": p}]}],
                    "generationConfig": {"maxOutputTokens": 8192, "temperature": 0.0}
                }, timeout=30)
                print(f"[IA] Gemini {modelo} status: {r.status_code}")
                if r.status_code == 200:
                    print("[IA] JSON COMPLETO GEMINI:")
                    print(r.text[:3000])
                    print(f"[IA] Finish reason: {r.json()['candidates'][0].get('finishReason')}")
                    parts = r.json()["candidates"][0]["content"]["parts"]
                    text = "".join(x["text"] for x in parts if "text" in x)

                    print(f"[IA] TEXTO GEMINI: {text[:1000]}")

                    m = re.search(r'\[.*\]', text, re.DOTALL)
                    if m:
                        return json.loads(m.group(0))
                else:
                    print(f"[IA] Gemini {modelo} resposta: {r.text[:200]}")
            except Exception as e:
                print(f"[IA] Gemini {modelo} erro: {e}")
                traceback.print_exc()

    def _claude(p):
        print("[CLAUDE] Entrou na função")
        if not _CLAUDE_KEY:
            print("[CLAUDE] motivo retorno None: _CLAUDE_KEY vazia")
            return None
        try:
            print("[CLAUDE] Enviando requisição")
            r = requests.post("https://api.anthropic.com/v1/messages",
                headers={"x-api-key": _CLAUDE_KEY, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": "claude-haiku-4-5-20251001", "max_tokens": 2000,
                      "messages": [{"role": "user", "content": p}]}, timeout=30)
            print(f"[CLAUDE] status: {r.status_code}")
            print(f"[CLAUDE] resposta: {r.text[:1000]}")
            print("[CLAUDE] JSON recebido")
            text = r.json()["content"][0]["text"].strip()
            m = re.search(r'\[.*\]', text, re.DOTALL)
            if not m:
                print("[CLAUDE] motivo retorno None: nenhum array JSON encontrado no texto")
                return None
            return json.loads(m.group(0))
        except Exception as e:
            print(f"[CLAUDE] motivo retorno None: exceção — {e}")
            print(f"[IA] Claude erro: {e}")
            traceback.print_exc()
            return None

    LOTE = 10
    ai_results = []
    for start in range(0, len(anuncios), LOTE):
        lote = anuncios[start:start + LOTE]
        lote_num = start // LOTE + 1
        print(f"[IA] Lote {lote_num}: anúncios {start+1}–{start+len(lote)}")
        prompt = _build_prompt(lote, start)
        print("[IA] Tentando Claude primeiro...")
        resultado = _claude(prompt)
        print(f"[IA] Claude retornou: {type(resultado)} len={len(resultado) if resultado else 0}")
        if not resultado:
            print("[IA] Claude falhou, tentando Gemini...")
            resultado = _gemini(prompt)
        print(f"[IA] Resultado final lote {lote_num}: {type(resultado)} len={len(resultado) if resultado else 0}")
        if resultado:
            ai_results.extend(resultado)

    # remove duplicados por índice (mantém primeira ocorrência)
    vistos = set()
    ai_results = [x for x in ai_results if not (x.get("indice") in vistos or vistos.add(x.get("indice")))]
    print(f"[IA] Total acumulado após lotes: {len(ai_results)} itens")

    if not ai_results:
        print("[IA] Sem resultado da IA — usando regex como fallback")
        return _extrair_regex_titulos(anuncios)

    compatibilidades = []
    for i, a in enumerate(anuncios):
        ai = next((x for x in ai_results if x.get("indice") == i + 1), {})
        if not ai.get("marca"):
            continue
        compatibilidades.append({
            "titulo_original": a["titulo"],
            "marca": ai.get("marca", ""),
            "modelo": ai.get("modelo", ""),
            "motor": ai.get("motor", ""),
            "cambio": ai.get("cambio", ""),
            "ano_inicial": ai.get("ano_inicial"),
            "ano_final": ai.get("ano_final"),
        })

    print(f"[IA] {len(compatibilidades)} compatibilidades extraídas de {len(anuncios)} títulos")
    return compatibilidades


def _agrupar_compat(compatibilidades: list) -> list:
    """Agrupa por marca+modelo e expande range de anos."""
    grupos = {}
    for c in compatibilidades:
        chave = f"{(c.get('marca') or '').lower()}|{(c.get('modelo') or '').lower()}"
        if chave not in grupos:
            grupos[chave] = dict(c)
            grupos[chave]["count"] = 1
        else:
            g = grupos[chave]
            g["count"] += 1
            if c.get("ano_inicial") and (not g.get("ano_inicial") or c["ano_inicial"] < g["ano_inicial"]):
                g["ano_inicial"] = c["ano_inicial"]
            if c.get("ano_final") and (not g.get("ano_final") or c["ano_final"] > g["ano_final"]):
                g["ano_final"] = c["ano_final"]
            if not g.get("motor") and c.get("motor"):
                g["motor"] = c["motor"]

    return sorted(grupos.values(), key=lambda x: x["count"], reverse=True)


# ─── Filtro: descarta anúncios isolados ──────────────────────────────────────

def _filtrar_anuncios_isolados(anuncios: list) -> list:
    """Filtra anúncios fora da marca dominante quando ela representa >60% do total."""
    from collections import Counter

    def _detectar_marca(titulo):
        tl = titulo.lower()
        for pat in _MARCAS_RE:
            m = re.search(pat, tl, re.IGNORECASE)
            if m:
                return _MARCAS_NORM.get(m.group(0).lower().strip(), m.group(0).lower().strip())
        return None

    print(f"[ML] Antes filtro: {len(anuncios)} anúncios")

    marcas = [_detectar_marca(a["titulo"]) for a in anuncios]
    classificados = [m for m in marcas if m]
    contagem_marcas = Counter(classificados)

    if not classificados:
        print("[ML] Nenhuma marca detectada — sem filtro aplicado")
        return anuncios

    marca_dominante, count_dominante = contagem_marcas.most_common(1)[0]
    pct_dominante = count_dominante / len(classificados)

    print(f"[ML] Marca dominante: {marca_dominante} ({count_dominante}/{len(classificados)} anúncios classificados)")
    print(f"[ML] Percentual dominante: {pct_dominante:.0%}")

    _AGRICOLA = re.compile(r'\b(trator|massey|colheitadeira|implemento)\b', re.IGNORECASE)

    if pct_dominante > 0.60:
        por_marca = [(a, m) for a, m in zip(anuncios, marcas) if m is None or m == marca_dominante]
        removidos_marca = len(anuncios) - len(por_marca)
        print(f"[ML] Removidos por marca divergente: {removidos_marca}")

        anuncios_filtrados = [a for a, _ in por_marca if not _AGRICOLA.search(a["titulo"])]
        removidos_agricola = len(por_marca) - len(anuncios_filtrados)
        print(f"[ML] Removidos por categoria agrícola: {removidos_agricola}")
    else:
        # sem marca dominante clara — mantém quem aparece >= 2 vezes
        from collections import Counter as _C
        chaves = [f"{_detectar_marca(a['titulo'])}" for a in anuncios]
        cont = _C(k for k in chaves if k != "None")
        por_freq = [a for a, k in zip(anuncios, chaves) if k == "None" or cont[k] > 1]
        print(f"[ML] Removidos por marca divergente: 0 (sem dominante, filtro por frequência)")
        anuncios_filtrados = [a for a in por_freq if not _AGRICOLA.search(a["titulo"])]
        removidos_agricola = len(por_freq) - len(anuncios_filtrados)
        print(f"[ML] Removidos por categoria agrícola: {removidos_agricola}")

    print(f"[ML] Após filtro: {len(anuncios_filtrados)} anúncios")
    return anuncios_filtrados


# ─── Supabase: salvar compatibilidades ───────────────────────────────────────

def _salvar_supabase(oem: str, grupos: list, confianca: int) -> int:
    """Salva grupo de compatibilidades no Supabase. Retorna quantos salvos."""
    hdrs = _crm_headers()

    total = 0
    for g in grupos:
        marca = (g.get("marca") or "").strip()
        if not marca:
            continue
        payload = {
            "oem": oem,
            "marca": marca,
            "modelo": (g.get("modelo") or "").strip() or None,
            "motor": (g.get("motor") or "").strip() or None,
            "cambio": (g.get("cambio") or "").strip() or None,
            "ano_inicial": g.get("ano_inicial") or None,
            "ano_final": g.get("ano_final") or None,
            "fonte": "mercadolivre_playwright",
            "confianca": confianca,
            "data_validacao": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        }
        try:
            r = requests.post(
                f"https://{_CRM_HOST}/rest/v1/oem_compatibilidades",
                headers=hdrs, json=payload, timeout=10
            )
            if r.status_code in (200, 201):
                total += 1
            else:
                print(f"[Supabase] Erro {r.status_code}: {r.text[:200]}")
        except Exception as e:
            print(f"[Supabase] Exceção: {e}")

    print(f"[Supabase] {total} compatibilidades salvas para OEM {oem}")
    return total


# ─── Rota principal ───────────────────────────────────────────────────────────

def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


@app.after_request
def add_cors(resp):
    return _cors(resp)


@app.route("/buscar-compat", methods=["POST", "GET", "OPTIONS"])
def buscar_compat():
    if request.method == "OPTIONS":
        return Response("", status=204)

    data = request.get_json(force=True, silent=True) or {}
    oem = (data.get("oem") or request.args.get("oem", "")).strip().upper()
    if not oem or len(oem) < 4:
        return jsonify({"ok": False, "erro": "OEM inválido (mínimo 4 caracteres)"}), 400

    print(f"\n{'='*50}")
    print(f"[LOCAL] Buscando OEM: {oem}")
    t0 = time.time()

    try:
        # 1. Raspar ML com Playwright
        anuncios = _raspar_ml(oem)
        if not anuncios:
            return jsonify({
                "ok": True,
                "oem": oem,
                "fonte": "ml_local",
                "compatibilidades_confirmadas": [],
                "grau_de_confianca": 0,
                "mensagem": "Nenhum anúncio encontrado no ML para este OEM",
                "tempo_s": round(time.time() - t0, 1)
            })

        # 2. Filtra anúncios isolados (aparecem apenas 1 vez)
        anuncios = _filtrar_anuncios_isolados(anuncios)

        # 3. IA extrai compatibilidade dos títulos
        compat_raw = _extrair_compat_ia(oem, anuncios)
        print(f"[DEBUG] Compatibilidades IA: {len(compat_raw)}")
        for c in compat_raw:
            print(f"[DEBUG]   marca={c.get('marca')} modelo={c.get('modelo')} motor={c.get('motor')} anos={c.get('ano_inicial')}-{c.get('ano_final')}")

        grupos = _agrupar_compat(compat_raw)
        print(f"[DEBUG] Após agrupamento (marca|modelo): {len(grupos)} grupos")
        for g in grupos:
            print(f"[DEBUG]   grupo marca={g.get('marca')} modelo={g.get('modelo')} count={g.get('count')}")

        # 3. Calcula confiança pela frequência
        total_confirmados = len(compat_raw)
        top_count = grupos[0]["count"] if grupos else 0
        confianca = round((top_count / total_confirmados) * 100) if total_confirmados > 0 else 0

        # 4. Salva no Supabase
        print(f"[DEBUG] Antes Supabase: {len(grupos)} grupos a salvar")
        salvos = 0
        if grupos:
            salvos = _salvar_supabase(oem, grupos, confianca)
        print(f"[DEBUG] Salvos Supabase: {salvos}")

        # 5. Formata para o frontend (mesmo formato do Railway)
        compat_confirmadas = [
            {
                "veiculo": f"{g.get('marca','')} {g.get('modelo','')}".strip(),
                "anos": f"{g.get('ano_inicial','')} - {g.get('ano_final','')}" if g.get("ano_inicial") else "",
                "detalhes": g.get("motor", ""),
                "marca": g.get("marca"),
                "modelo": g.get("modelo"),
                "motor": g.get("motor"),
                "cambio": g.get("cambio"),
                "ano_inicial": g.get("ano_inicial"),
                "ano_final": g.get("ano_final"),
                "titulo_original": g.get("titulo_original", "")
            }
            for g in grupos
        ]

        print(f"[LOCAL] Concluído em {round(time.time()-t0,1)}s — {len(compat_confirmadas)} compatibilidades")
        return jsonify({
            "ok": True,
            "oem": oem,
            "fonte": "ml_local",
            "compatibilidades_confirmadas": compat_confirmadas,
            "grau_de_confianca": confianca,
            "total_anuncios": len(anuncios),
            "total_confirmados": total_confirmados,
            "salvos_supabase": salvos,
            "tempo_s": round(time.time() - t0, 1)
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "erro": str(e), "tempo_s": round(time.time() - t0, 1)}), 500


@app.route("/status", methods=["GET"])
def status():
    return jsonify({"ok": True, "msg": "Servidor local de compatibilidade OEM rodando"})


if __name__ == "__main__":
    print("=" * 50)
    print("Servidor local de compatibilidade OEM")
    print("Porta: 5001")
    print("Endpoint: POST http://localhost:5001/buscar-compat")
    print("=" * 50)
    app.run(host="127.0.0.1", port=5001, debug=False)
