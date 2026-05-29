# oem_compat_routes.py
# Módulo separado: compatibilidade OEM via busca no Mercado Livre (Playwright)
# Registra rotas no app Flask principal via register_routes(app, cfg_fn)

import json, re, os, subprocess, time
import requests

_DIR      = os.path.dirname(os.path.abspath(__file__))
_NODE     = os.path.join(_DIR, "pw_driver", "node.exe")
_OEM_JS   = os.path.join(_DIR, "ml_oem_compat.js")
_IS_LINUX = os.name == "posix"

_ML_CLIENT_ID     = os.environ.get("ML_CLIENT_ID",     "5450531514024470")
_ML_CLIENT_SECRET = os.environ.get("ML_CLIENT_SECRET", "s9gn1wlLSuHv2JlDbKnhoJYRQziI7YTu")
_ml_token_cache   = {"token": "", "expires_at": 0}

def _get_ml_token():
    if _ml_token_cache["token"] and time.time() < _ml_token_cache["expires_at"] - 30:
        return _ml_token_cache["token"]
    try:
        r = requests.post("https://api.mercadolibre.com/oauth/token", data={
            "grant_type": "client_credentials",
            "client_id": _ML_CLIENT_ID,
            "client_secret": _ML_CLIENT_SECRET,
        }, timeout=10)
        if r.status_code == 200:
            d = r.json()
            _ml_token_cache["token"] = d.get("access_token", "")
            _ml_token_cache["expires_at"] = time.time() + d.get("expires_in", 21600)
            return _ml_token_cache["token"]
    except Exception:
        pass
    return ""

# PartHub Supabase — mesmos dados do api_server.py
_PH_HOST  = "iftzoceaalhpyckuznae.supabase.co"
_PH_ANON  = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImlmdHpvY2VhYWxocHlja3V6bmFlIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NjA0MzMwNjcsImV4cCI6MjA3NjAwOTA2N30.VZY9NLFvRMX-lr9FQUlOkMfE0RfdGxk0HVpslxMYDYg"
_PH_EMAIL = "geisuaine2025@gmail.com"
_PH_SENHA = "Vitoria12$"
_jwt_cache = {"token": None, "expires_at": 0}


def _get_jwt():
    if _jwt_cache["token"] and time.time() < _jwt_cache["expires_at"] - 60:
        return _jwt_cache["token"]
    try:
        r = requests.post(
            f"https://{_PH_HOST}/auth/v1/token?grant_type=password",
            json={"email": _PH_EMAIL, "password": _PH_SENHA},
            headers={"apikey": _PH_ANON, "Content-Type": "application/json"},
            timeout=10
        )
        if r.status_code == 200:
            d = r.json()
            _jwt_cache["token"] = d.get("access_token")
            _jwt_cache["expires_at"] = time.time() + d.get("expires_in", 3600)
            return _jwt_cache["token"]
    except Exception:
        pass
    return None


