"""
api_server.py — Servidor local WRX-Search (porta 5678)
Expõe /buscar?q=CODIGO para o Desmonte X preencher o formulário automaticamente.

Instalar dependências (uma vez):
    pip install flask

Rodar:
    python api_server.py
"""

import json, re, os, subprocess, time, threading, sys
import requests

# ─── Config ───────────────────────────────────────────────────────────────────
# Railway define PORT via env var; local usa 5678
PORT        = int(os.environ.get("PORT", 5678))
_IS_RAILWAY = bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"))
_DIR        = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(_DIR, "config.json")
NCM_FILE    = os.path.join(_DIR, "ncm_cest.json")
DB_FILE     = os.path.join(_DIR, "carros_db.json")
FOTOS_DIR   = os.path.join(_DIR, "fotos_carros")
_NODE       = os.path.join(_DIR, "pw_driver", "node.exe")
_SCRAPER    = os.path.join(_DIR, "scraper.js")
_UA_ML      = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
ML_CLIENT_ID     = "4767104516094208"
ML_CLIENT_SECRET = "32qBHwkLG6X18SuzIN74TxG8SvIgzBiv"

def carregar_config():
    # Env vars têm prioridade (Railway); fallback para config.json local
    cfg = {}
    try:
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
    except Exception:
        pass
    if os.environ.get("ANTHROPIC_API_KEY"):
        cfg["api_key"] = os.environ["ANTHROPIC_API_KEY"]
    if os.environ.get("GEMINI_API_KEY"):
        cfg["gemini_key"] = os.environ["GEMINI_API_KEY"]
    if os.environ.get("AI_PROVIDER"):
        cfg["provider"] = os.environ["AI_PROVIDER"]
    return cfg

def buscar_ncm_local(nome_peca):
    if not nome_peca:
        return None
    try:
        with open(NCM_FILE, encoding="utf-8") as f:
            lista = json.load(f)
        nome = nome_peca.lower()
        for entry in lista:
            if any(k.lower() in nome for k in entry.get("keywords", [])):
                return entry
    except Exception:
        pass
    return None

