"""
local_compat_server.py
Servidor local (Windows) para buscar compatibilidade OEM no Mercado Livre via Playwright.
Roda em localhost:5001. Execute: python local_compat_server.py

Fluxo:
  Frontend → Railway (checa Supabase) → sem dados → chama localhost:5001
  localhost:5001 → Playwright → ML → IA extrai compatibilidade → salva Supabase → retorna
"""

import json, re, os, time
import requests
from flask import Flask, request, jsonify, Response
from playwright.sync_api import sync_playwright

app = Flask(__name__)

# ─── Configuração ─────────────────────────────────────────────────────────────

# CRM Supabase — tabela oem_compatibilidades com RLS pública (sem auth necessária)
_CRM_HOST = "uthsiihzpsgarargegcw.supabase.co"
_CRM_ANON = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InV0aHNpaWh6cHNnYXJhcmdlZ2N3Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzkxMTAyMzcsImV4cCI6MjA5NDY4NjIzN30.vLsk56k7VUROClBmyg4NJFXHtpTGmr1f0Xl_dARbtZE"

_GEMINI_KEY = os.environ.get("GEMINI_KEY", "AIzaSyCG0XhzMPJi6w0mB3v3Fg5ISmxLxnYGi4A")
_CLAUDE_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


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
    print(f"[ML] Buscando: {url}")

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

        page.goto(url, timeout=30000, wait_until="domcontentloaded")
        try:
            page.wait_for_selector(
                "li.ui-search-layout__item, .ui-search-result__wrapper, .poly-card",
                timeout=12000
            )
        except Exception:
            pass
        page.wait_for_timeout(2000)

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

    print(f"[ML] Encontrou {len(anuncios)} anúncios")
    return anuncios


# ─── IA: extrair compatibilidade dos títulos ─────────────────────────────────

def _extrair_compat_ia(oem: str, anuncios: list) -> list:
    """Usa IA para extrair marca/modelo/motor/anos dos títulos — retorna lista de compatibilidades."""
    if not anuncios:
        return []

    titulos_txt = "\n".join([f"{i+1}. {a['titulo']}" for i, a in enumerate(anuncios)])
    prompt = f"""Analise os títulos de anúncios do Mercado Livre Brasil sobre a peça OEM: {oem}

Títulos:
{titulos_txt}

Para cada título, extraia SOMENTE dados EXPLÍCITOS no texto:
- oem_confirmado: true se "{oem}" (sem espaços) aparece no título
- marca: marca do veículo (Renault, Nissan, Fiat, VW, Ford, Chevrolet, Hyundai, Toyota, Honda, etc.) ou null
- modelo: modelo do veículo (Duster, Kicks, Uno, Gol, Onix, etc.) ou null
- motor: cilindrada (1.6, 2.0, 1.5 Turbo, etc.) ou null
- cambio: Manual ou Automatico se mencionado, senão null
- ano_inicial: primeiro ano (4 dígitos) ou null
- ano_final: último ano (4 dígitos) ou null

NÃO invente. Se não estiver no título, coloque null.
RETORNE APENAS JSON array:
[{{"indice":1,"oem_confirmado":true,"marca":"Renault","modelo":"Duster","motor":"1.6","cambio":null,"ano_inicial":2012,"ano_final":2018}}]"""

    def _gemini(p):
        for modelo in ["gemini-2.5-flash", "gemini-2.0-flash"]:
            try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent?key={_GEMINI_KEY}"
                r = requests.post(url, json={
                    "contents": [{"parts": [{"text": p}]}],
                    "generationConfig": {"maxOutputTokens": 2000, "temperature": 0.0,
                                        "thinkingConfig": {"thinkingBudget": 0}}
                }, timeout=30)
                if r.status_code == 200:
                    parts = r.json()["candidates"][0]["content"]["parts"]
                    text = " ".join(x["text"] for x in parts if "text" in x)
                    m = re.search(r'\[.*\]', text, re.DOTALL)
                    if m:
                        return json.loads(m.group(0))
            except Exception as e:
                print(f"[IA] Gemini {modelo} erro: {e}")
        return None

    def _claude(p):
        if not _CLAUDE_KEY:
            return None
        try:
            r = requests.post("https://api.anthropic.com/v1/messages",
                headers={"x-api-key": _CLAUDE_KEY, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": "claude-haiku-4-5-20251001", "max_tokens": 2000,
                      "messages": [{"role": "user", "content": p}]}, timeout=30)
            text = r.json()["content"][0]["text"].strip()
            m = re.search(r'\[.*\]', text, re.DOTALL)
            return json.loads(m.group(0)) if m else None
        except Exception as e:
            print(f"[IA] Claude erro: {e}")
            return None

    ai_results = _gemini(prompt) or _claude(prompt)
    if not ai_results:
        print("[IA] Sem resultado da IA")
        return []

    compatibilidades = []
    for i, a in enumerate(anuncios):
        ai = next((x for x in ai_results if x.get("indice") == i + 1), {})
        if not ai.get("oem_confirmado") or not ai.get("marca"):
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

        # 2. IA extrai compatibilidade dos títulos
        compat_raw = _extrair_compat_ia(oem, anuncios)
        grupos = _agrupar_compat(compat_raw)

        # 3. Calcula confiança pela frequência
        total_confirmados = len(compat_raw)
        top_count = grupos[0]["count"] if grupos else 0
        confianca = round((top_count / total_confirmados) * 100) if total_confirmados > 0 else 0

        # 4. Salva no Supabase
        if grupos:
            _salvar_supabase(oem, grupos, confianca)

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
            "salvos_supabase": len(grupos) if grupos else 0,
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