def _ph_headers():
    jwt = _get_jwt()
    if not jwt:
        return None
    return {
        "apikey": _PH_ANON,
        "Authorization": f"Bearer {jwt}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }


# ─── Playwright: busca ML ────────────────────────────────────────────────────

def _buscar_ml_node(oem):
    """Windows: chama ml_oem_compat.js via node.exe embutido."""
    if not os.path.exists(_NODE) or not os.path.exists(_OEM_JS):
        return None
    try:
        r = subprocess.run(
            [_NODE, _OEM_JS, oem],
            capture_output=True,
            timeout=90
        )
        if r.returncode == 0 and r.stdout:
            return json.loads(r.stdout.decode("utf-8", errors="replace"))
    except Exception as e:
        print(f"[OEM-COMPAT] Node erro: {e}")
    return None


def _buscar_ml_playwright_python(oem):
    """Linux/Railway: usa playwright Python diretamente."""
    try:
        import asyncio
        from playwright.async_api import async_playwright
        import shutil

        exec_path = next(
            (shutil.which(n) for n in ["chromium", "chromium-browser", "google-chrome-stable", "google-chrome"]
             if shutil.which(n)), None
        )

        async def _run():
            async with async_playwright() as p:
                launch_kwargs = dict(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                          "--disable-setuid-sandbox", "--disable-blink-features=AutomationControlled"]
                )
                if exec_path:
                    launch_kwargs["executable_path"] = exec_path
                browser = await p.chromium.launch(**launch_kwargs)
                ctx = await browser.new_context(
                    locale="pt-BR",
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
                    extra_http_headers={"Accept-Language": "pt-BR,pt;q=0.9"}
                )
                page = await ctx.new_page()
                await page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")

                oem_enc = requests.utils.quote(oem)
                await page.goto("https://www.mercadolivre.com.br/",
                                timeout=15000, wait_until="domcontentloaded")
                await page.wait_for_timeout(1000)
                await page.goto(
                    f"https://lista.mercadolivre.com.br/acessorios-veiculos/{oem_enc}",
                    timeout=30000, wait_until="domcontentloaded"
                )
                try:
                    await page.wait_for_selector(
                        "li.ui-search-layout__item, .ui-search-result__wrapper",
                        timeout=10000
                    )
                except Exception:
                    pass
                await page.wait_for_timeout(2000)

                anuncios = await page.evaluate("""() => {
                    const res = [];
                    const items = document.querySelectorAll(
                        'li.ui-search-layout__item, .ui-search-result__wrapper'
                    );
                    for (let i = 0; i < Math.min(items.length, 20); i++) {
                        const t = items[i].querySelector(
                            '.ui-search-item__title, .poly-component__title'
                        );
                        const a = items[i].querySelector('a[href*="mercadolivre"]');
                        const p = items[i].querySelector('.andes-money-amount__fraction');
                        if (t && t.textContent.trim().length > 5)
                            res.push({
                                titulo: t.textContent.trim(),
                                link: a ? a.href.split('?')[0] : '',
                                preco: p ? p.textContent.trim() : ''
                            });
                    }
                    return res;
                }""")
                await browser.close()
                return anuncios

        return asyncio.run(_run())
    except Exception as e:
        print(f"[OEM-COMPAT] Playwright Python erro: {e}")
        return []


_UA_ML = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"

def _buscar_ml_api(oem):
    """Scraping direto do site ML com BeautifulSoup (funciona no Railway sem Playwright)."""
    from bs4 import BeautifulSoup
    query = re.sub(r'\s+', '-', oem.strip())
    urls = [
        f"https://lista.mercadolivre.com.br/acessorios-veiculos/{query}",
        f"https://lista.mercadolivre.com.br/{query}",
    ]
    headers = {
        "User-Agent": _UA_ML,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    }
    anuncios = []
    for url in urls:
        try:
            r = requests.get(url, headers=headers, timeout=20, allow_redirects=True)
            if r.status_code != 200 or len(r.text) < 1000:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            items = (soup.select("li.ui-search-layout__item") or
                     soup.select("div.ui-search-result") or
                     soup.select("[data-item-id]"))
            for item in items:
                t_tag = (item.find(class_="ui-search-item__title") or
                         item.find(class_=re.compile(r"poly-component__title|title|item__title")))
                if not t_tag:
                    continue
                titulo = t_tag.get_text(strip=True)
                if not titulo or len(titulo) < 8:
                    continue
                link_tag = item.find("a", href=True)
                frac = item.find(class_="andes-money-amount__fraction")
                preco = ""
                if frac:
                    try:
                        preco = str(float(frac.get_text(strip=True).replace(".", "").replace(",", "")))
                    except Exception:
                        pass
                anuncios.append({
                    "titulo": titulo,
                    "link": (link_tag["href"].split("?")[0] if link_tag else ""),
                    "preco": preco
                })
            if anuncios:
                break
        except Exception as e:
            print(f"[OEM-COMPAT] Scraping ML erro ({url}): {e}")
            continue
    return anuncios


def _buscar_ml(oem):
    """Tenta Node primeiro (Windows), depois Playwright Python (Linux), depois API direta."""
    resultado = _buscar_ml_node(oem)
    if resultado:
        return resultado
    resultado = _buscar_ml_playwright_python(oem)
    if resultado:
        return resultado
    # Fallback final: API pública do ML (sempre funciona no Railway)
    return _buscar_ml_api(oem)


# ─── Compatibilidade via IA (catálogo OEM) ───────────────────────────────────