def calcular_preco_sugerido(prices):
    if not prices:
        return 0
    s = sorted(prices)
    if len(s) == 1:
        return round(s[0], 2)
    mediana_bruta = s[len(s) // 2]
    filtrados = [p for p in s if p >= mediana_bruta * 0.25 and p <= mediana_bruta * 3.0]
    if not filtrados:
        filtrados = s
    n = len(filtrados)
    corte = max(1, int(n * 0.15)) if n > 4 else 0
    meio = filtrados[corte: n - corte] if corte else filtrados
    if not meio:
        meio = filtrados
    return round(meio[len(meio) // 2], 2)

# ─── ML OAuth ─────────────────────────────────────────────────────────────────
def _get_ml_token():
    try:
        r = requests.post("https://api.mercadolibre.com/oauth/token", data={
            "grant_type": "client_credentials",
            "client_id": ML_CLIENT_ID,
            "client_secret": ML_CLIENT_SECRET,
        }, timeout=10)
        if r.status_code == 200:
            return r.json().get("access_token", "")
    except Exception:
        pass
    return ""

# ─── Parsing HTML ─────────────────────────────────────────────────────────────
def _has_resultados(html):
    return any(c in html for c in [
        "ui-search-layout__item", "ui-search-result", "andes-money-amount__fraction"
    ])

def _parse_html_ml(html):
    from bs4 import BeautifulSoup
    titles, novos, usados = [], [], []
    soup  = BeautifulSoup(html, "html.parser")
    items = (soup.select("li.ui-search-layout__item") or
             soup.select("div.ui-search-result") or
             soup.select("[data-item-id]"))
    for item in items:
        t_tag = (item.find(class_="ui-search-item__title") or
                 item.find(class_=re.compile(r"title|item__title|name")))
        if t_tag:
            t = t_tag.get_text(strip=True)
            if len(t) > 8 and t not in titles:
                titles.append(t)
        frac = (item.find(class_="andes-money-amount__fraction") or
                item.find(class_="price-tag-fraction"))
        if not frac:
            meta = item.find("meta", itemprop="price")
            if meta:
                try:
                    p = float(str(meta.get("content", "0")).replace(",", "."))
                    if 5 < p < 100000:
                        novos.append(p)
                except Exception:
                    pass
            continue
        cent = item.find(class_=re.compile(r"andes-money-amount__cents"))
        try:
            inteiro = float(frac.get_text(strip=True).replace(".", "").replace(",", ""))
            decimal = float(cent.get_text(strip=True).replace(",", ".")) / 100 if cent else 0
            p = round(inteiro + decimal, 2)
            if not (5 < p < 100000):
                continue
        except Exception:
            continue
        if item.find(string=re.compile(r"[Uu]sado")):
            usados.append(p)
        else:
            novos.append(p)
    return titles, novos, usados

# ─── Camada 1: API ML ─────────────────────────────────────────────────────────
def _buscar_api_ml(codigo):
    titles, novos, usados = [], [], []
    token   = _get_ml_token()
    headers = {"Accept": "application/json", "Accept-Language": "pt-BR,pt;q=0.9"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for offset in [0, 20, 40]:
        try:
            r = requests.get("https://api.mercadolibre.com/sites/MLB/search",
                             params={"q": codigo, "limit": 20, "offset": offset},
                             headers=headers, timeout=20)
            if r.status_code != 200:
                break
            data = r.json().get("results", [])
            if not data:
                break
            for item in data:
                t    = item.get("title", "")
                p    = float(item.get("price") or 0)
                cond = item.get("condition", "new")
                if t and t not in titles:
                    titles.append(t)
                if p > 5:
                    (usados if cond == "used" else novos).append(p)
        except Exception:
            break
    return titles, novos, usados

# ─── Camada 2: Node.js Playwright ─────────────────────────────────────────────
def _buscar_navegador_raw(urls):
    htmls = []
    if os.path.exists(_NODE) and os.path.exists(_SCRAPER):
        for url in urls:
            try:
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                r = subprocess.run(
                    [_NODE, _SCRAPER, url],
                    capture_output=True, text=True, timeout=45,
                    encoding="utf-8", errors="replace",
                    creationflags=0x08000000, startupinfo=si,
                    cwd=_DIR
                )
                h = r.stdout
                if h and _has_resultados(h):
                    htmls.append(h)
                    break
            except Exception:
                continue
    return htmls

def _buscar_navegador(urls):
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(_buscar_navegador_raw, urls)
        try:
            return fut.result(timeout=50)
        except Exception:
            return []

# ─── Camada 3: PowerShell (apenas Windows) ────────────────────────────────────
def _baixar_html_via_powershell(url, timeout_sec=25):
    if os.name != 'nt':
        return ""
    ps_cmd = (
        "$ProgressPreference='SilentlyContinue'; "
        "$headers=@{"
        f"'User-Agent'='{_UA_ML}';"
        "'Accept'='text/html,application/xhtml+xml';"
        "'Accept-Language'='pt-BR,pt;q=0.9'"
        "}; "
        f"(Invoke-WebRequest -UseBasicParsing -Uri '{url}' -Headers $headers -TimeoutSec {timeout_sec}).Content"
    )
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        r = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=timeout_sec + 10,
            creationflags=0x08000000, startupinfo=si)
        return r.stdout if r.stdout and len(r.stdout) > 1000 else ""
    except Exception:
        return ""

# ─── Camada 3b: Playwright Python (Railway/Linux) ─────────────────────────────
def _chromium_exec():
    import shutil
    for nome in ["chromium", "chromium-browser", "google-chrome", "google-chrome-stable"]:
        p = shutil.which(nome)
        if p:
            return p
    return None

def _buscar_playwright_python(urls):
    try:
        import asyncio
        from playwright.async_api import async_playwright
        exec_path = _chromium_exec()

        async def _run():
            async with async_playwright() as p:
                launch_args = dict(
                    headless=True,
                    args=["--no-sandbox","--disable-dev-shm-usage","--disable-gpu","--disable-setuid-sandbox"]
                )
                if exec_path:
                    launch_args["executable_path"] = exec_path
                browser = await p.chromium.launch(**launch_args)
                ctx = await browser.new_context(locale="pt-BR", user_agent=_UA_ML)
                page = await ctx.new_page()
                for url in urls:
                    try:
                        await page.goto(url, timeout=25000, wait_until="domcontentloaded")
                        await page.wait_for_selector(
                            "li.ui-search-layout__item, div.ui-search-result",
                            timeout=10000
                        )
                        html = await page.content()
                        await browser.close()
                        if _has_resultados(html):
                            return [html]
                    except Exception:
                        continue
                await browser.close()
            return []

        return asyncio.run(_run())
    except Exception:
        return []

# ─── Camada 4: requests direto + extração JSON embarcado ─────────────────────
def _extrair_json_ml_page(html):
    """Extrai títulos e preços do JSON embarcado que o ML inclui no HTML (SSR)."""
    titles, novos, usados = [], [], []
    import json as _json
    patterns = [
        r'window\.__PRELOADED_STATE__\s*=\s*(\{.*?\})(?:\s*;|\s*</script)',
        r'"initialState"\s*:\s*(\{.*?"results".*?\})',
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
    ]
    for pat in patterns:
        for m in re.finditer(pat, html, re.DOTALL):
            try:
                obj = _json.loads(m.group(1))
                results = obj if isinstance(obj, list) else (
                    obj.get("results") or obj.get("items") or
                    obj.get("search", {}).get("results") or []
                )
                for item in (results if isinstance(results, list) else []):
                    t = item.get("title") or item.get("name", "")
                    p = float(item.get("price") or item.get("original_price") or 0)
                    cond = item.get("condition", "new")
                    if t and len(t) > 5 and t not in titles:
                        titles.append(t)
                    if p > 5:
                        (usados if cond == "used" else novos).append(p)
            except Exception:
                continue
    return titles, novos, usados

def _buscar_requests_html(urls):
    headers = {
        "User-Agent": _UA_ML,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    }
    for url in urls:
        try:
            r = requests.get(url, headers=headers, timeout=20, allow_redirects=True)
            if r.status_code == 200 and len(r.text) > 1000:
                return [r.text]
        except Exception:
            continue
    return []

# ─── Orquestrador ─────────────────────────────────────────────────────────────
def buscar_ml(codigo):
    titles, novos, usados = [], [], []
    query    = re.sub(r'\s+', '-', codigo.strip())
    urls_html = [
        f"https://lista.mercadolivre.com.br/acessorios-veiculos/{query}",
        f"https://lista.mercadolivre.com.br/{query}",
    ]

    def _absorver(t2, n2, u2):
        titles.extend(t for t in t2 if t not in titles)
        novos.extend(n2)
        usados.extend(u2)

    _absorver(*_buscar_api_ml(codigo))

    if not novos and not usados:
        for h in _buscar_navegador(urls_html):
            _absorver(*_parse_html_ml(h))

    if not novos and not usados:
        for url in urls_html:
            html_ps = _baixar_html_via_powershell(url)
            if html_ps and _has_resultados(html_ps):
                _absorver(*_parse_html_ml(html_ps))
                break

    # Camada 3b: Playwright Python (Railway/Linux)
    if not novos and not usados:
        for h in _buscar_playwright_python(urls_html):
            _absorver(*_parse_html_ml(h))

    # Camada 4: requests + extração JSON embarcado (Railway/Linux fallback)
    if not novos and not usados:
        for h in _buscar_requests_html(urls_html):
            _absorver(*_extrair_json_ml_page(h))
            if not novos and not usados:
                _absorver(*_parse_html_ml(h))

    novos  = sorted(set(round(p, 2) for p in novos))[:15]
    usados = sorted(set(round(p, 2) for p in usados))[:15]
    return titles[:15], novos, usados

# ─── IA: prompt + ajuste + chamada ────────────────────────────────────────────
def _build_prompt(codigo, titles, prices):
    tlist = "\n".join(f"- {t}" for t in titles) if titles else "(sem anúncios encontrados no ML — use seu conhecimento do código OEM)"
    plist = ", ".join(f"R$ {p:.2f}" for p in prices) if prices else "(sem preços — sugira baseado no mercado brasileiro)"
    return f"""Analise a peça automotiva com código OEM: {codigo}

Anúncios Mercado Livre:
{tlist}

Preços encontrados: {plist}

Retorne APENAS um JSON com esta estrutura exata (sem texto fora):
{{
  "titulos_otimizados": ["titulo padrao1 sem codigo", "titulo padrao2 com codigo", "titulo padrao3 com anos", "titulo padrao4 com marca"],
  "titulo_ia": "titulo completo com codigo no final",
  "preco_sugerido": 0.00,
  "compatibilidade": [
    {{"veiculo": "Renault Logan", "anos": "2014 a 2024", "status": "COMPATÍVEL"}}
  ],
  "versoes": [
    {{"veiculo": "Renault Logan", "anos": "2014 2015 2016 2017 2018 2019 2020 2021 2022 2023 2024", "detalhes": "Motor 1.0, 1.6"}}
  ],
  "explicacao": "o que é a peça e para que serve em 2 linhas max",
  "funcao": "função técnica em 1 linha",
  "categoria": "categoria do produto (ex: Motor e Câmbio, Suspensão, Elétrica, Freios, Carroceria, Ar-condicionado, Iluminação)"
}}

Regras:
- titulos_otimizados: EXATAMENTE 4, máximo 60 caracteres cada (serão reorganizados pelo sistema)
- titulo_ia: máximo 60 chars, código {codigo} SEMPRE ÚLTIMO ELEMENTO
- compatibilidade: APENAS veículos CONFIRMADOS para este código exato. NÃO inclua por suposição de marca.
  Ex: se é Peugeot 208, NÃO inclua Peugeot 308. Se é Fiat Uno, NÃO duplique como "Fiat Uno Way".
  Use o nome base do modelo (Fiat Uno, não "Fiat Uno Way Attractive").
- versoes: MESMO conjunto que compatibilidade, com anos individuais e detalhes de motor.
  NUNCA repita o mesmo modelo duas vezes.
- versoes.anos: listar CADA ANO individualmente separado por espaço (NUNCA "a" ou "-")
- versoes.detalhes: OBRIGATÓRIO — motores compatíveis ex: "Motor 1.0, 1.3" ou "Motor 1.0 Turbo"
- categoria: escolha a mais adequada para Mercado Livre Peças Automotivas
- Responda SOMENTE o JSON válido"""

def _ajustar_titulos(data, codigo):
    def so_truncar(titulo):
        if not titulo: return titulo
        t = titulo.strip().replace("  ", " ").replace(codigo, "").strip()
        return t[:60].strip()

    def com_codigo(titulo):
        if not titulo: return titulo
        t = titulo.strip().replace(codigo, "").strip()
        espaco = 60 - len(codigo) - 1
        base = t[:espaco].strip()
        return f"{base} {codigo}"[:60]

    if "titulos_otimizados" in data:
        titulos = data["titulos_otimizados"]
        while len(titulos) < 4:
            titulos.append(titulos[-1] if titulos else codigo)
        data["titulos_otimizados"] = [
            so_truncar(titulos[0]),
            com_codigo(titulos[1]),
            so_truncar(titulos[2]),
            so_truncar(titulos[3]),
        ]
    if "titulo_ia" in data:
        data["titulo_ia"] = com_codigo(data["titulo_ia"])
    return data

def _chamar_ia(prompt):
    cfg = carregar_config()
    provider = cfg.get("provider", "gemini")
    if provider == "gemini":
        return _gemini(cfg.get("gemini_key", ""), prompt)
    return _claude(cfg.get("api_key", ""), prompt)

def _claude(api_key, prompt):
    if not api_key: return None
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 1800,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=40
        )
        text  = r.json()["content"][0]["text"]
        match = re.search(r"\{[\s\S]*\}", text)
        return json.loads(match.group(0)) if match else None
    except Exception:
        return None

def _gemini(api_key, prompt):
    if not api_key: return None
    tentativas = [
        ("v1beta", "gemini-2.5-flash"),
        ("v1beta", "gemini-flash-latest"),
        ("v1", "gemini-2.0-flash"),
        ("v1", "gemini-2.0-flash-lite"),
    ]
    for versao, modelo in tentativas:
        try:
            url = f"https://generativelanguage.googleapis.com/{versao}/models/{modelo}:generateContent?key={api_key}"
            r = requests.post(url,
                json={"contents": [{"parts": [{"text": prompt}]}],
                      "generationConfig": {"maxOutputTokens": 1800, "temperature": 0.3,
                                           "thinkingConfig": {"thinkingBudget": 0}}},
                timeout=60)
            if r.status_code not in (200,):
                continue
            parts = r.json()["candidates"][0]["content"]["parts"]
            text  = " ".join(p["text"] for p in parts if "text" in p)
            match = re.search(r"\{[\s\S]*\}", text)
            if match:
                return json.loads(match.group(0))
        except Exception:
            continue
    return None

# ─── Identifica nome da peça para busca secundária ────────────────────────────
def _identificar_nome_peca(codigo):
    """Pede à IA só o nome da peça para usar como query no ML."""
    cfg = carregar_config()
    provider = cfg.get("provider", "gemini")
    prompt = f"Qual o nome comercial desta peça automotiva no Brasil (código OEM: {codigo})? Responda APENAS o nome curto, ex: 'Sensor Pressão Óleo Renault'. Sem explicações."
    if provider == "gemini":
        r = _gemini(cfg.get("gemini_key", ""), prompt)
    else:
        r = _claude(cfg.get("api_key", ""), prompt)
    if isinstance(r, dict):
        v = list(r.values())[0] if r else ""
        return str(v).strip()[:60]
    return ""

# ─── Lógica principal de busca ────────────────────────────────────────────────
def executar_busca(codigo):
    titles, novos, usados = buscar_ml(codigo)
    ml_achou = bool(titles or novos or usados)
    print(f"[WRX] ML achou: titles={len(titles)} novos={len(novos)} usados={len(usados)}")

    # Se ML não achou pelo código OEM, tenta buscar pelo nome da peça
    if not ml_achou:
        nome_peca = _identificar_nome_peca(codigo)
        if nome_peca:
            print(f"[WRX] Buscando ML por nome: {nome_peca}")
            t2, n2, u2 = buscar_ml(nome_peca)
            if t2 or n2 or u2:
                titles, novos, usados = t2, n2, u2
                ml_achou = True

    prices    = novos or usados
    preco_ref = calcular_preco_sugerido(prices)
    prompt    = _build_prompt(codigo, titles, prices[:10])
    data      = _chamar_ia(prompt)

    if not data:
        return {"erro": "Falha na IA. Verifique a API Key no WRX-Search."}

    # Força preço calculado pelo Python
    if preco_ref > 0:
        data["preco_sugerido"] = preco_ref

    data = _ajustar_titulos(data, codigo)

    # Adiciona NCM/CEST se encontrar
    nome_peca = (data.get("titulos_otimizados") or [codigo])[0]
    ncm = buscar_ncm_local(nome_peca)
    if ncm:
        data["ncm"]  = ncm.get("ncm", "")
        data["cest"] = ncm.get("cest", "")
        data["ncm_desc"] = ncm.get("descricao", "")

    # Adiciona preços separados
    data["precos_novos"]  = novos
    data["precos_usados"] = usados
    data["fonte"] = "ml+ia" if ml_achou else "ia_pura"
    data["ml_titulos_encontrados"] = len(titles)

    return data

# ─── Servidor HTTP ────────────────────────────────────────────────────────────
try:
    from flask import Flask, request, jsonify
    from flask import Response
    USE_FLASK = True
except ImportError:
    USE_FLASK = False

if USE_FLASK:
    app = Flask(__name__)

    def _cors(resp):
        resp.headers["Access-Control-Allow-Origin"]  = "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Access-Control-Request-Private-Network"
        resp.headers["Access-Control-Allow-Private-Network"] = "true"
        return resp

    @app.after_request
    def after(resp):
        return _cors(resp)

    @app.route("/buscar", methods=["GET", "OPTIONS"])
    @app.route("/wrx-buscar", methods=["GET", "OPTIONS"])
    def rota_buscar():
        if request.method == "OPTIONS":
            return _cors(app.response_class(status=204))
        codigo = request.args.get("q", "").strip()
        if not codigo:
            return jsonify({"erro": "Parâmetro ?q= obrigatório"}), 400
        print(f"[WRX-API] Buscando: {codigo}")
        resultado = executar_busca(codigo)
        print(f"[WRX-API] Concluído: {codigo}")
        return jsonify(resultado)

    @app.route("/ping", methods=["GET", "OPTIONS"])
    def ping():
        if request.method == "OPTIONS":
            return _cors(app.response_class(status=204))
        return jsonify({"ok": True, "porta": PORT})

    @app.route("/debug-ml")
    def debug_ml():
        q = request.args.get("q", "sensor pressao oleo")
        resultado = {"query": q, "camadas": {}}
        # Testa API ML
        try:
            r = requests.get("https://api.mercadolibre.com/sites/MLB/search",
                             params={"q": q, "limit": 5}, timeout=15)
            resultado["camadas"]["api_ml"] = {
                "status": r.status_code,
                "total": r.json().get("paging", {}).get("total", 0) if r.status_code == 200 else 0,
                "titulos": [x.get("title") for x in r.json().get("results", [])[:3]] if r.status_code == 200 else []
            }
        except Exception as e:
            resultado["camadas"]["api_ml"] = {"erro": str(e)}
        # Testa requests HTML
        try:
            url = f"https://lista.mercadolivre.com.br/acessorios-veiculos/{q.replace(' ', '-')}"
            r2 = requests.get(url, headers={"User-Agent": _UA_ML}, timeout=15)
            resultado["camadas"]["requests_html"] = {
                "status": r2.status_code,
                "tamanho": len(r2.text),
                "tem_produtos": _has_resultados(r2.text)
            }
        except Exception as e:
            resultado["camadas"]["requests_html"] = {"erro": str(e)}
        # Testa Playwright
        try:
            from playwright.async_api import async_playwright
            resultado["camadas"]["playwright"] = {"instalado": True, "chromium_path": _chromium_exec()}
            import asyncio
            async def _pw_test():
                async with async_playwright() as p:
                    browser = await p.chromium.launch(headless=True, args=["--no-sandbox","--disable-dev-shm-usage","--disable-gpu"])
                    page = await browser.new_page()
                    await page.set_extra_http_headers({"Accept-Language": "pt-BR"})
                    url_pw = f"https://lista.mercadolivre.com.br/acessorios-veiculos/{q.replace(' ','-')}"
                    await page.goto(url_pw, timeout=30000, wait_until="domcontentloaded")
                    try:
                        await page.wait_for_selector("li.ui-search-layout__item", timeout=8000)
                        items = await page.query_selector_all("li.ui-search-layout__item")
                        html = await page.content()
                        await browser.close()
                        return len(items), len(html)
                    except Exception as e2:
                        html = await page.content()
                        await browser.close()
                        return 0, len(html)
            nitens, tam = asyncio.run(_pw_test())
            resultado["camadas"]["playwright"]["itens_encontrados"] = nitens
            resultado["camadas"]["playwright"]["tamanho_html"] = tam
        except ImportError:
            resultado["camadas"]["playwright"] = {"instalado": False}
        except Exception as e:
            resultado["camadas"]["playwright"]["erro"] = str(e)
        return jsonify(resultado)

    @app.route("/")
    @app.route("/criar-anuncio.html")
    def rota_form():
        """Serve o formulário localmente (evita Mixed Content HTTPS→HTTP)."""
        candidatos = [
            r"C:\Users\Geisuane\Desktop\criação de sait\criar-anuncio.html",
            os.path.join(_DIR, "..", "..", "..", "Desktop", "criação de sait", "criar-anuncio.html"),
        ]
        for p in candidatos:
            p = os.path.normpath(p)
            if os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    return f.read(), 200, {"Content-Type": "text/html; charset=utf-8"}
        return "Formulário não encontrado. Verifique o caminho do criar-anuncio.html.", 404

    @app.route("/carros")
    def rota_carros():
        """Busca carros — banco local com fallback Bing quando sem foto."""
        import urllib.parse
        q = request.args.get("q", "").strip().lower()
        try:
            with open(DB_FILE, encoding="utf-8") as f:
                db = json.load(f)
        except Exception:
            return jsonify([])

        # Parse da query uma única vez
        palavras = [p for p in (q.split() if q else []) if p != "a"]
        q_anos   = [int(p) for p in palavras if p.isdigit() and 1990 <= int(p) <= 2030]
        q_texto  = [p for p in palavras if not (p.isdigit() and 1990 <= int(p) <= 2030)]

        def _bing_url(nome):
            bq = urllib.parse.quote(nome + " carro brasil fundo branco")
            return f"https://tse1.mm.bing.net/th?q={bq}&w=640&h=360&c=7&rs=1&p=0&o=5&dpr=1&pid=1.7"

        results = []
        for slug, data in db.items():
            veiculo = data.get("veiculo", "")
            anos    = data.get("anos", "")
            texto   = veiculo.lower()
            anos_lista = [int(a) for a in anos.split() if a.isdigit()]

            # TODAS as palavras de texto devem estar no veiculo ou slug
            if q_texto and not all(p in texto or p in slug for p in q_texto):
                continue

            # Busca com 2+ palavras → quantidade de palavras do veiculo deve bater exatamente
            # "fiat uno" (2) não bate em "Fiat Novo Uno" (3) nem "Fiat Uno Vivace" (3)
            if len(q_texto) >= 2:
                if len([w for w in texto.strip().split() if w]) != len(q_texto):
                    continue

            # Se informou anos, o carro deve ter anos no intervalo
            if q_anos and anos_lista:
                ano_min, ano_max = min(q_anos), max(q_anos)
                if not any(ano_min <= a <= ano_max for a in anos_lista):
                    continue

            # Resolve foto local
            fotos = data.get("fotos", [])
            idx   = data.get("selecionada", 0)
            foto_path = ""
            if fotos:
                foto_path = fotos[min(idx, len(fotos)-1)]
            elif data.get("foto"):
                foto_path = data["foto"]
            if not foto_path or not os.path.exists(foto_path):
                for sufixo in (slug + "_0.png", slug + ".png"):
                    c = os.path.join(FOTOS_DIR, sufixo)
                    if os.path.exists(c):
                        foto_path = c
                        break

            if foto_path and os.path.exists(foto_path):
                filename = os.path.basename(foto_path)
                foto_url = f"http://127.0.0.1:{PORT}/foto/{filename}"
            else:
                # Fallback internet: Bing thumbnail
                foto_url = _bing_url(veiculo)

            results.append({"veiculo": veiculo, "anos": anos, "slug": slug, "foto_url": foto_url})

        # Se nada encontrado no banco e há busca textual → resultado sintético via Bing
        if not results and q_texto:
            nome_sint = " ".join(t.capitalize() for t in q_texto)
            anos_sint = " ".join(str(a) for a in sorted(set(q_anos))) if q_anos else ""
            results.append({
                "veiculo": nome_sint,
                "anos":    anos_sint,
                "slug":    "_".join(q_texto),
                "foto_url": _bing_url(nome_sint),
            })

        return jsonify(results)

    @app.route("/remover-fundo", methods=["POST", "OPTIONS"])
    def rota_remover_fundo():
        if request.method == "OPTIONS":
            return Response(status=204, headers={"Access-Control-Allow-Origin":"*","Access-Control-Allow-Methods":"POST,OPTIONS","Access-Control-Allow-Headers":"Content-Type"})
        import base64, io
        try:
            data     = request.get_json(force=True)
            img_b64  = data.get("imagem", "")
            if not img_b64:
                return jsonify({"erro": "Campo 'imagem' obrigatório"}), 400
            img_bytes = base64.b64decode(img_b64)
            resultado = None
            # 1. rembg direto
            try:
                import rembg
                resultado = rembg.remove(img_bytes)
            except Exception:
                pass
            # 2. rembg via subprocess Python 3.12
            if not resultado:
                for py_path in [
                    r"C:\Users\cauav\AppData\Local\Programs\Python\Python312\python.exe",
                    r"C:\Users\Geisuane\AppData\Local\Programs\Python\Python312\python.exe",
                ]:
                    if os.path.exists(py_path):
                        try:
                            script = "import sys,rembg; sys.stdout.buffer.write(rembg.remove(sys.stdin.buffer.read()))"
                            r = subprocess.run([py_path, "-c", script], input=img_bytes,
                                               capture_output=True, timeout=40,
                                               creationflags=0x08000000)
                            if r.returncode == 0 and r.stdout:
                                resultado = r.stdout
                                break
                        except Exception:
                            pass
            # 3. OpenCV GrabCut fallback
            if not resultado:
                try:
                    import cv2, numpy as np
                    from PIL import Image
                    img_pil = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
                    img_cv  = cv2.cvtColor(np.array(img_pil.convert("RGB")), cv2.COLOR_RGB2BGR)
                    h, w    = img_cv.shape[:2]
                    scale   = min(1200/w, 1540/h, 1.0)
                    rsz     = cv2.resize(img_cv, (int(w*scale), int(h*scale))) if scale < 1 else img_cv
                    mask    = np.zeros(rsz.shape[:2], np.uint8)
                    bgd, fgd = np.zeros((1,65), np.float64), np.zeros((1,65), np.float64)
                    rect    = (5, 5, rsz.shape[1]-10, rsz.shape[0]-10)
                    cv2.grabCut(rsz, mask, rect, bgd, fgd, 5, cv2.GC_INIT_WITH_RECT)
                    mask2   = np.where((mask==2)|(mask==0), 0, 255).astype('uint8')
                    if scale < 1:
                        mask2 = cv2.resize(mask2, (w, h))
                    rgba    = np.array(img_pil)
                    rgba[:,:,3] = mask2
                    buf = io.BytesIO()
                    Image.fromarray(rgba).save(buf, format="PNG")
                    resultado = buf.getvalue()
                except Exception:
                    pass
            if not resultado:
                return jsonify({"erro": "Instale rembg: pip install rembg"}), 500
            return jsonify({"png": base64.b64encode(resultado).decode()})
        except Exception as e:
            return jsonify({"erro": str(e)}), 500

    @app.route("/foto/<filename>")
    def rota_foto(filename):
        """Serve imagens de carros para o Desmonte X."""
        filepath = os.path.join(FOTOS_DIR, filename)
        if not os.path.exists(filepath):
            return "not found", 404
        ext = filename.rsplit(".", 1)[-1].lower()
        mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}.get(ext, "image/png")
        with open(filepath, "rb") as f:
            data = f.read()
        return Response(data, mimetype=mime, headers={"Access-Control-Allow-Origin": "*"})

    @app.route("/ml-precos", methods=["GET", "OPTIONS"])
    def rota_ml_precos():
        if request.method == "OPTIONS":
            return _cors(app.response_class(status=204))
        query = request.args.get("q", "").strip()
        nome = request.args.get("nome", "").strip()
        if not query:
            return jsonify({"erro": "Parâmetro ?q= obrigatório"}), 400
        token = _get_ml_token()
        headers = {"Accept": "application/json", "Accept-Language": "pt-BR,pt;q=0.9"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        def _buscar_usados(termo):
            resultados = []
            for offset in [0, 48, 96]:
                try:
                    r = requests.get(
                        "https://api.mercadolibre.com/sites/MLB/search",
                        params={"q": termo, "condition": "used", "limit": 48, "offset": offset},
                        headers=headers, timeout=15
                    )
                    if r.status_code != 200:
                        break
                    items = r.json().get("results", [])
                    if not items:
                        break
                    for item in items:
                        qty = int(item.get("available_quantity") or 99)
                        if qty > 2:
                            continue
                        price = float(item.get("price") or 0)
                        if price <= 5:
                            continue
                        resultados.append({
                            "price": price,
                            "qty": qty,
                            "title": item.get("title", "")[:60],
                            "link": item.get("permalink", "")
                        })
                except Exception:
                    break
            return resultados

        usados = _buscar_usados(query)
        # Se não achou nada com OEM/código, tenta com nome da peça
        if not usados and nome:
            usados = _buscar_usados(nome)
        usados.sort(key=lambda x: x["price"])
        return jsonify({"usados": usados[:10], "novos": [], "total": len(usados), "termo_usado": query if usados else (nome or query)})

    @app.route("/", methods=["OPTIONS"])
    @app.route("/buscar", methods=["OPTIONS"])
    @app.route("/wrx-buscar", methods=["OPTIONS"])
    @app.route("/carros", methods=["OPTIONS"])
    @app.route("/ml-precos", methods=["OPTIONS"])
    def options():
        return Response(status=204, headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        })

    def main():
        sys.stdout.reconfigure(encoding="utf-8", errors="replace") if hasattr(sys.stdout, "reconfigure") else None
        print(f"WRX-Search API Server - porta {PORT}")
        print(f"  http://localhost:{PORT}/buscar?q=CODIGO")
        print(f"  http://localhost:{PORT}/carros?q=fiat+uno")
        print(f"  http://localhost:{PORT}/ping")
        host = "0.0.0.0" if _IS_RAILWAY else "127.0.0.1"
        app.run(host=host, port=PORT, debug=False, threaded=True)

else:
    # Fallback: stdlib http.server sem Flask
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import urlparse, parse_qs

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            print(f"[WRX-API] {fmt % args}")

        def _send_cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

        def do_OPTIONS(self):
            self.send_response(204)
            self._send_cors()
            self.end_headers()

        def do_GET(self):
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            q = params.get("q", [""])[0].strip()

            if parsed.path == "/ping":
                body = json.dumps({"ok": True}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self._send_cors()
                self.end_headers()
                self.wfile.write(body)
                return

            if parsed.path != "/buscar" or not q:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self._send_cors()
                self.end_headers()
                self.wfile.write(json.dumps({"erro": "Use /buscar?q=CODIGO"}).encode())
                return

            print(f"[WRX-API] Buscando: {q}")
            resultado = executar_busca(q)
            body = json.dumps(resultado, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self._send_cors()
            self.end_headers()
            self.wfile.write(body)

    def main():
        print(f"WRX-Search API — porta {PORT}")
        print(f"  (Flask não instalado — usando stdlib http.server)")
        print(f"  GET http://localhost:{PORT}/buscar?q=CODIGO")
        host = "0.0.0.0" if _IS_RAILWAY else "127.0.0.1"
        server = HTTPServer((host, PORT), Handler)
        server.serve_forever()

if __name__ == "__main__":
    main()