def _buscar_compat_via_ia(oem, nome_peca, cfg):
    """Consulta IA com conhecimento de catálogo OEM para determinar compatibilidade."""
    api_key = cfg.get("api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
    gemini_key = cfg.get("gemini_key") or os.environ.get("GEMINI_KEY", "")
    nome_ctx = f"\nNome da peça registrado no sistema: {nome_peca}" if nome_peca else ""

    prompt = f"""Você é um especialista em catálogos de autopeças OEM para o mercado brasileiro.

Código OEM: {oem}{nome_ctx}

TAREFA: Determine a compatibilidade veicular deste código OEM.

REGRAS ABSOLUTAS:
1. Use SOMENTE conhecimento de catálogos oficiais de fabricantes
2. NUNCA invente compatibilidade — se não tiver certeza, retorne lista vazia
3. Cada veículo deve ter marca, modelo, motor e anos confirmados
4. grau_de_confianca: 90-100 = confirmado por catálogo, 70-89 = provável, abaixo de 70 = incerto

Retorne SOMENTE JSON válido:
{{
  "nome_peca": "nome comercial da peça",
  "compatibilidades_confirmadas": [
    {{"veiculo": "Marca Modelo Versão Motor", "anos": "2018 2019 2020 2021", "detalhes": "1.6 Flex"}}
  ],
  "grau_de_confianca": 0,
  "observacoes": "fonte do catálogo ou aviso se incerto"
}}

Se não souber a peça ou compatibilidade: retorne compatibilidades_confirmadas vazio e grau_de_confianca 0."""

    def _chamar_gemini(key, p):
        for modelo in ["gemini-2.5-flash", "gemini-2.0-flash"]:
            try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent?key={key}"
                r = requests.post(url, json={
                    "contents": [{"parts": [{"text": p}]}],
                    "generationConfig": {"maxOutputTokens": 600, "temperature": 0.1,
                                        "thinkingConfig": {"thinkingBudget": 0}}
                }, timeout=30)
                if r.status_code == 200:
                    parts = r.json()["candidates"][0]["content"]["parts"]
                    text = " ".join(x["text"] for x in parts if "text" in x)
                    m = re.search(r"\{[\s\S]*\}", text)
                    if m:
                        return json.loads(m.group(0))
            except Exception:
                continue
        return None

    def _chamar_claude(key, p):
        try:
            r = requests.post("https://api.anthropic.com/v1/messages",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": "claude-haiku-4-5-20251001", "max_tokens": 600,
                      "messages": [{"role": "user", "content": p}]}, timeout=30)
            text = r.json()["content"][0]["text"]
            m = re.search(r"\{[\s\S]*\}", text)
            return json.loads(m.group(0)) if m else None
        except Exception:
            return None

    resultado = None
    provider = cfg.get("provider", "gemini")
    if provider == "gemini" and gemini_key:
        resultado = _chamar_gemini(gemini_key, prompt)
    if not resultado and api_key:
        resultado = _chamar_claude(api_key, prompt)
    if not resultado and gemini_key:
        resultado = _chamar_gemini(gemini_key, prompt)

    if not resultado:
        return {"compatibilidades_confirmadas": [], "grau_de_confianca": 0,
                "nome_peca": nome_peca or oem, "observacoes": "IA indisponível"}

    # Garante campos obrigatórios
    resultado.setdefault("compatibilidades_confirmadas", [])
    resultado.setdefault("grau_de_confianca", 0)
    resultado.setdefault("nome_peca", nome_peca or oem)

    # Normaliza lista de compatibilidades para o formato do frontend
    compat = resultado.get("compatibilidades_confirmadas") or []
    resultado["anuncios"] = [
        {"veiculo": c.get("veiculo",""), "anos": c.get("anos",""),
         "motor": c.get("detalhes",""), "titulo_original": c.get("veiculo","")}
        for c in compat
    ]
    resultado["consenso"] = {
        "compatibilidades": compat,
        "confianca": resultado.get("grau_de_confianca", 0)
    }
    return resultado


# ─── Análise com AI ──────────────────────────────────────────────────────────

def _extrair_com_ai(anuncios, oem, cfg):
    """Usa Claude Haiku para extrair marca/modelo/motor/anos dos títulos — só analisa o que foi encontrado."""
    api_key = cfg.get("api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key or not anuncios:
        return _extrair_regex(anuncios, oem)

    titulos_txt = "\n".join([f"{i+1}. {a['titulo']}" for i, a in enumerate(anuncios)])

    prompt = f"""Analise os títulos de anúncios abaixo encontrados no Mercado Livre pesquisando o OEM: {oem}

Títulos:
{titulos_txt}

Para cada título, extraia SOMENTE informações que estão EXPLICITAMENTE no texto:
- oem_confirmado: true se o código "{oem}" aparece no título (ignorar variações)
- marca: marca do veículo (Renault, Nissan, Fiat, etc.) ou null
- modelo: modelo do veículo (Duster, Kicks, Uno, etc.) ou null
- motor: cilindrada (1.6, 2.0, etc.) ou null
- cambio: Manual ou Automatico se mencionado, senão null
- ano_inicial: primeiro ano mencionado (inteiro) ou null
- ano_final: segundo ano ou último ano mencionado (inteiro) ou null

NÃO invente informações. Se não estiver no título, coloque null.

Retorne APENAS um JSON array (sem explicações, sem markdown):
[{{"indice":1,"oem_confirmado":true,"marca":"Renault","modelo":"Duster","motor":"1.6","cambio":null,"ano_inicial":2012,"ano_final":2018}}]"""

    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json"
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 2048,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=30
        )
        if r.status_code == 200:
            txt = r.json()["content"][0]["text"].strip()
            m = re.search(r'\[.*\]', txt, re.DOTALL)
            if m:
                ai_results = json.loads(m.group())
                # Mescla resultado AI com dados originais
                resultado = []
                for i, a in enumerate(anuncios):
                    ai = next((x for x in ai_results if x.get("indice") == i + 1), {})
                    resultado.append({
                        "titulo_original": a["titulo"],
                        "link": a.get("link", ""),
                        "preco": a.get("preco", ""),
                        "oem_confirmado": ai.get("oem_confirmado", False),
                        "marca": ai.get("marca"),
                        "modelo": ai.get("modelo"),
                        "motor": ai.get("motor"),
                        "cambio": ai.get("cambio"),
                        "ano_inicial": ai.get("ano_inicial"),
                        "ano_final": ai.get("ano_final")
                    })
                return resultado
    except Exception as e:
        print(f"[OEM-COMPAT] AI erro: {e}")

    return _extrair_regex(anuncios, oem)


def _extrair_regex(anuncios, oem):
    """Extração por regex quando AI não está disponível."""
    _MARCAS = [
        "renault", "nissan", "fiat", "volkswagen", "vw", "ford", "chevrolet",
        "toyota", "honda", "hyundai", "kia", "peugeot", "citroen", "mitsubishi",
        "jeep", "dodge", "ram", "bmw", "mercedes", "audi", "volvo"
    ]
    oem_upper = re.sub(r'\s+', '', oem.upper())
    resultado = []

    for a in anuncios:
        titulo = a.get("titulo", "")
        titulo_clean = re.sub(r'\s+', '', titulo.upper())
        oem_confirmado = oem_upper in titulo_clean

        marca = None
        for m in _MARCAS:
            if re.search(r'\b' + m + r'\b', titulo, re.IGNORECASE):
                marca = m.title()
                break

        anos = re.findall(r'\b(20\d{2})\b', titulo)
        ano_ini = int(min(anos)) if anos else None
        ano_fim = int(max(anos)) if anos else None

        motor = None
        m_motor = re.search(r'\b(\d+[.,]\d)\s*(?:v\b|turbo|flex|aspirado)?', titulo, re.IGNORECASE)
        if m_motor:
            motor = m_motor.group(1).replace(",", ".")

        resultado.append({
            "titulo_original": titulo,
            "link": a.get("link", ""),
            "preco": a.get("preco", ""),
            "oem_confirmado": oem_confirmado,
            "marca": marca,
            "modelo": None,
            "motor": motor,
            "cambio": None,
            "ano_inicial": ano_ini,
            "ano_final": ano_fim
        })
    return resultado


# ─── Consenso ────────────────────────────────────────────────────────────────

def _calcular_consenso(anuncios_extraidos):
    """Agrupa por veículo e calcula confiança por frequência."""
    confirmados = [a for a in anuncios_extraidos if a.get("oem_confirmado")]
    total = len(confirmados)

    if total == 0:
        return {
            "consenso": None,
            "confianca": 0,
            "mensagem": "Nenhum anúncio com o OEM confirmado no título",
            "veiculos_encontrados": [],
            "total_confirmados": 0,
            "total_anuncios": len(anuncios_extraidos)
        }

    # Agrupa por marca + modelo (None é agrupado separadamente)
    grupos = {}
    for a in confirmados:
        marca = (a.get("marca") or "").strip()
        modelo = (a.get("modelo") or "").strip()
        if not marca and not modelo:
            continue
        chave = f"{marca.lower()}|{modelo.lower()}"
        if chave not in grupos:
            grupos[chave] = {
                "marca": marca or None,
                "modelo": modelo or None,
                "motor": a.get("motor"),
                "cambio": a.get("cambio"),
                "ano_inicial": a.get("ano_inicial"),
                "ano_final": a.get("ano_final"),
                "count": 0,
                "titulos": []
            }
        g = grupos[chave]
        g["count"] += 1
        g["titulos"].append(a.get("titulo_original", "")[:80])
        # Expande range de anos
        if a.get("ano_inicial"):
            g["ano_inicial"] = min(filter(None, [g["ano_inicial"], a["ano_inicial"]]))
        if a.get("ano_final"):
            g["ano_final"] = max(filter(None, [g["ano_final"], a["ano_final"]]))

    if not grupos:
        return {
            "consenso": None,
            "confianca": 0,
            "mensagem": "OEM confirmado nos títulos mas não foi possível extrair marca/modelo",
            "veiculos_encontrados": [],
            "total_confirmados": total,
            "total_anuncios": len(anuncios_extraidos),
            "anuncios_confirmados": [a["titulo_original"] for a in confirmados[:5]]
        }

    ordenados = sorted(grupos.values(), key=lambda x: x["count"], reverse=True)
    top = ordenados[0]
    confianca = round((top["count"] / total) * 100)

    veiculos = [
        {
            "marca": g["marca"],
            "modelo": g["modelo"],
            "motor": g["motor"],
            "ano_inicial": g["ano_inicial"],
            "ano_final": g["ano_final"],
            "count": g["count"],
            "percentual": round((g["count"] / total) * 100)
        }
        for g in ordenados[:5]
    ]

    if confianca < 50:
        return {
            "consenso": None,
            "confianca": confianca,
            "mensagem": "Compatibilidade não confirmada — divergências encontradas",
            "veiculos_encontrados": veiculos,
            "total_confirmados": total,
            "total_anuncios": len(anuncios_extraidos)
        }

    veiculo_str = f"{top['marca'] or ''} {top['modelo'] or ''}".strip()
    motor_str = f" {top['motor']}" if top.get("motor") else ""
    anos_str = ""
    if top.get("ano_inicial"):
        anos_str = f" {top['ano_inicial']}"
        if top.get("ano_final") and top["ano_final"] != top["ano_inicial"]:
            anos_str += f"-{top['ano_final']}"

    return {
        "consenso": {
            "marca": top["marca"],
            "modelo": top["modelo"],
            "motor": top["motor"],
            "cambio": top["cambio"],
            "ano_inicial": top["ano_inicial"],
            "ano_final": top["ano_final"]
        },
        "confianca": confianca,
        "mensagem": f"Compatibilidade sugerida: {veiculo_str}{motor_str}{anos_str} ({confianca}% de confiança)",
        "veiculos_encontrados": veiculos,
        "total_confirmados": total,
        "total_anuncios": len(anuncios_extraidos),
        "anuncios_base": [a["titulo_original"] for a in confirmados[:5]]
    }


# ─── Registro de rotas ───────────────────────────────────────────────────────

def register_routes(app, cfg_fn):
    """
    Registra rotas no app Flask.
    cfg_fn: callable que retorna dict de configuração (api_key, etc.)
    """
    from flask import request, jsonify, Response

    def _options():
        r = Response("", status=204)
        r.headers["Access-Control-Allow-Origin"] = "*"
        r.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        r.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        return r

    def _buscar_supabase_cache(oem):
        """Retorna compatibilidades salvas no Supabase para este OEM, ou None se não tiver."""
        hdrs = _ph_headers()
        if not hdrs:
            return None
        try:
            r = requests.get(
                f"https://{_PH_HOST}/rest/v1/oem_compatibilidades",
                headers=hdrs,
                params={"oem": f"eq.{oem}", "order": "confianca.desc", "limit": "20"},
                timeout=8
            )
            if r.status_code == 200:
                rows = r.json()
                if rows:
                    return rows
        except Exception as e:
            print(f"[OEM-COMPAT] Supabase cache erro: {e}")
        return None

    @app.route("/compatibilidade/buscar-oem", methods=["POST", "OPTIONS"])
    def compat_buscar_oem():
        if request.method == "OPTIONS":
            return _options()
        data = request.get_json(force=True) or {}
        oem = (data.get("oem") or "").strip().upper()
        nome_peca = (data.get("nome_peca") or "").strip()
        if not oem or len(oem) < 4:
            return jsonify({"ok": False, "erro": "OEM inválido (mínimo 4 caracteres)"}), 400

        print(f"[OEM-COMPAT] Buscando OEM: {oem}" + (f" | nome: {nome_peca}" if nome_peca else ""))
        t0 = time.time()

        # 1. Verifica cache no Supabase (dados reais do ML)
        cache = _buscar_supabase_cache(oem)
        if cache:
            confianca = cache[0].get("confianca", 0)
            compat = [
                {
                    "veiculo": f"{row.get('marca','')} {row.get('modelo','')}".strip(),
                    "anos": (f"{row['ano_inicial']} - {row['ano_final']}"
                             if row.get("ano_inicial") and row.get("ano_final")
                             else str(row.get("ano_inicial", "") or "")),
                    "detalhes": row.get("motor", "") or "",
                    "marca": row.get("marca"),
                    "modelo": row.get("modelo"),
                    "motor": row.get("motor"),
                    "cambio": row.get("cambio"),
                    "ano_inicial": row.get("ano_inicial"),
                    "ano_final": row.get("ano_final"),
                }
                for row in cache
            ]
            print(f"[OEM-COMPAT] Cache Supabase: {len(compat)} registros em {round(time.time()-t0,2)}s")
            return jsonify({
                "ok": True,
                "oem": oem,
                "fonte": "supabase_cache",
                "compatibilidades_confirmadas": compat,
                "grau_de_confianca": confianca,
                "tempo_s": round(time.time() - t0, 1)
            })

        # 2. Sem dados reais — retorna sem_dados para o frontend chamar servidor local
        print(f"[OEM-COMPAT] Sem dados no Supabase para OEM {oem} — retornando sem_dados")
        return jsonify({
            "ok": True,
            "oem": oem,
            "fonte": "sem_dados",
            "compatibilidades_confirmadas": [],
            "grau_de_confianca": 0,
            "mensagem": "OEM sem dados no banco. Execute o servidor local para buscar no Mercado Livre.",
            "tempo_s": round(time.time() - t0, 1)
        })

    @app.route("/compatibilidade/salvar", methods=["POST", "OPTIONS"])
    def compat_salvar():
        if request.method == "OPTIONS":
            return _options()
        data = request.get_json(force=True) or {}
        oem    = (data.get("oem") or "").strip().upper()
        marca  = (data.get("marca") or "").strip()
        modelo = (data.get("modelo") or "").strip()

        if not oem or not marca:
            return jsonify({"ok": False, "erro": "oem e marca são obrigatórios"}), 400

        payload = {
            "oem": oem,
            "marca": marca,
            "modelo": modelo or None,
            "motor": (data.get("motor") or "").strip() or None,
            "cambio": (data.get("cambio") or "").strip() or None,
            "ano_inicial": data.get("ano_inicial") or None,
            "ano_final": data.get("ano_final") or None,
            "fonte": "mercadolivre",
            "confianca": int(data.get("confianca") or 0),
            "data_validacao": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        }

        hdrs = _ph_headers()
        if not hdrs:
            return jsonify({"ok": False, "erro": "Falha de autenticação no Supabase"}), 500

        try:
            r = requests.post(
                f"https://{_PH_HOST}/rest/v1/oem_compatibilidades",
                headers=hdrs,
                json=payload,
                timeout=10
            )
            if r.status_code in (200, 201):
                saved = r.json()
                return jsonify({"ok": True, "msg": f"OEM {oem} salvo", "id": saved[0].get("id") if saved else None})
            return jsonify({"ok": False, "erro": f"Supabase {r.status_code}: {r.text[:300]}"}), 500
        except Exception as e:
            return jsonify({"ok": False, "erro": str(e)}), 500

    @app.route("/compatibilidade/buscar-salvos", methods=["GET", "OPTIONS"])
    def compat_buscar_salvos():
        if request.method == "OPTIONS":
            return _options()
        oem = request.args.get("oem", "").strip().upper()
        if not oem:
            return jsonify({"ok": False, "erro": "?oem= obrigatório"}), 400

        hdrs = _ph_headers()
        if not hdrs:
            return jsonify({"ok": True, "dados": [], "aviso": "Supabase não autenticado"})

        try:
            r = requests.get(
                f"https://{_PH_HOST}/rest/v1/oem_compatibilidades",
                headers=hdrs,
                params={"oem": f"eq.{oem}", "order": "data_validacao.desc", "limit": "10"},
                timeout=10
            )
            return jsonify({"ok": True, "dados": r.json() if r.status_code == 200 else []})
        except Exception as e:
            return jsonify({"ok": False, "erro": str(e)}), 500

    print("[OEM-COMPAT] Rotas registradas: /compatibilidade/buscar-oem, /compatibilidade/salvar, /compatibilidade/buscar-salvos")
