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

# Instala Chromium automaticamente no Railway se não existir
def _ensure_playwright_chromium():
    if os.name != "posix":
        return
    try:
        import playwright
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium", "--with-deps"],
            capture_output=True, timeout=120
        )
        if result.returncode == 0:
            print("[STARTUP] Playwright Chromium instalado/verificado com sucesso")
        else:
            print(f"[STARTUP] playwright install chromium saiu com código {result.returncode}")
    except Exception as e:
        print(f"[STARTUP] playwright install chromium erro: {e}")

if os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"):
    threading.Thread(target=_ensure_playwright_chromium, daemon=True).start()

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
ML_CLIENT_ID     = os.environ.get("ML_CLIENT_ID",     "5450531514024470")
ML_CLIENT_SECRET = os.environ.get("ML_CLIENT_SECRET", "s9gn1wlLSuHv2JlDbKnhoJYRQziI7YTu")

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
        "ui-search-layout__item", "ui-search-result", "andes-money-amount__fraction",
        "poly-card", "poly-component__title", "resultados",
    ])

def _parse_html_ml(html):
    from bs4 import BeautifulSoup
    titles, novos, usados = [], [], []
    soup  = BeautifulSoup(html, "html.parser")
    items = (soup.select("li.ui-search-layout__item") or
             soup.select("div.ui-search-result") or
             soup.select(".poly-card") or
             soup.select("[data-item-id]"))
    for item in items:
        t_tag = (item.find(class_="ui-search-item__title") or
                 item.find(class_=re.compile(r"poly-component__title|title|item__title|name")))
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

def _buscar_api_ml_detalhado(codigo):
    """Busca via ML API com atributos completos por item."""
    items_detalhados = []
    token = _get_ml_token()
    headers = {"Accept": "application/json", "Accept-Language": "pt-BR,pt;q=0.9"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = requests.get(
            "https://api.mercadolibre.com/sites/MLB/search",
            params={"q": codigo, "limit": 20},
            headers=headers, timeout=20
        )
        if r.status_code != 200:
            return []
        for item in r.json().get("results", []):
            attrs = {}
            for a in item.get("attributes", []):
                nome = (a.get("name") or "").lower().strip()
                valor = a.get("value_name") or ""
                if nome and valor:
                    if nome in attrs:
                        attrs[nome] = attrs[nome] + ", " + valor
                    else:
                        attrs[nome] = valor
            items_detalhados.append({
                "id": item.get("id", ""),
                "titulo": item.get("title", ""),
                "preco": float(item.get("price") or 0),
                "condicao": item.get("condition", "new"),
                "permalink": item.get("permalink", ""),
                "atributos": attrs,
            })
    except Exception as e:
        print(f"[ML-API-DET] Erro: {e}")
    return items_detalhados


def _buscar_item_completo_api(item_id):
    """Busca atributos completos + descrição de um item pelo ID."""
    token = _get_ml_token()
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resultado = {"id": item_id, "atributos": {}, "descricao": "", "titulo": "", "preco": 0}
    try:
        r = requests.get(f"https://api.mercadolibre.com/items/{item_id}", headers=headers, timeout=15)
        if r.status_code == 200:
            data = r.json()
            attrs = {}
            for a in data.get("attributes", []):
                nome = (a.get("name") or "").lower().strip()
                # Agrega múltiplos valores do mesmo atributo
                valores = [v.get("name", "") for v in a.get("values", []) if v.get("name")]
                if not valores and a.get("value_name"):
                    valores = [a["value_name"]]
                if nome and valores:
                    attrs[nome] = ", ".join(filter(None, valores))
            resultado["atributos"] = attrs
            resultado["titulo"] = data.get("title", "")
            resultado["preco"] = float(data.get("price") or 0)
            resultado["condicao"] = data.get("condition", "new")
    except Exception as e:
        print(f"[ML-API-ITEM] Erro {item_id}: {e}")
    try:
        r2 = requests.get(f"https://api.mercadolibre.com/items/{item_id}/description", headers=headers, timeout=10)
        if r2.status_code == 200:
            resultado["descricao"] = (r2.json().get("plain_text") or "")[:3000]
    except Exception:
        pass
    return resultado


def _normalizar_chave_attr(chave):
    """Normaliza nomes de atributos ML para chaves canônicas."""
    c = chave.lower().strip()
    mapa = {
        "marca do veículo compatível": "marca", "marca do veiculo compativel": "marca",
        "marca do veículo": "marca", "marca compatível": "marca", "marca": "marca",
        "modelo do veículo compatível": "modelo", "modelo do veiculo compativel": "modelo",
        "modelo": "modelo", "modelo do veículo": "modelo",
        "ano do veículo": "ano", "ano de fabricação": "ano", "anos compatíveis": "ano",
        "ano": "ano", "year": "ano",
        "motor": "motor", "motor compatível": "motor", "tipo de motor": "motor",
        "cilindrada": "motor",
        "código oem": "oem", "código de peça": "oem", "número de peça": "oem",
        "part number": "oem", "número oem": "oem", "referência oem": "oem",
        "código da peça": "oem", "cod. oem": "oem",
        "lado": "lado", "lado do veículo": "lado", "lado da instalação": "lado",
        "posição": "posicao", "posição no veículo": "posicao", "posição de instalação": "posicao",
        "tipo de peça": "tipo", "tipo": "tipo", "categoria": "tipo",
    }
    return mapa.get(c, c)


def _extrair_urls_da_lista_html(html):
    """Extrai URLs de anúncios do HTML da página de lista do ML."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    urls = []
    vistos = set()
    # Seletores em ordem de prioridade — inclui novo layout poly-card do ML (2024+)
    for sel in [
        ".poly-card a",
        ".poly-component__title",
        "li.ui-search-layout__item a.ui-search-item__image-link",
        "li.ui-search-layout__item a[href*='MLB']",
        ".ui-search-result a[href*='MLB']",
        "a[href*='mercadolivre.com.br/']",
    ]:
        for a in soup.select(sel):
            href = a.get("href", "")
            if not href:
                continue
            href = href.split("?")[0].split("#")[0]
            # Aceita MLB e MLBU (novo formato de URL)
            if "mercadolivre.com.br" in href and re.search(r"MLB[A-Z]?\d+", href):
                if href not in vistos:
                    vistos.add(href)
                    urls.append(href)
        if len(urls) >= 5:
            break
    return urls[:8]


def _scrape_paginas_anuncio_playwright(urls):
    """Abre páginas individuais de anúncio e retorna lista de {url, html}."""
    try:
        import asyncio
        from playwright.async_api import async_playwright
        exec_path = _chromium_exec()
        async def _run():
            resultados = []
            async with async_playwright() as p:
                launch_args = dict(headless=True, args=[
                    "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                    "--disable-setuid-sandbox", "--disable-blink-features=AutomationControlled",
                ])
                if exec_path:
                    launch_args["executable_path"] = exec_path
                browser = await p.chromium.launch(**launch_args)
                ctx = await browser.new_context(
                    locale="pt-BR", user_agent=_UA_ML,
                    viewport={"width": 1280, "height": 900}, java_script_enabled=True,
                )
                await ctx.add_init_script(_STEALTH_JS)
                page = await ctx.new_page()
                for url in urls[:5]:
                    try:
                        await page.goto(url, timeout=30000, wait_until="domcontentloaded")
                        try:
                            await page.wait_for_selector(
                                "h1.ui-pdp-title, .ui-pdp-title, [class*='pdp-title'], .andes-table",
                                timeout=10000
                            )
                        except Exception:
                            pass
                        html = await page.content()
                        if len(html) > 5000:
                            resultados.append({"url": url, "html": html})
                            print(f"[PW-PDN] OK {url[:60]} → {len(html)} bytes")
                    except Exception as e:
                        print(f"[PW-PDN] Erro {url[:60]}: {e}")
                await browser.close()
            return resultados
        return asyncio.run(_run())
    except Exception as e:
        print(f"[PW-PDN] Exceção: {e}")
        return []


def _parse_pagina_anuncio(html, url=""):
    """Extrai dados estruturados de uma página de anúncio ML."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    titulo = ""
    for sel in ["h1.ui-pdp-title", ".ui-pdp-title", "h1[class*='title']", "h1"]:
        t = soup.select_one(sel)
        if t:
            titulo = t.get_text(strip=True)
            break
    atributos = {}
    # Formato andes-table
    for row in soup.select("table.andes-table tr, .andes-table__row"):
        cells = row.select("th, td, .andes-table__column")
        if len(cells) >= 2:
            k = cells[0].get_text(strip=True)
            v = cells[1].get_text(strip=True)
            if k and v:
                atributos[k.lower()] = v
    # Formato specs list
    for item in soup.select(".ui-pdp-specs__item, [class*='specs__item']"):
        label = item.select_one("[class*='label'], [class*='key'], dt")
        value = item.select_one("[class*='value'], [class*='val'], dd")
        if label and value:
            atributos[label.get_text(strip=True).lower()] = value.get_text(strip=True)
    # Formato dl
    for dl in soup.select("dl"):
        for dt, dd in zip(dl.find_all("dt"), dl.find_all("dd")):
            k = dt.get_text(strip=True).lower()
            v = dd.get_text(strip=True)
            if k and v and k not in atributos:
                atributos[k] = v
    # Descrição
    descricao = ""
    for sel in [".ui-pdp-description__content", "[class*='description__content']", "#description"]:
        d = soup.select_one(sel)
        if d:
            descricao = d.get_text(separator="\n", strip=True)[:2000]
            break
    return {"titulo": titulo, "atributos": atributos, "descricao": descricao, "url": url}


def _consolidar_e_score(anuncios, codigo_oem):
    """
    Cruza informações de múltiplos anúncios e cria score de compatibilidade.
    Só aprova compatibilidades consistentes (aparece em >= 2 anúncios OU único disponível).
    """
    from collections import Counter, defaultdict
    if not anuncios:
        return None
    compat_counter = Counter()
    compat_anos = defaultdict(set)
    compat_motor = {}
    lado_counter = Counter()
    posicao_counter = Counter()
    tipo_counter = Counter()
    precos = []
    titulos = []
    for anuncio in anuncios:
        attrs_raw = anuncio.get("atributos", {})
        titulo = anuncio.get("titulo", "")
        if titulo:
            titulos.append(titulo)
        preco = anuncio.get("preco", 0)
        if preco > 0:
            precos.append(preco)
        # Normaliza atributos
        norm = {}
        for k, v in attrs_raw.items():
            norm[_normalizar_chave_attr(k)] = v
        marca  = norm.get("marca", "").strip()
        modelo = norm.get("modelo", "").strip()
        motor  = norm.get("motor", "").strip()
        ano_raw = norm.get("ano", "")
        lado   = norm.get("lado", "").strip()
        posicao = norm.get("posicao", "").strip()
        tipo   = norm.get("tipo", "").strip()
        if lado:
            lado_counter[lado] += 1
        if posicao:
            posicao_counter[posicao] += 1
        if tipo:
            tipo_counter[tipo] += 1
        # Extrai anos
        anos = set()
        faixa_m = re.search(r'\b(20\d{2}|19\d{2})\s*(?:[aA]|\/|-)\s*(20\d{2}|19\d{2})\b', ano_raw)
        if faixa_m:
            ini, fim = int(faixa_m.group(1)), int(faixa_m.group(2))
            for a in range(ini, min(fim + 1, ini + 20)):
                if 1990 <= a <= 2035:
                    anos.add(str(a))
        else:
            for m in re.finditer(r'\b(20\d{2}|19\d{2})\b', ano_raw):
                a = int(m.group(1))
                if 1990 <= a <= 2035:
                    anos.add(str(a))
        if marca or modelo:
            # Separa múltiplos modelos se a string tiver vírgula
            modelos_lista = [m.strip() for m in modelo.split(",") if m.strip()] or [""]
            marcas_lista  = [m.strip() for m in marca.split(",") if m.strip()] or [""]
            for marc in marcas_lista:
                for mod in modelos_lista:
                    key = (marc.lower(), mod.lower())
                    compat_counter[key] += 1
                    compat_anos[key].update(anos)
                    if motor and key not in compat_motor:
                        compat_motor[key] = motor
        elif titulo:
            # Fallback: extrai do título
            compat_titulo = _extrair_compatibilidade_dos_titulos([titulo], codigo_oem)
            for c in compat_titulo:
                key = (c["veiculo"].lower(), "")
                compat_counter[key] += 1
                compat_anos[key].update(c["anos"].split())
    # Monta lista final — inclui se aparece em >= 1 anúncio (score ponderado depois)
    total = max(len(anuncios), 1)
    compat_final = []
    for key, count in compat_counter.most_common():
        marc_k, mod_k = key
        if not marc_k:
            continue
        partes = [marc_k.title()]
        if mod_k:
            partes.append(mod_k.title())
        veiculo_nome = " ".join(partes)
        motor_val = compat_motor.get(key, "")
        anos_sorted = sorted(compat_anos[key])
        compat_final.append({
            "veiculo": veiculo_nome,
            "motor": motor_val,
            "anos": " ".join(anos_sorted),
            "detalhes": motor_val,
            "ocorrencias": count,
            "confianca": round(count / total, 2),
        })
    return {
        "compatibilidade": compat_final,
        "lado": lado_counter.most_common(1)[0][0] if lado_counter else "",
        "posicao": posicao_counter.most_common(1)[0][0] if posicao_counter else "",
        "tipo_peca": tipo_counter.most_common(1)[0][0] if tipo_counter else "",
        "precos": sorted(set(round(p, 2) for p in precos))[:15],
        "titulos_ml": titulos[:10],
        "n_anuncios": total,
    }


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

_STEALTH_JS = """
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    Object.defineProperty(navigator, 'plugins', {get: () => [
        {filename: 'internal-pdf-viewer', description: 'Portable Document Format'},
        {filename: 'internal-nacl-plugin', description: 'Native Client'},
    ]});
    Object.defineProperty(navigator, 'languages', {get: () => ['pt-BR', 'pt', 'en-US', 'en']});
    window.chrome = {runtime: {}, loadTimes: function(){}, csi: function(){}, app: {}};
    try {
        const orig = window.navigator.permissions.query;
        window.navigator.permissions.query = (p) =>
            p.name === 'notifications'
                ? Promise.resolve({state: Notification.permission})
                : orig(p);
    } catch(e) {}
"""

def _buscar_playwright_python(urls):
    try:
        import asyncio
        from playwright.async_api import async_playwright
        exec_path = _chromium_exec()

        async def _dismiss_cookies(page):
            seletores = [
                "button:has-text('Aceitar cookies')",
                "button[data-testid='action:understood-button']",
                "button:has-text('Entendi')",
                "[class*='cookie'] button",
            ]
            for sel in seletores:
                try:
                    btn = await page.query_selector(sel)
                    if btn and await btn.is_visible():
                        await btn.click()
                        print(f"[PW] Banner cookies fechado: {sel}")
                        await page.wait_for_timeout(800)
                        return True
                except Exception:
                    continue
            print("[PW] Banner de cookies não encontrado")
            return False

        async def _run():
            async with async_playwright() as p:
                launch_args = dict(
                    headless=True,
                    args=[
                        "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                        "--disable-setuid-sandbox", "--disable-blink-features=AutomationControlled",
                        "--window-size=1280,800",
                    ]
                )
                if exec_path:
                    launch_args["executable_path"] = exec_path
                browser = await p.chromium.launch(**launch_args)
                ctx = await browser.new_context(
                    locale="pt-BR",
                    user_agent=_UA_ML,
                    viewport={"width": 1280, "height": 800},
                    java_script_enabled=True,
                )
                await ctx.add_init_script(_STEALTH_JS)
                page = await ctx.new_page()

                for url in urls:
                    try:
                        t0 = __import__("time").time()
                        print(f"[PW] Navegando: {url}")
                        await page.goto(url, timeout=35000, wait_until="domcontentloaded")
                        final_url = page.url
                        print(f"[PW] URL final: {final_url}")

                        html_inicial = await page.content()
                        print(f"[PW] HTML após domcontentloaded: {len(html_inicial)} bytes")

                        await _dismiss_cookies(page)

                        print("[PW] Aguardando resultados...")
                        try:
                            await page.wait_for_selector(
                                "li.ui-search-layout__item, div.ui-search-result, .poly-card, .poly-component__title",
                                timeout=18000
                            )
                            print("[PW] Seletor encontrado")
                        except Exception as e_sel:
                            print(f"[PW] Seletor não encontrado após 18s: {e_sel}")
                            html_vazio = await page.content()
                            print(f"[PW] HTML após timeout: {len(html_vazio)} bytes")
                            try:
                                ss_path = os.path.join(_DIR, f"pw_debug_{int(__import__('time').time())}.png")
                                await page.screenshot(path=ss_path, full_page=False)
                                print(f"[PW] Screenshot salvo: {ss_path}")
                            except Exception as e_ss:
                                print(f"[PW] Screenshot falhou: {e_ss}")
                            continue

                        html = await page.content()
                        elapsed = round(__import__("time").time() - t0, 2)
                        n_items = html.count("ui-search-layout__item")
                        print(f"[PW] HTML final: {len(html)} bytes | itens: {n_items} | tempo: {elapsed}s")

                        await browser.close()
                        if _has_resultados(html):
                            return [html]
                        print("[PW] _has_resultados=False com seletor presente")
                    except Exception as e:
                        print(f"[PW] Erro em {url}: {e}")
                        continue

                print("[PW] Nenhuma URL retornou resultados")
                await browser.close()
            return []

        return asyncio.run(_run())
    except Exception as e:
        print(f"[PW] Exceção geral: {e}")
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

# ─── Extrai nome da peça a partir de títulos ML que contêm o OEM exato ────────
_MARCAS_VEICULOS = re.compile(
    r'\b(renault|peugeot|citroen|volkswagen|vw|fiat|chevrolet|gm|toyota|honda|ford|hyundai|'
    r'nissan|mitsubishi|jeep|dodge|ram|kia|bmw|mercedes|benz|mb|audi|volvo|suzuki|subaru|'
    r'lifan|caoa|chery|jac|haval|great wall|byd|geely|mg|troller|'
    r'logan|sandero|duster|clio|kangoo|megane|fluence|captur|kwid|oroch|'
    r'uno|palio|siena|strada|punto|bravo|toro|cronos|argo|pulse|fastback|mobi|'
    r'onix|prisma|cobalt|spin|s10|tracker|montana|trailblazer|cruze|equinox|'
    r'gol|polo|voyage|fox|up|saveiro|amarok|tiguan|t-cross|virtus|nivus|taos|jetta|passat|'
    r'ka|ecosport|fiesta|focus|ranger|transit|territory|bronco|mustang|fusion|'
    r'hb20|ix35|creta|tucson|santa fe|azera|elantra|sonata|veracruz|'
    r'march|versa|kicks|frontier|livina|sentra|x-trail|murano|'
    r'corolla|hilux|yaris|etios|sw4|camry|rav4|land cruiser|prado|'
    r'city|civic|fit|hr-v|cr-v|wr-v|accord|pilot|'
    r'renegade|compass|commander|wrangler|grand cherokee|cherokee|'
    r'c3|c4|c5|berlingo|jumper|207|208|308|408|3008|5008|expert|'
    r'duster|master|traffic|1\.0|1\.3|1\.6|2\.0|2\.5|diesel|flex|turbo|automático|manual|cvt|'
    r'\d{4}/\d{4}|\d{4})\b',
    re.IGNORECASE
)

def _extrair_nome_oem_do_titulo(titulo, codigo):
    """Extrai o nome da peça de um título ML que contém o OEM.
    Ex: 'Caixa Filtro Ar Renault Duster 8200420871b' → 'Caixa Filtro Ar'"""
    # Remove o código OEM do título
    t = re.sub(re.escape(codigo), '', titulo, flags=re.IGNORECASE).strip()
    # Quebra nas palavras antes da primeira marca/modelo/ano
    m = _MARCAS_VEICULOS.search(t)
    nome = t[:m.start()].strip() if m else t
    # Remove caracteres especiais do final
    nome = re.sub(r'[\-–—/|\\]+$', '', nome).strip()
    # Remove palavras genéricas soltas
    nome = re.sub(r'\b(original|oem|genuina|genuíno|novo|nova|par)\b', '', nome, flags=re.IGNORECASE).strip()
    nome = re.sub(r'\s+', ' ', nome).strip()
    return nome if len(nome) > 3 else titulo.split()[0] if titulo else ''

def _titulos_com_oem(titles, codigo):
    """Filtra títulos ML que contêm o código OEM exato."""
    cod = codigo.lower().strip()
    return [t for t in titles if cod in t.lower()]


def _extrair_compatibilidade_dos_titulos(titulos, codigo):
    """
    Extrai compatibilidade de veículos diretamente dos títulos ML raspados.
    Agrupa por veículo e expande faixas de anos.
    Retorna lista de dicts: [{"veiculo": "...", "anos": "2021 2022 2023", "detalhes": ""}]
    """
    _FAIXA = re.compile(
        r'\b(20\d{2}|19\d{2})\s*(?:[aA]|\/|-)\s*(20\d{2}|19\d{2})\b'
    )
    _ANO   = re.compile(r'\b(20\d{2}|19\d{2})\b')
    _LIXO  = re.compile(
        r'\b(original|oem|genuina|genuíno|genuino|novo|nova|par|código|codigo|code|peça|peca)\b',
        re.IGNORECASE
    )

    compat_map = {}  # key(lower) → {veiculo, anos: set}

    for titulo in titulos:
        # Remove o OEM do título
        t = re.sub(re.escape(codigo), '', titulo, flags=re.IGNORECASE)
        t = re.sub(r'\s+', ' ', t).strip()

        # Localiza onde a marca/modelo começa
        m_marca = _MARCAS_VEICULOS.search(t)
        if not m_marca:
            continue

        trecho = t[m_marca.start():]  # "Jeep Compass Renegade 1.3 Turbo 2021 A 2024"

        # Coleta anos (faixa tem prioridade)
        anos: set = set()
        for faixa in _FAIXA.finditer(trecho):
            ini, fim = int(faixa.group(1)), int(faixa.group(2))
            for a in range(ini, min(fim, ini + 15) + 1):
                if 1990 <= a <= 2035:
                    anos.add(str(a))
        if not anos:
            for m in _ANO.finditer(trecho):
                a = int(m.group(1))
                if 1990 <= a <= 2035:
                    anos.add(str(a))

        if not anos:
            continue

        # Isola o nome do veículo: remove anos, faixas e palavras lixo
        veic = _FAIXA.sub('', trecho)
        veic = _ANO.sub('', veic)
        veic = _LIXO.sub('', veic)
        veic = re.sub(r'[\-–/|\\,;:.]+$', '', veic.strip())
        veic = re.sub(r'\s+', ' ', veic).strip()

        if len(veic) < 4:
            continue

        key = veic.lower()
        if key not in compat_map:
            compat_map[key] = {'veiculo': veic, 'anos': set()}
        compat_map[key]['anos'].update(anos)

    result = []
    for entry in sorted(compat_map.values(), key=lambda x: x['veiculo']):
        anos_sorted = sorted(entry['anos'])
        if anos_sorted:
            result.append({
                "veiculo": entry['veiculo'],
                "anos": " ".join(anos_sorted),
                "detalhes": ""
            })

    return result


# ─── IA: prompt + ajuste + chamada ────────────────────────────────────────────
def _build_prompt(codigo, titles, prices, compatibilidade_oem=None,
                  nome_peca_confirmado=None):
    tlist = "\n".join(f"- {t}" for t in titles) if titles else "(nenhum anúncio coletado)"
    plist = ", ".join(f"R$ {p:.2f}" for p in prices) if prices else "(sem preços coletados — estime com base em concorrentes reais)"

    if compatibilidade_oem:
        # Monta lista de veículos confirmados para uso nos exemplos
        veiculos_lista = []
        for c in compatibilidade_oem:
            v = c.get('veiculo', '?')
            a = c.get('anos', '')
            anos_list = a.split() if a else []
            ano_ini = anos_list[0] if anos_list else ''
            ano_fim = anos_list[-1] if anos_list else ''
            faixa = f"{ano_ini}/{ano_fim}" if ano_ini and ano_fim and ano_ini != ano_fim else ano_ini
            veiculos_lista.append({'veiculo': v, 'anos': a, 'faixa': faixa, 'detalhes': c.get('detalhes', '')})

        linhas_oem = "\n".join(
            f"  {i+1}. {v['veiculo']} | anos: {v['anos']}"
            for i, v in enumerate(veiculos_lista)
        )

        # Extrai modelos únicos para exemplos de títulos
        modelos_unicos = list(dict.fromkeys(
            v['veiculo'].split()[1] if len(v['veiculo'].split()) > 1 else v['veiculo']
            for v in veiculos_lista
        ))
        marcas_unicas = list(dict.fromkeys(v['veiculo'].split()[0] for v in veiculos_lista))
        marca_ex = marcas_unicas[0] if marcas_unicas else 'Marca'
        modelo_ex1 = modelos_unicos[0] if modelos_unicos else 'Modelo'
        modelo_ex2 = modelos_unicos[1] if len(modelos_unicos) > 1 else modelo_ex1
        faixa_ex = veiculos_lista[0]['faixa'] if veiculos_lista else '2021/2024'
        nome_ex = nome_peca_confirmado or 'Peça OEM'

        # Monta bullets expandidos por ano (um por linha) para descricao_completa
        import re as _re
        bullets_grupos = []
        for v in veiculos_lista:
            anos_indiv = [a for a in v['anos'].split() if _re.match(r'^(19|20)\d{2}$', a)]
            if anos_indiv:
                grupo = "\n".join(f"• {v['veiculo']} {ano}" for ano in anos_indiv)
            else:
                grupo = f"• {v['veiculo']} {v['faixa']}"
            bullets_grupos.append(grupo)
        bullets_desc = "\n\n".join(bullets_grupos)  # linha em branco entre veículos diferentes

        bloco_compat = (
            "╔══════════════════════════════════════════╗\n"
            "║  COMPATIBILIDADE EXTRAÍDA DA RASPAGEM ML ║\n"
            "╚══════════════════════════════════════════╝\n\n"
            "Fonte: títulos reais coletados do Mercado Livre via scraping.\n"
            "Estes veículos aparecem NOS PRÓPRIOS ANÚNCIOS do OEM — são dados reais, não inferência.\n\n"
            f"{linhas_oem}\n\n"
            "REGRA ABSOLUTA: use SOMENTE estes veículos em 'compatibilidades_confirmadas'.\n"
            "PROIBIDO adicionar qualquer outro veículo — mesmo que pareça óbvio ou relacionado.\n"
        )

        regra_compat_json = (
            "- compatibilidades_confirmadas: SOMENTE os veículos da lista OEM acima, sem adicionar nem remover"
        )

        regra_titulos = f"""
REGRAS OBRIGATÓRIAS DE TÍTULO (compatibilidade OEM confirmada):

━━━ PROIBIDO ━━━
✗ Títulos genéricos sem veículo: "Filtro De Ar Original Motor Performance Premium"
✗ Adjetivos vazios sem referência de veículo: "Qualidade Superior", "Premium", "Original" sozinhos
✗ Título com apenas o código OEM
✗ Código OEM em mais de 1 dos 4 títulos

━━━ OBRIGATÓRIO ━━━
✓ Cada título DEVE conter: Nome da Peça + Marca + Modelo (da lista OEM confirmada)
✓ Com múltiplos modelos: distribuir entre os 4 títulos, cobrindo todos os veículos

━━━ EXEMPLOS COM OS DADOS DESTE OEM ━━━
CORRETO: "{nome_ex} {marca_ex} {modelo_ex1} {modelo_ex2} {faixa_ex}"
CORRETO: "{nome_ex} {marca_ex} {modelo_ex1} {faixa_ex} OEM {codigo}"
CORRETO: "{nome_ex} {marca_ex} {modelo_ex2} {faixa_ex} Original"
CORRETO: "{nome_ex} {marca_ex} {modelo_ex1} {modelo_ex2} Código {codigo}"

━━━ DISTRIBUIÇÃO DOS 4 TÍTULOS ━━━
- Título 1: nome_peca + modelo principal + faixa de anos
- Título 2: nome_peca + segundo modelo (se houver) + motor
- Título 3: nome_peca + todos os modelos abreviados + código OEM (1x)
- Título 4: nome_peca + marca + motor + faixa de anos

━━━ BULLETS PARA descricao_completa ━━━
Use exatamente esta lista de compatibilidades (uma por linha):
{bullets_desc}
"""
    else:
        bloco_compat = (
            "╔══════════════════════════════════════════╗\n"
            "║  SEM COMPATIBILIDADE OEM FORNECIDA       ║\n"
            "╚══════════════════════════════════════════╝\n\n"
            "Nenhuma compatibilidade confirmada foi recebida para este código.\n"
            "REGRA ABSOLUTA: NÃO deduza compatibilidade a partir de anúncios,\n"
            "títulos, SEO ou inferência própria.\n"
            "Retorne 'compatibilidades_confirmadas' como lista vazia.\n"
        )
        regra_compat_json = (
            "- compatibilidades_confirmadas: lista VAZIA [] quando não há confirmação OEM"
        )
        bullets_desc = "(sem compatibilidade confirmada)"
        regra_titulos = """
TÍTULOS SEM COMPATIBILIDADE CONFIRMADA:
- Use apenas: nome da peça + variações (Original, OEM, Novo, código)
- Não inventar veículos
"""

    if nome_peca_confirmado:
        bloco_nome = (
            "╔══════════════════════════════════════════╗\n"
            "║  NOME DA PEÇA CONFIRMADO POR OEM EXATO   ║\n"
            "╚══════════════════════════════════════════╝\n\n"
            f"Nome confirmado pelo Mercado Livre via correspondência OEM exata:\n"
            f"  → {nome_peca_confirmado}\n\n"
            "REGRA ABSOLUTA: use ESTE nome no campo 'nome_peca'. NÃO altere, NÃO substitua.\n"
            "Todos os títulos devem usar este nome como base.\n"
        )
    else:
        bloco_nome = (
            "ETAPA 1 — IDENTIFICAÇÃO DA PEÇA\n"
            "Use o código OEM para identificar o nome comercial da peça.\n"
            "Prioridade: OEM > catálogo > anúncios (somente para nome).\n"
        )

    return f"""Você é um especialista em precificação e geração de títulos para Mercado Livre, Shopee e OLX de autopeças.

CÓDIGO OEM / PEÇA: {codigo}

{bloco_nome}
{bloco_compat}
═══════════════════════════════════════
ETAPA 2 — PREÇOS (use os anúncios APENAS para preço)
═══════════════════════════════════════

ANÚNCIOS COLETADOS DO MERCADO LIVRE (referência de PREÇO SOMENTE — a compatibilidade já foi extraída acima):
{tlist}

PREÇOS ENCONTRADOS:
{plist}

Ignorar: concessionárias, montadoras, fabricantes oficiais.
Usar: vendedores independentes com boa reputação.
Calcular 4 faixas: médio mercado, competitivo, venda rápida, premium.
NÃO use os anúncios acima para inferir compatibilidade — ela já está definida no bloco anterior.

═══════════════════════════════════════
ETAPA 3 — TÍTULOS MERCADO LIVRE
═══════════════════════════════════════
{regra_titulos}
SHOPEE — 1 título, até 100 caracteres, descritivo, com veículo e código OEM.
OLX — 1 título, até 100 caracteres, tom comercial, com veículo.

═══════════════════════════════════════
ETAPA 4 — DESCRIÇÃO COMPLETA
═══════════════════════════════════════

Gere o campo "descricao_completa" com este formato EXATO (substitua os valores):

CÓDIGO OEM:
{codigo}

APLICAÇÃO:
[função específica da peça no veículo — 1 linha técnica objetiva]

COMPATIBILIDADE:
{bullets_desc}

OBSERVAÇÕES:
[notas técnicas relevantes: lado, variantes, revisão manual se necessário]

═══════════════════════════════════════
VALIDAÇÃO FINAL
═══════════════════════════════════════

1. Os títulos contêm marca + modelo do OEM confirmado? (se não, REESCREVA)
2. Existe veículo inferido em compatibilidades_confirmadas? (se sim, REMOVA)
3. O código OEM aparece em no máximo 1 dos 4 títulos?
4. descricao_completa usa o formato exato solicitado?

═══════════════════════════════════════
SAÍDA — JSON PURO
═══════════════════════════════════════

Retorne SOMENTE o JSON abaixo, sem texto antes ou depois:

{{
  "nome_peca": "Nome comercial da peça identificada pelo código OEM",
  "oem": "{codigo}",
  "compatibilidades_confirmadas": [
    {{"veiculo": "Marca Modelo Versão Motor", "anos": "2021 2022 2023 2024", "detalhes": "1.3 Turbo Flex"}}
  ],
  "grau_de_confianca": 95,
  "mercado_livre": [
    "Título ML 1 com veículo (55-60 chars)",
    "Título ML 2 com veículo (55-60 chars)",
    "Título ML 3 com veículo (55-60 chars)",
    "Título ML 4 com veículo (55-60 chars)"
  ],
  "shopee": "Título Shopee com veículo e OEM (máx 100 chars)",
  "olx": "Título OLX com veículo (máx 100 chars)",
  "titulos_otimizados": ["cópia de mercado_livre[0]", "cópia [1]", "cópia [2]", "cópia [3]"],
  "titulo_ia": "Cópia de mercado_livre[0] com código {codigo} no final (máx 60 chars)",
  "preco_sugerido": 0.00,
  "preco_medio_mercado": 0.00,
  "preco_competitivo": 0.00,
  "preco_venda_rapida": 0.00,
  "preco_premium": 0.00,
  "explicacao": "O que é a peça e para que serve (2 linhas máximo)",
  "funcao": "Função técnica resumida em 1 linha",
  "descricao_completa": "CÓDIGO OEM:\\n{codigo}\\n\\nAPLICAÇÃO:\\n...\\n\\nCOMPATIBILIDADE:\\n• ...\\n\\nOBSERVAÇÕES:\\n...",
  "categoria": "Categoria ML exata",
  "ncm": "",
  "cest": "",
  "seo_palavras_chave": ["palavra com veículo", "OEM e modelo", "marca modelo peça"],
  "observacoes": "Notas técnicas (lado, variantes, OEM original vs similar)"
}}

REGRAS DO JSON:
- mercado_livre: EXATAMENTE 4 títulos entre 55 e 60 chars, todos diferentes, TODOS com veículo da lista OEM
- shopee: máximo 100 chars
- olx: máximo 100 chars
- titulos_otimizados: cópia exata de mercado_livre
- titulo_ia: máximo 60 chars, código {codigo} SEMPRE no final
{regra_compat_json}
- compatibilidades_confirmadas[].anos: cada ano INDIVIDUAL separado por espaço (NUNCA "a" ou "-")
- descricao_completa: campo obrigatório no formato CÓDIGO OEM / APLICAÇÃO / COMPATIBILIDADE / OBSERVAÇÕES
- seo_palavras_chave: inclua marca, modelo e código OEM nas keywords
- grau_de_confianca: 0-100
- preco_sugerido = preco_competitivo
- Responda SOMENTE JSON válido, sem markdown, sem explicações"""


def _ajustar_titulos(data, codigo):
    def limpar(titulo, maxlen=60):
        if not titulo: return ""
        return titulo.strip().replace("  ", " ")[:maxlen].strip()

    def com_codigo(titulo, maxlen=60):
        if not titulo: return ""
        # Remove todas as ocorrências do código para evitar duplicatas
        t = re.sub(re.escape(codigo), "", titulo, flags=re.IGNORECASE).strip().replace("  ", " ").strip()
        if not t:
            return codigo[:maxlen]
        espaco = maxlen - len(codigo) - 1
        if espaco <= 3:
            return codigo[:maxlen]
        base = t[:espaco].strip()
        return f"{base} {codigo}"[:maxlen]

    def enforcar_faixa(titulo, max_len=60):
        return limpar(titulo, max_len)

    def _valido(titulo):
        """Retorna False se o título for apenas o código OEM repetido."""
        if not titulo: return False
        sem_cod = re.sub(re.escape(codigo), "", titulo, flags=re.IGNORECASE).strip()
        return len(sem_cod) >= 5

    # Títulos ML
    ml = data.get("mercado_livre") or data.get("titulos_otimizados") or []
    nome_base = data.get("nome_peca") or ""
    # Filtra títulos inválidos (só com o OEM)
    ml = [t for t in ml if _valido(t)]
    # Completa até 4 títulos usando nome_peca como fallback
    while len(ml) < 4:
        if nome_base and _valido(nome_base):
            sufixos = ["", " Original", " Usado", " Nacional"]
            ml.append(f"{nome_base}{sufixos[len(ml)%4]}"[:60])
        else:
            ml.append(f"Peça Automotiva {codigo}"[:60])
    ml_ajustados = [
        enforcar_faixa(ml[0]),
        com_codigo(ml[1]),
        enforcar_faixa(ml[2]),
        enforcar_faixa(ml[3]),
    ]
    data["mercado_livre"]      = ml_ajustados
    data["titulos_otimizados"] = ml_ajustados

    # Shopee
    data["shopee"] = limpar(data.get("shopee") or (ml_ajustados[0] + " " + codigo), 100)

    # OLX
    data["olx"] = limpar(data.get("olx") or ml_ajustados[0], 100)

    # titulo_ia
    data["titulo_ia"] = com_codigo(data.get("titulo_ia") or ml_ajustados[1])

    # Mapeia compatibilidades_confirmadas → compatibilidade (campo legado lido pelo frontend)
    confirmadas = data.get("compatibilidades_confirmadas")
    if isinstance(confirmadas, list):
        data["compatibilidade"] = [
            {"veiculo": c.get("veiculo", ""), "anos": c.get("anos", ""), "status": "COMPATÍVEL"}
            for c in confirmadas
        ]
        data["versoes"] = [
            {"veiculo": c.get("veiculo", ""), "anos": c.get("anos", ""), "detalhes": c.get("detalhes", "")}
            for c in confirmadas
        ]
    elif not data.get("compatibilidade"):
        data["compatibilidade"] = []
        data["versoes"] = []

    # grau_de_confianca — garante campo presente
    if "grau_de_confianca" not in data:
        data["grau_de_confianca"] = 0

    return data

def _chamar_ia(prompt):
    cfg = carregar_config()
    provider = cfg.get("provider", "gemini")
    if provider == "gemini":
        return _gemini(cfg.get("gemini_key", ""), prompt)
    return _claude(cfg.get("api_key", ""), prompt)

def _chamar_ia_texto(prompt_text):
    """Chama IA esperando resposta texto puro (não JSON)."""
    cfg = carregar_config()
    provider = cfg.get("provider", "gemini")
    try:
        if provider == "gemini":
            api_key = cfg.get("gemini_key", "")
            if not api_key: return None
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
            r = requests.post(url, json={
                "contents": [{"parts": [{"text": prompt_text}]}],
                "generationConfig": {"maxOutputTokens": 80, "temperature": 0.1,
                                     "thinkingConfig": {"thinkingBudget": 0}}
            }, timeout=20)
            if r.status_code == 200:
                parts = r.json()["candidates"][0]["content"]["parts"]
                return " ".join(p["text"] for p in parts if "text" in p).strip()
        else:
            api_key = cfg.get("api_key", "")
            if not api_key: return None
            r = requests.post("https://api.anthropic.com/v1/messages",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": "claude-haiku-4-5-20251001", "max_tokens": 80,
                      "messages": [{"role": "user", "content": prompt_text}]}, timeout=20)
            return r.json()["content"][0]["text"].strip()
    except Exception:
        return None

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
    """Pede à IA o nome comercial da peça para usar como query no ML."""
    prompt = (
        f"Código OEM de autopeça: {codigo}\n"
        "Qual é o nome comercial desta peça no Brasil? "
        "Responda SOMENTE o nome curto em português, ex: 'Sensor Rotação Renault' ou 'Vela Ignição NGK'. "
        "Sem explicações, sem código, apenas o nome."
    )
    texto = _chamar_ia_texto(prompt)
    if texto:
        # Remove o próprio código se a IA o repetiu
        limpo = texto.replace(codigo, "").strip().strip(".,:-").strip()
        if limpo and len(limpo) > 3:
            return limpo[:60]
    return ""

# ─── Lógica principal de busca ────────────────────────────────────────────────
def executar_busca(codigo, compatibilidade_oem=None, nome_peca_fixo=None):
    """
    FLUXO CORRETO:
    1. Lista ML pelo OEM exato → lista.mercadolivre.com.br/{oem}
    2. Abre anúncios relevantes individualmente via API + Playwright
    3. Lê título, atributos, descrição de cada um
    4. Consolida + score de compatibilidade
    5. IA apenas formata (títulos, descrição, SEO) — NUNCA deduz compat
    """
    print(f"\n[WRX] ══ BUSCA: {codigo} ══")

    # ── ETAPA 1: ML API busca rápida com atributos básicos ───────────────────
    print(f"[WRX] ETAPA 1: ML API search...")
    items_api = _buscar_api_ml_detalhado(codigo)
    titulos_ml   = [i["titulo"] for i in items_api if i["titulo"]]
    precos_novos = [i["preco"] for i in items_api if i.get("condicao") == "new"  and i["preco"] > 5]
    precos_usados= [i["preco"] for i in items_api if i.get("condicao") == "used" and i["preco"] > 5]
    items_com_oem = [i for i in items_api if codigo.upper().replace(" ","") in i["titulo"].upper().replace(" ","")]
    print(f"[WRX] API: {len(items_api)} resultados | {len(items_com_oem)} com OEM no título")

    # Fallback 1: se API não retornou nada, tenta buscar_ml() com HTML scraping multicamada
    if not items_api:
        print(f"[WRX] API sem resultados — fallback para buscar_ml() (HTML scraping)...")
        titulos_fb, novos_fb, usados_fb = buscar_ml(codigo)
        if titulos_fb:
            titulos_ml.extend(t for t in titulos_fb if t not in titulos_ml)
            precos_novos.extend(novos_fb)
            precos_usados.extend(usados_fb)
            print(f"[WRX] buscar_ml() encontrou {len(titulos_fb)} títulos | novos={len(novos_fb)} usados={len(usados_fb)}")

    # Fallback 2 (seguro): busca "nome + OEM" juntos na API — nunca só pelo nome
    # Isso filtra peças com mesmo nome mas OEM diferente
    if not items_api and nome_peca_fixo:
        query_segura = f"{nome_peca_fixo} {codigo}"
        print(f"[WRX] Fallback seguro: buscando '{query_segura}'...")
        items_por_nome = _buscar_api_ml_detalhado(query_segura)
        if items_por_nome:
            items_api = items_por_nome
            items_com_oem = [i for i in items_por_nome if codigo.upper().replace(" ","") in i["titulo"].upper().replace(" ","")]
            titulos_ml.extend(i["titulo"] for i in items_por_nome if i["titulo"] and i["titulo"] not in titulos_ml)
            precos_novos.extend(i["preco"] for i in items_por_nome if i.get("condicao") == "new"  and i["preco"] > 5)
            precos_usados.extend(i["preco"] for i in items_por_nome if i.get("condicao") == "used" and i["preco"] > 5)
            print(f"[WRX] Fallback nome+OEM: {len(items_por_nome)} resultados | {len(items_com_oem)} com OEM no título")

    # ── ETAPA 2: Detalhes completos dos itens com OEM confirmado via API ─────
    anuncios = []
    # Inclui os básicos da busca
    anuncios.extend(items_api[:5])
    # Busca atributos completos dos que têm OEM
    ids = [i["id"] for i in (items_com_oem or items_api)[:5] if i.get("id")]
    print(f"[WRX] ETAPA 2: Buscando detalhes de {len(ids)} itens via API...")
    for item_id in ids:
        det = _buscar_item_completo_api(item_id)
        if det.get("atributos"):
            anuncios.append(det)
            print(f"[WRX]   {item_id}: {len(det['atributos'])} atributos | {det['titulo'][:50]}")

    # ── ETAPA 3: Playwright nas páginas de anúncio se atributos insuficientes ─
    def _tem_compat_nos_atributos(lista):
        for a in lista:
            for k in a.get("atributos", {}):
                if _normalizar_chave_attr(k) in ("marca", "modelo"):
                    return True
        return False

    if not _tem_compat_nos_atributos(anuncios):
        print(f"[WRX] ETAPA 3: API sem atributos de compat → Playwright nas páginas...")
        urls_lista = [
            f"https://lista.mercadolivre.com.br/{codigo}",
            f"https://lista.mercadolivre.com.br/acessorios-veiculos/{codigo}",
        ]
        htmls_lista = _buscar_playwright_python(urls_lista)
        if not htmls_lista:
            htmls_lista = _buscar_requests_html(urls_lista)

        urls_pdp = []
        for h in htmls_lista:
            urls_pdp.extend(_extrair_urls_da_lista_html(h))
        # Adiciona permalinks da API
        for i in (items_com_oem or items_api)[:3]:
            if i.get("permalink") and i["permalink"] not in urls_pdp:
                urls_pdp.append(i["permalink"])
        urls_pdp = list(dict.fromkeys(urls_pdp))[:5]

        if urls_pdp:
            print(f"[WRX] Scraping {len(urls_pdp)} páginas de anúncio...")
            for item in _scrape_paginas_anuncio_playwright(urls_pdp):
                parsed = _parse_pagina_anuncio(item["html"], item["url"])
                if parsed.get("atributos") or parsed.get("titulo"):
                    anuncios.append(parsed)
                    print(f"[WRX]   PDP: {len(parsed['atributos'])} attrs | {parsed['titulo'][:50]}")
        else:
            print(f"[WRX] ETAPA 3: Sem URLs de anúncio para scraping")

    # ── ETAPA 4: Consolida e cria score ──────────────────────────────────────
    print(f"[WRX] ETAPA 4: Consolidando {len(anuncios)} anúncios...")
    consolidado = _consolidar_e_score(anuncios, codigo)

    oem_confirmado_ml = bool(items_com_oem)

    # Também confirma OEM se aparecer em título, atributos ou descrição das páginas raspadas
    if not oem_confirmado_ml:
        oem_clean = codigo.upper().replace(" ", "")
        for a in anuncios:
            texto = " ".join([
                a.get("titulo", ""),
                a.get("descricao", ""),
                " ".join(str(v) for v in a.get("atributos", {}).values())
            ]).upper().replace(" ", "")
            if oem_clean in texto:
                oem_confirmado_ml = True
                print(f"[WRX] OEM confirmado via PDP: {a.get('titulo','')[:60]}")
                break

    compat_consolidada = (consolidado or {}).get("compatibilidade", [])

    if compat_consolidada:
        print(f"[WRX] Compat consolidada: {len(compat_consolidada)} veículo(s)")
        for c in compat_consolidada:
            print(f"  → {c['veiculo']} {c.get('motor','')} | {c['anos']} | x{c.get('ocorrencias',1)}")

    # Compatibilidade fornecida pelo frontend tem prioridade absoluta
    compat_final = compatibilidade_oem or compat_consolidada

    # Fonte e confiança
    if oem_confirmado_ml and compat_final:
        fonte_resultado = "oem_exato_ml"
        grau_confianca  = 99
    elif oem_confirmado_ml:
        fonte_resultado = "oem_exato_ml"
        grau_confianca  = 85
    elif items_api:
        fonte_resultado = "ml_sem_oem_exato"
        grau_confianca  = 50
    else:
        fonte_resultado = "ia_pura" if (nome_peca_fixo or titulos_ml) else "nao_encontrado"
        grau_confianca  = 30 if (nome_peca_fixo or titulos_ml) else 0

    # OEM não confirmado em nenhum anúncio E sem compat do frontend → bloquear
    # nome_peca_fixo NÃO é exceção: evita gerar anúncio com dados errados
    # (só compatibilidade_oem enviada pelo frontend é exceção, pois o usuário validou)
    if not oem_confirmado_ml and not compat_final:
        print(f"[WRX] Bloqueando: OEM {codigo!r} não confirmado em nenhum anúncio ML")
        return {
            "ok": False,
            "erro": f"OEM {codigo} não confirmado em nenhum anúncio do Mercado Livre.",
            "mensagem": "OEM não confirmado. Verifique o código ou cadastre manualmente.",
            "oem_pesquisado": codigo,
            "oem_encontrado": False,
            "fonte_resultado": "oem_nao_confirmado",
            "fonte": "oem_nao_confirmado",
            "grau_de_confianca": 0,
            "nome_peca": nome_peca_fixo or "",
            "titulos_otimizados": [],
            "mercado_livre": [],
            "compatibilidades_confirmadas": [],
        }

    # Nome da peça
    nome_peca_confirmado = nome_peca_fixo
    if not nome_peca_confirmado and items_com_oem:
        nome_peca_confirmado = _extrair_nome_oem_do_titulo(items_com_oem[0]["titulo"], codigo)
    if not nome_peca_confirmado and titulos_ml:
        nome_peca_confirmado = _extrair_nome_oem_do_titulo(titulos_ml[0], codigo) or titulos_ml[0]

    print(f"[WRX] Nome peça: {nome_peca_confirmado!r}")
    print(f"[WRX] Fonte: {fonte_resultado} | Confiança: {grau_confianca}")

    # ── ETAPA 5: IA apenas para formatação — nunca para inferir compat ────────
    precos = sorted(set(round(p,2) for p in precos_novos))[:15] or \
             sorted(set(round(p,2) for p in precos_usados))[:15]
    if consolidado and consolidado.get("precos"):
        precos = precos or consolidado["precos"]
    preco_ref = calcular_preco_sugerido(precos)

    print(f"[WRX] ETAPA 5: Enviando para IA (formatação apenas)...")
    prompt = _build_prompt(
        codigo, titulos_ml, precos[:10],
        compatibilidade_oem=compat_final,
        nome_peca_confirmado=nome_peca_confirmado
    )
    data = _chamar_ia(prompt)

    if not data:
        nome_fallback = nome_peca_confirmado or (titulos_ml[0] if titulos_ml else f"Peça {codigo}")
        titulos_gerados = [
            nome_fallback[:60],
            f"{nome_fallback} {codigo}".strip()[:60],
            f"{nome_fallback} Original".strip()[:60],
            f"{nome_fallback} Usado".strip()[:60],
        ]
        data = {
            "nome_peca": nome_fallback,
            "oem": codigo, "codigo": codigo,
            "titulos_otimizados": titulos_gerados,
            "mercado_livre": titulos_gerados,
            "titulo_ia": titulos_gerados[0],
            "preco_sugerido": preco_ref,
            "compatibilidade": [], "compatibilidades_confirmadas": [], "versoes": [],
            "funcao": nome_fallback, "sem_ia": True,
        }

    if nome_peca_confirmado:
        data["nome_peca"] = nome_peca_confirmado
    if preco_ref > 0:
        data["preco_sugerido"] = preco_ref
    if oem_confirmado_ml:
        data["grau_de_confianca"] = grau_confianca

    data = _ajustar_titulos(data, codigo)

    # NCM/CEST
    nome_ncm = data.get("nome_peca") or (data.get("titulos_otimizados") or [codigo])[0]
    ncm = buscar_ncm_local(nome_ncm)
    if ncm:
        data["ncm"]      = ncm.get("ncm", "")
        data["cest"]     = ncm.get("cest", "")
        data["ncm_desc"] = ncm.get("descricao", "")

    # Campos de auditoria
    data["oem_pesquisado"]         = codigo
    data["oem_encontrado"]         = oem_confirmado_ml
    data["oem_titulo_ml"]          = items_com_oem[0]["titulo"] if items_com_oem else None
    data["fonte_resultado"]        = fonte_resultado
    data["grau_de_confianca"]      = data.get("grau_de_confianca") or grau_confianca
    data["ml_titulos_encontrados"] = len(titulos_ml)
    data["precos_novos"]           = precos_novos
    data["precos_usados"]          = precos_usados
    # legado
    data["fonte"] = fonte_resultado

    print(f"[WRX] Resultado: nome={data.get('nome_peca')!r} | fonte={fonte_resultado} | conf={data['grau_de_confianca']}")
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

    # ── Módulo de compatibilidade OEM (Playwright + ML) ──────────────────────
    try:
        from oem_compat_routes import register_routes as _reg_oem_compat
        _reg_oem_compat(app, carregar_config)
    except Exception as _e_oem:
        print(f"[OEM-COMPAT] Módulo não carregado: {_e_oem}")

    def _cors(resp):
        resp.headers["Access-Control-Allow-Origin"]  = "*"
        resp.headers.setdefault("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Cache-Control, Authorization, Access-Control-Request-Private-Network"
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
        # Compatibilidade OEM confirmada enviada pelo frontend (JSON array)
        compatibilidade_oem = None
        raw_compat = request.args.get("compatibilidade_oem", "").strip()
        if raw_compat:
            try:
                parsed = json.loads(raw_compat)
                if isinstance(parsed, list) and parsed:
                    compatibilidade_oem = [
                        {"veiculo": str(c.get("veiculo", c.get("v", ""))).strip(),
                         "anos": str(c.get("anos", c.get("a", ""))).strip()}
                        for c in parsed
                        if c.get("veiculo") or c.get("v")
                    ] or None
            except Exception:
                pass
        # Nome da peça já cadastrado no sistema — tem prioridade absoluta sobre ML
        nome_peca_fixo = request.args.get("nome_peca", "").strip()[:80] or None
        if nome_peca_fixo:
            print(f"[WRX-API] Nome fixo recebido do frontend: {nome_peca_fixo!r}")
        print(f"[WRX-API] Buscando: {codigo}" + (f" | OEM compat: {len(compatibilidade_oem)} veículos" if compatibilidade_oem else ""))
        resultado = executar_busca(codigo, compatibilidade_oem=compatibilidade_oem, nome_peca_fixo=nome_peca_fixo)
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
                    browser = await p.chromium.launch(
                        headless=True,
                        args=["--no-sandbox","--disable-dev-shm-usage","--disable-gpu",
                              "--disable-setuid-sandbox","--disable-blink-features=AutomationControlled"]
                    )
                    ctx2 = await browser.new_context(
                        locale="pt-BR", user_agent=_UA_ML,
                        viewport={"width": 1280, "height": 800}
                    )
                    await ctx2.add_init_script(_STEALTH_JS)
                    page = await ctx2.new_page()
                    url_pw = f"https://lista.mercadolivre.com.br/{q.replace(' ','-')}"
                    await page.goto(url_pw, timeout=35000, wait_until="domcontentloaded")
                    html_init = await page.content()
                    try:
                        await page.wait_for_selector("li.ui-search-layout__item", timeout=15000)
                        items = await page.query_selector_all("li.ui-search-layout__item")
                        html = await page.content()
                        await browser.close()
                        return len(items), len(html), html_init[:200]
                    except Exception as e2:
                        html = await page.content()
                        await browser.close()
                        return 0, len(html), html_init[:200]
            nitens, tam, html_preview = asyncio.run(_pw_test())
            resultado["camadas"]["playwright"]["itens_encontrados"] = nitens
            resultado["camadas"]["playwright"]["tamanho_html"] = tam
            resultado["camadas"]["playwright"]["html_preview"] = html_preview
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
        if not query and not nome:
            return jsonify({"erro": "Parâmetro ?q= obrigatório"}), 400

        termo = query or nome

        def _scrape_condition(term, condition_suffix):
            """Raspa ML listing page para uma condição (u=usado, n=novo)."""
            slug = re.sub(r'\s+', '-', term.strip().lower())
            urls = [
                f"https://lista.mercadolivre.com.br/acessorios-veiculos/{slug}_Condition_{condition_suffix}",
                f"https://lista.mercadolivre.com.br/{slug}_Condition_{condition_suffix}",
            ]
            # Tenta browser (local Windows: Edge ou Chromium bundled)
            htmls = _buscar_navegador(urls)
            if not htmls:
                # Fallback: requests direto (pode não ter conteúdo em SPA, mas tenta)
                htmls = _buscar_requests_html(urls)
            if not htmls:
                htmls = _buscar_playwright_python(urls)
            precos = []
            for h in htmls:
                _, novos_h, usados_h = _parse_html_ml(h)
                precos.extend(novos_h + usados_h)
            return sorted(set(round(p, 2) for p in precos if p > 5))

        usados_precos = _scrape_condition(termo, "u")
        novos_precos  = _scrape_condition(termo, "n")

        # Fallback: se não achou nada com o termo principal e tem nome, tenta com nome
        if not usados_precos and not novos_precos and query and nome and nome != query:
            usados_precos = _scrape_condition(nome, "u")
            novos_precos  = _scrape_condition(nome, "n")
            termo = nome

        def _fmt(precos):
            return [{"price": p} for p in precos[:15]]

        return jsonify({
            "usados": _fmt(usados_precos),
            "novos":  _fmt(novos_precos),
            "total":  len(usados_precos) + len(novos_precos),
            "termo_usado": termo,
        })

    @app.route("/", methods=["OPTIONS"])
    @app.route("/buscar", methods=["OPTIONS"])
    @app.route("/wrx-buscar", methods=["OPTIONS"])
    @app.route("/carros", methods=["OPTIONS"])
    def options():
        return Response(status=204, headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Cache-Control, Authorization",
        })

    # ─── Integrações: Mercado Livre, OLX, Shopee ─────────────────────────────────
    import urllib.parse as _urlparse
    import hashlib as _hashlib
    import secrets as _secrets
    import base64 as _base64
    # redeploy-trigger-v4

    _INTEG_DIR = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "/tmp")
    _ML_TOKENS_FILE = os.path.join(_INTEG_DIR, "wrx_ml_tokens.json")
    _OLX_TOKENS_FILE = os.path.join(_INTEG_DIR, "wrx_olx_token.json")
    _ML_QUEUE_FILE = os.path.join(_INTEG_DIR, "wrx_ml_queue.json")
    _ml_tokens_mem = {}
    _olx_token_mem = {}
    _pkce_store = {}  # state -> code_verifier

    def _pkce_pair():
        verifier = _base64.urlsafe_b64encode(_secrets.token_bytes(32)).rstrip(b'=').decode()
        digest = _hashlib.sha256(verifier.encode()).digest()
        challenge = _base64.urlsafe_b64encode(digest).rstrip(b'=').decode()
        return verifier, challenge

    ML_REDIRECT_URI = os.environ.get("ML_REDIRECT_URI", "https://wrx-api-production.up.railway.app/integracoes/mercadolivre/oauth/callback")
    OLX_CLIENT_ID = os.environ.get("OLX_CLIENT_ID", "")
    OLX_CLIENT_SECRET = os.environ.get("OLX_CLIENT_SECRET", "")
    OLX_REDIRECT_URI = os.environ.get("OLX_REDIRECT_URI", "https://glowing-pastelito-6e4556.netlify.app/olx-callback.html")
    OLX_TELEFONE = os.environ.get("OLX_TELEFONE", "21964449123")
    OLX_CEP = os.environ.get("OLX_CEP", "22725001")
    SHOPEE_PARTNER_ID = int(os.environ.get("SHOPEE_PARTNER_ID", "2035574"))
    SHOPEE_PARTNER_KEY = os.environ.get("SHOPEE_PARTNER_KEY", "shpk4458415353465759486e516147454957414d4c444761414a577570795655")

    # ── Supabase do usuário (pecas_estoque, shopee_anuncios) ────────────────────
    _WRX_SB_URL = "https://uthsiihzpsgarargegcw.supabase.co"
    _WRX_SB_KEY = "sb_publishable_gOQgHrv2IVRgbiVV2Myhzg_BmzCXmXe"

    def _wrx_headers():
        return {
            "apikey": _WRX_SB_KEY,
            "Authorization": f"Bearer {_WRX_SB_KEY}",
            "Content-Type": "application/json",
        }

    # ── PartHub Supabase — persistência de tokens entre redeployments ──────────
    _PH_HOST  = "iftzoceaalhpyckuznae.supabase.co"
    _PH_ANON  = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImlmdHpvY2VhYWxocHlja3V6bmFlIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NjA0MzMwNjcsImV4cCI6MjA3NjAwOTA2N30.VZY9NLFvRMX-lr9FQUlOkMfE0RfdGxk0HVpslxMYDYg"
    _PH_EMAIL = "geisuaine2025@gmail.com"
    _PH_SENHA = "Vitoria12$"
    _ph_jwt_cache = {"token": None, "expires_at": 0}

    def _ph_get_jwt():
        if _ph_jwt_cache["token"] and time.time() < _ph_jwt_cache["expires_at"] - 60:
            return _ph_jwt_cache["token"]
        try:
            _r = requests.post(
                f"https://{_PH_HOST}/auth/v1/token?grant_type=password",
                json={"email": _PH_EMAIL, "password": _PH_SENHA},
                headers={"apikey": _PH_ANON, "Content-Type": "application/json"},
                timeout=10
            )
            if _r.status_code == 200:
                _d = _r.json()
                _ph_jwt_cache["token"] = _d.get("access_token")
                _ph_jwt_cache["expires_at"] = time.time() + _d.get("expires_in", 3600)
                return _ph_jwt_cache["token"]
        except Exception:
            pass
        return None

    def _ph_save_tokens_remote(ml_tokens):
        jwt = _ph_get_jwt()
        if not jwt:
            print("[ML-TOKENS] ERRO: falha ao obter JWT do Supabase — tokens nao persistidos remotamente")
            return False
        try:
            r = requests.put(
                f"https://{_PH_HOST}/auth/v1/user",
                json={"data": {"wrx_ml_tokens": ml_tokens}},
                headers={"apikey": _PH_ANON, "Authorization": f"Bearer {jwt}", "Content-Type": "application/json"},
                timeout=15
            )
            if r.status_code == 200:
                print(f"[ML-TOKENS] Supabase: tokens salvos. Contas: {list(ml_tokens.keys())}")
                return True
            print(f"[ML-TOKENS] ERRO Supabase ao salvar: HTTP {r.status_code} — {r.text[:200]}")
            return False
        except Exception as e:
            print(f"[ML-TOKENS] ERRO Supabase (excecao): {e}")
            return False

    def _ph_load_tokens_remote():
        jwt = _ph_get_jwt()
        if not jwt:
            return {}
        try:
            _r = requests.get(
                f"https://{_PH_HOST}/auth/v1/user",
                headers={"apikey": _PH_ANON, "Authorization": f"Bearer {jwt}"},
                timeout=10
            )
            if _r.status_code == 200:
                return _r.json().get("user_metadata", {}).get("wrx_ml_tokens", {})
        except Exception:
            pass
        return {}

    def _ml_load_tokens():
        global _ml_tokens_mem
        if _ml_tokens_mem:
            return _ml_tokens_mem
        try:
            with open(_ML_TOKENS_FILE) as _f:
                loaded = json.load(_f)
                if loaded:
                    _ml_tokens_mem = loaded
                    print(f"[ML-TOKENS] Carregado do arquivo local. Contas: {list(loaded.keys())}")
                    return _ml_tokens_mem
        except Exception:
            pass
        # Arquivo local vazio/inexistente — busca no Supabase (fonte primária entre redeployments)
        print("[ML-TOKENS] Arquivo local ausente. Buscando no Supabase...")
        remote = _ph_load_tokens_remote()
        if remote:
            _ml_tokens_mem = remote
            print(f"[ML-TOKENS] Carregado do Supabase. Contas: {list(remote.keys())}")
            try:
                with open(_ML_TOKENS_FILE, "w") as _f:
                    json.dump(remote, _f)
            except Exception:
                pass
        else:
            print("[ML-TOKENS] Supabase sem tokens. Nenhuma conta autorizada.")
        return _ml_tokens_mem

    def _ml_save_tokens(tokens):
        global _ml_tokens_mem
        _ml_tokens_mem = tokens
        contas = list(tokens.keys())
        # Arquivo local (secundário — /tmp é efêmero no Railway sem volume configurado)
        try:
            with open(_ML_TOKENS_FILE, "w") as _f:
                json.dump(tokens, _f)
            print(f"[ML-TOKENS] Arquivo local salvo: {_ML_TOKENS_FILE}. Contas: {contas}")
        except Exception as e:
            print(f"[ML-TOKENS] Aviso: arquivo local nao salvo ({e})")
        # Supabase — fonte primária de persistência entre redeployments (síncrono)
        ok = _ph_save_tokens_remote(tokens)
        if not ok:
            print(f"[ML-TOKENS] FALHA CRITICA: tokens NAO persistidos no Supabase. Contas em risco: {contas}")
        return ok

    def _ml_get_user_token(conta="default"):
        tokens = _ml_load_tokens()
        t = tokens.get(conta, {})
        if not t.get("access_token"):
            print(f"[ML-TOKENS] Conta '{conta}' nao autorizada. Contas disponiveis: {list(tokens.keys())}")
            return None
        if t.get("expires_at", 0) - time.time() < 300:
            print(f"[ML-TOKENS] Token da conta '{conta}' expirando. Iniciando refresh...")
            try:
                _r = requests.post("https://api.mercadolibre.com/oauth/token", data={
                    "grant_type": "refresh_token",
                    "client_id": ML_CLIENT_ID,
                    "client_secret": ML_CLIENT_SECRET,
                    "refresh_token": t.get("refresh_token", "")
                }, timeout=10)
                if _r.status_code == 200:
                    _d = _r.json()
                    t["access_token"] = _d["access_token"]
                    t["refresh_token"] = _d.get("refresh_token", t["refresh_token"])
                    t["expires_at"] = time.time() + _d.get("expires_in", 21600)
                    tokens[conta] = t
                    _ml_save_tokens(tokens)
                    print(f"[ML-TOKENS] Token da conta '{conta}' renovado com sucesso.")
                else:
                    print(f"[ML-TOKENS] ERRO ao renovar token '{conta}': HTTP {_r.status_code} — {_r.text[:200]}")
                    del tokens[conta]
                    _ml_save_tokens(tokens)
                    return None
            except Exception as e:
                print(f"[ML-TOKENS] ERRO (excecao) ao renovar token '{conta}': {e}")
                return None
        print(f"[ML-TOKENS] Conta '{conta}' autorizada. Token valido.")
        return t.get("access_token")

    def _options_resp():
        return Response(status=204, headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, PUT, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Cache-Control, Authorization",
        })

    # ML OAuth
    @app.route("/integracoes/mercadolivre/oauth")
    def ml_oauth():
        from flask import redirect as _redir
        conta = request.args.get("conta", "default")
        verifier, challenge = _pkce_pair()
        # Embute conta+verifier no state para evitar problema de múltiplos workers
        state_payload = _base64.urlsafe_b64encode(f"{conta}:{verifier}".encode()).rstrip(b'=').decode()
        url = (
            "https://auth.mercadolivre.com.br/authorization"
            f"?response_type=code&client_id={ML_CLIENT_ID.strip()}"
            f"&redirect_uri={_urlparse.quote(ML_REDIRECT_URI, safe='')}"
            f"&state={state_payload}"
            f"&code_challenge={challenge}&code_challenge_method=S256"
        )
        return _redir(url)

    @app.route("/integracoes/mercadolivre/oauth/callback")
    def ml_oauth_callback():
        code = request.args.get("code", "")
        state_raw = request.args.get("state", "")
        conta = "default"
        verifier = None
        try:
            # Decodifica state que contém "conta:verifier"
            padded = state_raw + "=" * (4 - len(state_raw) % 4)
            decoded = _base64.urlsafe_b64decode(padded).decode()
            conta, verifier = decoded.split(":", 1)
        except Exception:
            conta = state_raw or "default"
        if not code:
            return jsonify({"erro": "codigo OAuth ausente"}), 400
        try:
            _exchange = {
                "grant_type": "authorization_code",
                "client_id": ML_CLIENT_ID.strip(),
                "client_secret": ML_CLIENT_SECRET.strip(),
                "code": code,
                "redirect_uri": ML_REDIRECT_URI
            }
            if verifier:
                _exchange["code_verifier"] = verifier
            _r = requests.post("https://api.mercadolibre.com/oauth/token",
                               data=_exchange, timeout=15)
            if _r.status_code != 200:
                return jsonify({"erro": f"ML {_r.status_code}: {_r.text[:300]}"}), 400
            _d = _r.json()
            tokens = _ml_load_tokens()
            tokens[conta] = {
                "access_token": _d["access_token"],
                "refresh_token": _d.get("refresh_token", ""),
                "expires_at": time.time() + _d.get("expires_in", 21600),
                "user_id": str(_d.get("user_id", ""))
            }
            ok_sb = _ml_save_tokens(tokens)
            print(f"[ML-OAUTH] Conta '{conta}' autorizada. Supabase: {'OK' if ok_sb else 'FALHOU'}")
            sb_cor = "#22c55e" if ok_sb else "#f59e0b"
            sb_msg = "Backup no Supabase: salvo ✓" if ok_sb else "⚠ Backup no Supabase falhou — verifique os logs"
            return (
                "<html><body style='font-family:sans-serif;text-align:center;padding:40px;"
                "background:#0f172a;color:#fff'>"
                "<h2 style='color:#22c55e'>&#10003; Mercado Livre conectado!</h2>"
                f"<p>Conta: <strong>{conta}</strong></p>"
                f"<p style='color:{sb_cor};font-size:13px'>{sb_msg}</p>"
                "<p style='color:#9ca3af'>Pode fechar esta janela.</p>"
                "</body></html>"
            )
        except Exception as _e:
            return jsonify({"erro": str(_e)}), 500

    @app.route("/integracoes/mercadolivre/config")
    def ml_config():
        tokens = _ml_load_tokens()
        contas = {c: bool(t.get("access_token")) for c, t in tokens.items()}
        return jsonify({
            "configured": True,
            "contas": contas,
            "oauth_url_default": "/integracoes/mercadolivre/oauth?conta=default",
            "oauth_url_geisa": "/integracoes/mercadolivre/oauth?conta=geisa",
        })

    @app.route("/integracoes/mercadolivre/debug-token")
    def ml_debug_token():
        conta = request.args.get("conta", "default")
        token = _ml_get_user_token(conta)
        if not token:
            return jsonify({"erro": "sem token para esta conta"}), 401
        try:
            me = requests.get("https://api.mercadolibre.com/users/me",
                              headers={"Authorization": f"Bearer {token}"}, timeout=10).json()
            cats = requests.get("https://api.mercadolibre.com/users/me/items/search?status=active&limit=5",
                                headers={"Authorization": f"Bearer {token}"}, timeout=10).json()
            test_payload = {
                "title": "Teste API",
                "family_name": "Teste API",
                "category_id": "MLB3530",
                "price": 10.0,
                "currency_id": "BRL",
                "available_quantity": 1,
                "buying_mode": "buy_it_now",
                "condition": "used",
                "listing_type_id": "free",
                "seller_custom_field": "TESTE-DEBUG",
                "shipping": {"mode": "me2", "free_shipping": False}
            }
            test_r = requests.post("https://api.mercadolibre.com/items",
                                   headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                                   json=test_payload, timeout=15)
            return jsonify({
                "user_id": me.get("id"),
                "nickname": me.get("nickname"),
                "site_id": me.get("site_id"),
                "seller_reputation": me.get("seller_reputation", {}).get("level_id"),
                "test_post_status": test_r.status_code,
                "test_post_response": test_r.json() if test_r.headers.get("content-type","").startswith("application/json") else test_r.text[:300],
                "active_items": cats.get("results", [])[:5]
            })
        except Exception as _e:
            return jsonify({"erro": str(_e)}), 500

    @app.route("/integracoes/mercadolivre/categorias/predizer", methods=["GET", "OPTIONS"])
    def ml_categorias_predizer():
        if request.method == "OPTIONS":
            return _options_resp()
        q = request.args.get("q", "").strip()
        site_id = request.args.get("site_id", "MLB")
        limit = request.args.get("limit", "1")
        if not q:
            return jsonify({"erro": "q obrigatorio"}), 400
        token = _get_ml_token()
        hdrs = {"Accept": "application/json"}
        if token:
            hdrs["Authorization"] = f"Bearer {token}"
        try:
            _r = requests.get(
                f"https://api.mercadolibre.com/sites/{site_id}/domain_discovery/search",
                params={"limit": limit, "q": q}, headers=hdrs, timeout=10
            )
            if _r.status_code == 200:
                _data = _r.json()
                if _data:
                    return jsonify(_data[0])
                return jsonify({"erro": "sem categoria"}), 404
            return jsonify({"erro": f"ML {_r.status_code}"}), _r.status_code
        except Exception as _e:
            return jsonify({"erro": str(_e)}), 500

    @app.route("/integracoes/mercadolivre/status-sku", methods=["GET", "OPTIONS"])
    def ml_status_sku():
        if request.method == "OPTIONS":
            return _options_resp()
        sku_raw = request.args.get("sku", "").strip()
        if not sku_raw:
            return jsonify({"erro": "sku obrigatorio"}), 400
        sku_base = sku_raw.split("-")[0]
        tokens = _ml_load_tokens()
        all_items = []
        for conta_nome in list(tokens.keys()):
            token = _ml_get_user_token(conta_nome)
            if not token:
                continue
            try:
                me_r = requests.get("https://api.mercadolibre.com/users/me",
                                    headers={"Authorization": f"Bearer {token}"}, timeout=8)
                if me_r.status_code != 200:
                    continue
                user_id = me_r.json().get("id")
                for sku_q in set([sku_raw, sku_base]):
                    sr = requests.get(
                        f"https://api.mercadolibre.com/users/{user_id}/items/search",
                        params={"seller_sku": sku_q, "limit": 20},
                        headers={"Authorization": f"Bearer {token}"}, timeout=8
                    )
                    if sr.status_code != 200:
                        continue
                    for iid in sr.json().get("results", [])[:8]:
                        ir = requests.get(f"https://api.mercadolibre.com/items/{iid}",
                                          headers={"Authorization": f"Bearer {token}"}, timeout=8)
                        if ir.status_code == 200:
                            item = ir.json()
                            all_items.append({
                                "mlId": item["id"],
                                "sku": item.get("seller_custom_field", sku_raw),
                                "skuInterno": sku_base,
                                "titulo": item.get("title", ""),
                                "status": item.get("status", ""),
                                "preco": item.get("price", 0),
                                "conta": conta_nome,
                                "grupo": item.get("status", "")
                            })
            except Exception:
                continue
        if all_items:
            return jsonify({"ok": True, "anuncios": all_items, "item": all_items[0]})
        return jsonify({"ok": False, "anuncios": [], "mensagem": "SKU nao encontrado"})

    @app.route("/integracoes/mercadolivre/publicar-local", methods=["POST", "OPTIONS"])
    def ml_publicar_local():
        if request.method == "OPTIONS":
            return _options_resp()
        data = request.get_json(force=True) or {}
        sku = data.get("sku", "")
        if not sku:
            return jsonify({"erro": "sku obrigatorio"}), 400
        try:
            queue = []
            if os.path.exists(_ML_QUEUE_FILE):
                with open(_ML_QUEUE_FILE) as _f:
                    queue = json.load(_f)
            queue = [q for q in queue if q.get("sku") != sku]
            data["saved_at"] = time.time()
            data["grupo"] = "pending_publish"
            queue.append(data)
            with open(_ML_QUEUE_FILE, "w") as _f:
                json.dump(queue, _f)
        except Exception:
            pass
        return jsonify({"ok": True, "sku": sku, "grupo": "pending_publish"})

    @app.route("/integracoes/mercadolivre/publicar", methods=["POST", "OPTIONS"])
    def ml_publicar():
        if request.method == "OPTIONS":
            return _options_resp()
        data = request.get_json(force=True) or {}
        conta_nome = data.get("nome", "default")
        sku = data.get("sku", "") or data.get("_sku", "")
        if not sku:
            return jsonify({"ok": False, "erro": "sku obrigatorio"}), 400
        token = _ml_get_user_token(conta_nome)
        if not token:
            # Save to queue and inform frontend
            try:
                queue = []
                if os.path.exists(_ML_QUEUE_FILE):
                    with open(_ML_QUEUE_FILE) as _f:
                        queue = json.load(_f)
                queue = [q for q in queue if q.get("sku") != sku]
                data["saved_at"] = time.time()
                data["grupo"] = "pending_publish"
                queue.append(data)
                with open(_ML_QUEUE_FILE, "w") as _f:
                    json.dump(queue, _f)
            except Exception:
                pass
            return jsonify({
                "ok": False,
                "erro": f"Conta '{conta_nome}' nao autorizada. Acesse /integracoes/mercadolivre/oauth?conta={conta_nome}",
                "authUrl": f"https://wrx-api-production.up.railway.app/integracoes/mercadolivre/oauth?conta={conta_nome}",
                "grupo": "pending_publish"
            }), 401
        fotos = [f for f in data.get("fotos", []) if f and (f.startswith("http://") or f.startswith("https://"))]
        preco = float(data.get("preco", 0) or 0)
        if preco <= 0:
            return jsonify({"ok": False, "erro": "preco invalido"}), 400
        _titulo = (data.get("titulo", "") or data.get("nomeInterno", ""))[:60]
        ml_payload = {
            "family_name": (data.get("family_name") or _titulo)[:60],
            "category_id": data.get("mlCategoryId", "") or "MLB3530",
            "price": preco,
            "currency_id": "BRL",
            "available_quantity": int(data.get("quantidade", 1) or 1),
            "buying_mode": "buy_it_now",
            "condition": data.get("condicao", "used"),
            "listing_type_id": data.get("listingTypeId", "gold_special"),
            "seller_custom_field": sku,
            "shipping": {"mode": "me2", "free_shipping": bool(data.get("freeShipping", False))}
        }
        if fotos:
            ml_payload["pictures"] = [{"source": f} for f in fotos[:10]]
        attrs = list(data.get("attributes") or [])
        attr_ids = {a.get("id") for a in attrs}
        # Atributos obrigatórios: PART_NUMBER, BRAND, dimensões de embalagem
        if "PART_NUMBER" not in attr_ids and sku:
            attrs.append({"id": "PART_NUMBER", "value_name": sku})
        if "BRAND" not in attr_ids:
            import re as _re
            brand_m = _re.search(r'\b([A-Za-záéíóúãõç]{3,})\s+\d{4}', _titulo)
            brand_val = brand_m.group(1).capitalize() if brand_m else "Genérico"
            attrs.append({"id": "BRAND", "value_name": brand_val})
        _pkg_defaults = [
            ("seller_package_height", f"{int(data.get('package_height') or 30)} cm"),
            ("seller_package_width",  f"{int(data.get('package_width')  or 30)} cm"),
            ("seller_package_length", f"{int(data.get('package_length') or 50)} cm"),
            ("seller_package_weight", f"{int(data.get('package_weight') or 2000)} g"),
        ]
        for pid, pval in _pkg_defaults:
            if pid not in attr_ids:
                attrs.append({"id": pid, "value_name": pval})
        if attrs:
            ml_payload["attributes"] = attrs
        try:
            _r = requests.post(
                "https://api.mercadolibre.com/items",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=ml_payload, timeout=25
            )
            if _r.status_code in (200, 201):
                item = _r.json()
                item_id = item.get("id")
                # Posta descrição separadamente (ML recomenda POST separado)
                if item_id and data.get("descricao"):
                    try:
                        requests.post(
                            f"https://api.mercadolibre.com/items/{item_id}/description",
                            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                            json={"plain_text": str(data["descricao"])[:50000]},
                            timeout=10
                        )
                    except Exception:
                        pass
                return jsonify({"ok": True, "mlId": item_id, "item": item, "conta": conta_nome})
            _err = _r.json() if _r.headers.get("content-type", "").startswith("application/json") else {}
            return jsonify({
                "ok": False,
                "erro": _err.get("message") or _err.get("error") or f"ML {_r.status_code}",
                "detalhes": _r.text[:500]
            }), _r.status_code
        except Exception as _e:
            return jsonify({"ok": False, "erro": str(_e)}), 500

    @app.route("/integracoes/mercadolivre/sincronizar", methods=["POST", "GET", "OPTIONS"])
    def ml_sincronizar():
        if request.method == "OPTIONS":
            return _options_resp()
        tokens = _ml_load_tokens()
        resultado = {}
        for conta_nome in list(tokens.keys()):
            token = _ml_get_user_token(conta_nome)
            resultado[conta_nome] = {"ok": bool(token)}
        return jsonify({"ok": True, "contas": resultado})

    # Cache local de anúncios ML por SKU
    _ML_ANUNCIOS_CACHE_FILE = os.path.join(_INTEG_DIR, "wrx_ml_anuncios.json")
    _ml_anuncios_mem = {}

    def _ml_anuncios_cache_load():
        global _ml_anuncios_mem
        if _ml_anuncios_mem:
            return _ml_anuncios_mem
        try:
            with open(_ML_ANUNCIOS_CACHE_FILE, encoding="utf-8") as f:
                _ml_anuncios_mem = json.load(f)
        except Exception:
            _ml_anuncios_mem = {}
        return _ml_anuncios_mem

    def _ml_anuncios_cache_save(por_sku):
        global _ml_anuncios_mem
        _ml_anuncios_mem = por_sku
        try:
            with open(_ML_ANUNCIOS_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(por_sku, f, ensure_ascii=False)
        except Exception as e:
            print(f"[ML-CACHE] Aviso: nao salvou ({e})")

    def _ml_buscar_todos_ids(token, user_id, status="active"):
        """Busca todos os IDs de anúncios ML do vendedor paginando."""
        ids = []
        offset = 0
        limit = 100
        while True:
            r = requests.get(
                f"https://api.mercadolibre.com/users/{user_id}/items/search",
                params={"status": status, "limit": limit, "offset": offset},
                headers={"Authorization": f"Bearer {token}"}, timeout=15
            )
            if r.status_code != 200:
                break
            d = r.json()
            batch = d.get("results", [])
            ids.extend(batch)
            total = d.get("paging", {}).get("total", 0)
            offset += limit
            if offset >= total or not batch:
                break
        return ids

    def _ml_buscar_detalhes_lote(token, ids):
        """Busca detalhes de até 20 itens por vez."""
        itens = []
        for i in range(0, len(ids), 20):
            lote = ids[i:i+20]
            r = requests.get(
                "https://api.mercadolibre.com/items",
                params={"ids": ",".join(lote),
                        "attributes": "id,title,price,available_quantity,seller_sku,status,thumbnail"},
                headers={"Authorization": f"Bearer {token}"}, timeout=20
            )
            if r.status_code != 200:
                continue
            for entry in r.json():
                if entry.get("code") == 200:
                    itens.append(entry.get("body", {}))
        return itens

    @app.route("/integracoes/mercadolivre/anuncios-db", methods=["GET", "OPTIONS"])
    def ml_anuncios_db():
        if request.method == "OPTIONS":
            return _options_resp()
        # Retorna cache local sem forçar sync
        cache = _ml_anuncios_cache_load()
        total = sum(len(v) for v in cache.values())
        return jsonify({"ok": True, "anunciosPorSku": cache, "totalSkus": len(cache), "totalAnuncios": total, "fonte": "cache"})

    @app.route("/integracoes/mercadolivre/sincronizar-anuncios", methods=["POST", "GET", "OPTIONS"])
    def ml_sincronizar_anuncios():
        if request.method == "OPTIONS":
            return _options_resp()
        tokens_data = _ml_load_tokens()
        if not tokens_data:
            return jsonify({"ok": False, "erro": "Mercado Livre nao autorizado"}), 401

        por_sku = {}
        total_anuncios = 0
        erros = []

        for conta_nome in list(tokens_data.keys()):
            token = _ml_get_user_token(conta_nome)
            if not token:
                erros.append(f"{conta_nome}: token invalido")
                continue
            # Buscar user_id
            user_id = tokens_data.get(conta_nome, {}).get("user_id", "")
            if not user_id:
                try:
                    _r = requests.get("https://api.mercadolibre.com/users/me",
                                      headers={"Authorization": f"Bearer {token}"}, timeout=10)
                    if _r.status_code == 200:
                        user_id = str(_r.json().get("id", ""))
                        tokens_data[conta_nome]["user_id"] = user_id
                        _ml_save_tokens(tokens_data)
                except Exception:
                    pass
            if not user_id:
                erros.append(f"{conta_nome}: user_id nao encontrado")
                continue
            print(f"[ML-SYNC] Buscando anuncios conta={conta_nome} user={user_id}")
            ids_ativos = _ml_buscar_todos_ids(token, user_id, "active")
            print(f"[ML-SYNC] {conta_nome}: {len(ids_ativos)} ativos")
            itens = _ml_buscar_detalhes_lote(token, ids_ativos)
            for item in itens:
                sku = str(item.get("seller_sku") or "").strip().upper()
                if not sku:
                    continue
                estoque = item.get("available_quantity", 0)
                if estoque <= 0:
                    continue  # só com estoque
                if sku not in por_sku:
                    por_sku[sku] = []
                por_sku[sku].append({
                    "externalListingId": item.get("id", ""),
                    "mlId": item.get("id", ""),
                    "titulo": item.get("title", ""),
                    "preco": item.get("price", 0),
                    "estoque": estoque,
                    "status": item.get("status", "active"),
                    "thumbnail": item.get("thumbnail", ""),
                    "integrationId": conta_nome,
                    "marketplace": "ml",
                })
                total_anuncios += 1

        _ml_anuncios_cache_save(por_sku)
        return jsonify({"ok": True, "totalSkus": len(por_sku), "totalAnuncios": total_anuncios, "erros": erros})

    @app.route("/integracoes/mercadolivre/video", methods=["POST", "OPTIONS"])
    def ml_video():
        if request.method == "OPTIONS":
            return _options_resp()
        data = request.get_json(force=True) or {}
        item_id = data.get("mlId") or data.get("item_id", "")
        video_id = data.get("videoId") or data.get("video_id", "")
        conta_nome = data.get("conta", "default")
        if not item_id or not video_id:
            return jsonify({"ok": False, "erro": "mlId e videoId obrigatorios"}), 400
        token = _ml_get_user_token(conta_nome)
        if not token:
            return jsonify({"ok": False, "erro": "conta nao autorizada"}), 401
        try:
            _r = requests.put(f"https://api.mercadolibre.com/items/{item_id}",
                              headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                              json={"video_id": video_id}, timeout=10)
            if _r.status_code in (200, 201):
                return jsonify({"ok": True})
            return jsonify({"ok": False, "erro": _r.text[:200]}), _r.status_code
        except Exception as _e:
            return jsonify({"ok": False, "erro": str(_e)}), 500

    # ── ML: perguntas dos compradores ────────────────────────────────────────────

    @app.route("/integracoes/mercadolivre/perguntas", methods=["GET", "OPTIONS"])
    def ml_perguntas():
        if request.method == "OPTIONS":
            return _options_resp()
        conta = request.args.get("conta", "default")
        status = request.args.get("status", "UNANSWERED")
        token = _ml_get_user_token(conta)
        if not token:
            return jsonify({"ok": False, "erro": "conta nao autorizada"}), 401
        tokens = _ml_load_tokens()
        user_id = tokens.get(conta, {}).get("user_id", "")
        if not user_id:
            # Buscar user_id via /users/me
            try:
                _r = requests.get("https://api.mercadolibre.com/users/me",
                                  headers={"Authorization": f"Bearer {token}"}, timeout=10)
                if _r.status_code == 200:
                    user_id = str(_r.json().get("id", ""))
                    tokens[conta]["user_id"] = user_id
                    _ml_save_tokens(tokens)
            except Exception:
                pass
        if not user_id:
            return jsonify({"ok": False, "erro": "user_id nao encontrado"}), 400
        try:
            _r = requests.get(
                "https://api.mercadolibre.com/questions/search",
                params={"seller_id": user_id, "status": status,
                        "sort_fields": "date_created", "sort_types": "DESC", "limit": 50},
                headers={"Authorization": f"Bearer {token}"}, timeout=15
            )
            if _r.status_code != 200:
                return jsonify({"ok": False, "erro": _r.text[:200]}), _r.status_code
            d = _r.json()
            perguntas = []
            for q in d.get("questions", []):
                # Buscar título do anúncio
                item_title = ""
                item_id = q.get("item_id", "")
                if item_id:
                    try:
                        _ri = requests.get(f"https://api.mercadolibre.com/items/{item_id}?attributes=title",
                                           headers={"Authorization": f"Bearer {token}"}, timeout=8)
                        if _ri.status_code == 200:
                            item_title = _ri.json().get("title", "")
                    except Exception:
                        pass
                perguntas.append({
                    "marketplace": "ml",
                    "id": q.get("id"),
                    "texto": q.get("text", ""),
                    "status": q.get("status", ""),
                    "item_id": item_id,
                    "item_titulo": item_title,
                    "comprador_id": q.get("from", {}).get("id", ""),
                    "data": q.get("date_created", ""),
                    "resposta": q.get("answer", {}).get("text", "") if q.get("answer") else None,
                })
            return jsonify({"ok": True, "total": len(perguntas), "perguntas": perguntas})
        except Exception as _e:
            return jsonify({"ok": False, "erro": str(_e)}), 500

    @app.route("/integracoes/mercadolivre/responder-pergunta", methods=["POST", "OPTIONS"])
    def ml_responder_pergunta():
        if request.method == "OPTIONS":
            return _options_resp()
        data = request.get_json(force=True) or {}
        question_id = data.get("question_id")
        resposta = data.get("resposta", "").strip()
        conta = data.get("conta", "default")
        if not question_id or not resposta:
            return jsonify({"ok": False, "erro": "question_id e resposta obrigatorios"}), 400
        token = _ml_get_user_token(conta)
        if not token:
            return jsonify({"ok": False, "erro": "conta nao autorizada"}), 401
        try:
            _r = requests.post(
                "https://api.mercadolibre.com/answers",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"question_id": question_id, "text": resposta},
                timeout=15
            )
            if _r.status_code in (200, 201):
                return jsonify({"ok": True})
            return jsonify({"ok": False, "erro": _r.text[:200]}), _r.status_code
        except Exception as _e:
            return jsonify({"ok": False, "erro": str(_e)}), 500

    @app.route("/integracoes/mercadolivre/vendas-recentes", methods=["GET", "OPTIONS"])
    def ml_vendas_recentes():
        if request.method == "OPTIONS":
            return _options_resp()
        conta = request.args.get("conta", "default")
        dias = int(request.args.get("dias", "30"))
        token = _ml_get_user_token(conta)
        if not token:
            return jsonify({"ok": False, "erro": "conta nao autorizada"}), 401
        tokens = _ml_load_tokens()
        user_id = tokens.get(conta, {}).get("user_id", "")
        if not user_id:
            try:
                _r = requests.get("https://api.mercadolibre.com/users/me",
                                  headers={"Authorization": f"Bearer {token}"}, timeout=10)
                if _r.status_code == 200:
                    user_id = str(_r.json().get("id", ""))
                    tokens[conta]["user_id"] = user_id
                    _ml_save_tokens(tokens)
            except Exception:
                pass
        if not user_id:
            return jsonify({"ok": False, "erro": "user_id nao encontrado"}), 400
        try:
            from datetime import timedelta
            date_from = (_datetime.utcnow() - timedelta(days=dias)).strftime("%Y-%m-%dT00:00:00.000-00:00")
            _r = requests.get(
                "https://api.mercadolibre.com/orders/search",
                params={"seller": user_id, "sort": "date_desc", "limit": 50,
                        "date_created.from": date_from},
                headers={"Authorization": f"Bearer {token}"}, timeout=15
            )
            if _r.status_code != 200:
                return jsonify({"ok": False, "erro": _r.text[:200]}), _r.status_code
            d = _r.json()
            vendas = []
            for o in d.get("results", []):
                itens = [{"titulo": i.get("item", {}).get("title", ""),
                           "sku": i.get("item", {}).get("seller_sku", ""),
                           "qty": i.get("quantity", 1),
                           "preco": i.get("unit_price", 0)} for i in o.get("order_items", [])]
                vendas.append({
                    "marketplace": "ml",
                    "order_id": o.get("id"),
                    "status": o.get("status", ""),
                    "data": o.get("date_created", ""),
                    "total": o.get("total_amount", 0),
                    "comprador": o.get("buyer", {}).get("nickname", ""),
                    "itens": itens,
                })
            return jsonify({"ok": True, "vendas": vendas, "total": len(vendas)})
        except Exception as _e:
            return jsonify({"ok": False, "erro": str(_e)}), 500

    # ─── OLX ─────────────────────────────────────────────────────────────────────
    @app.route("/integracoes/olx/config", methods=["GET", "OPTIONS"])
    def olx_config():
        if request.method == "OPTIONS":
            return _options_resp()
        global _olx_token_mem
        if not _olx_token_mem.get("access_token"):
            try:
                with open(_OLX_TOKENS_FILE) as _f:
                    _olx_token_mem = json.load(_f)
            except Exception:
                pass
        configured = bool(OLX_CLIENT_ID and OLX_CLIENT_SECRET)
        token_saved = bool(_olx_token_mem.get("access_token"))
        auth_url = None
        if configured and not token_saved:
            auth_url = (
                f"https://auth.olx.com.br/oauth/authorize"
                f"?client_id={OLX_CLIENT_ID}"
                f"&redirect_uri={_urlparse.quote(OLX_REDIRECT_URI, safe='')}"
                f"&response_type=code&scope=basic_user_info%20autoupload"
            )
        return jsonify({"configured": configured, "tokenSaved": token_saved, "authUrl": auth_url})

    @app.route("/integracoes/olx/oauth/callback")
    @app.route("/callback/olx")
    def olx_oauth_callback():
        global _olx_token_mem
        code = request.args.get("code", "")
        if not code or not OLX_CLIENT_ID:
            return jsonify({"erro": "OLX nao configurada ou codigo ausente"}), 400
        try:
            _r = requests.post("https://auth.olx.com.br/oauth/token", data={
                "grant_type": "authorization_code",
                "client_id": OLX_CLIENT_ID,
                "client_secret": OLX_CLIENT_SECRET,
                "code": code,
                "redirect_uri": OLX_REDIRECT_URI
            }, timeout=15)
            if _r.status_code != 200:
                return jsonify({"erro": f"OLX {_r.status_code}: {_r.text[:200]}"}), 400
            _d = _r.json()
            _olx_token_mem = {"access_token": _d.get("access_token", ""), "expires_at": time.time() + _d.get("expires_in", 3600)}
            try:
                with open(_OLX_TOKENS_FILE, "w") as _f:
                    json.dump(_olx_token_mem, _f)
            except Exception:
                pass
            return ("<html><body style='font-family:sans-serif;text-align:center;padding:40px;"
                    "background:#0f172a;color:#fff'><h2 style='color:#22c55e'>&#10003; OLX conectada!</h2>"
                    "<p style='color:#9ca3af'>Pode fechar esta janela.</p></body></html>")
        except Exception as _e:
            return jsonify({"erro": str(_e)}), 500

    @app.route("/integracoes/olx/oauth/trocar-codigo")
    def olx_trocar_codigo():
        global _olx_token_mem
        code = request.args.get("code", "")
        # aceita redirect_uri como parâmetro para sobrescrever o default
        redirect_uri_override = request.args.get("redirect_uri", "").strip()
        _redir = redirect_uri_override or OLX_REDIRECT_URI
        if not code or not OLX_CLIENT_ID:
            return jsonify({"erro": "code ausente ou OLX nao configurada"}), 400
        try:
            _r = requests.post("https://auth.olx.com.br/oauth/token", data={
                "grant_type": "authorization_code",
                "client_id": OLX_CLIENT_ID,
                "client_secret": OLX_CLIENT_SECRET,
                "code": code,
                "redirect_uri": _redir
            }, timeout=15)
            if _r.status_code != 200:
                return jsonify({"erro": f"OLX {_r.status_code}: {_r.text[:300]}"}), 400
            _d = _r.json()
            _olx_token_mem = {"access_token": _d.get("access_token", ""), "expires_at": time.time() + _d.get("expires_in", 3600)}
            try:
                with open(_OLX_TOKENS_FILE, "w") as _f:
                    json.dump(_olx_token_mem, _f)
            except Exception:
                pass
            return ("<html><body style='font-family:sans-serif;text-align:center;padding:40px;"
                    "background:#0f172a;color:#fff'><h2 style='color:#22c55e'>&#10003; OLX conectada!</h2>"
                    "<p style='color:#9ca3af'>Pode fechar esta janela.</p></body></html>")
        except Exception as _e:
            return jsonify({"erro": str(_e)}), 500

    @app.route("/integracoes/olx/salvar-token", methods=["POST", "OPTIONS"])
    def olx_salvar_token():
        if request.method == "OPTIONS":
            return _options_resp()
        global _olx_token_mem
        data = request.get_json(force=True) or {}
        token = data.get("access_token", "").strip()
        if not token:
            return jsonify({"ok": False, "erro": "access_token ausente"}), 400
        _olx_token_mem = {"access_token": token, "expires_at": time.time() + 3600}
        try:
            with open(_OLX_TOKENS_FILE, "w") as _f:
                json.dump(_olx_token_mem, _f)
        except Exception:
            pass
        return jsonify({"ok": True, "msg": "Token OLX salvo com sucesso"})

    @app.route("/integracoes/olx/publicar", methods=["POST", "OPTIONS"])
    def olx_publicar():
        if request.method == "OPTIONS":
            return _options_resp()
        if not OLX_CLIENT_ID:
            return jsonify({"ok": False, "erro": "OLX nao configurada — informe OLX_CLIENT_ID e OLX_CLIENT_SECRET nas variaveis de ambiente do Railway"}), 501
        global _olx_token_mem
        if not _olx_token_mem.get("access_token"):
            try:
                with open(_OLX_TOKENS_FILE) as _f:
                    _olx_token_mem = json.load(_f)
            except Exception:
                pass
        if not _olx_token_mem.get("access_token"):
            return jsonify({"ok": False, "erro": "OLX nao autorizada — clique em Conectar"}), 401
        data = request.get_json(force=True) or {}
        fotos = [f for f in (data.get("fotos") or data.get("images") or []) if f and f.startswith("http")]
        preco = float(data.get("preco") or data.get("price") or 0)
        if preco < 180:
            return jsonify({"ok": False, "erro": f"OLX exige preco minimo de R$ 180. Valor enviado: R$ {preco:.2f}"}), 400
        _sku = data.get("_sku") or data.get("sku", "")
        payload = {
            "subject": (data.get("subject") or data.get("titulo") or data.get("nomeInterno", "Peca Automotiva"))[:70],
            "body": (data.get("body") or data.get("descricao") or data.get("titulo", ""))[:6000],
            "price": int(preco),
            "category": {"id": "8020"},
            "phone": {"phone": data.get("telefone") or OLX_TELEFONE, "phone_hidden": False},
            "locations": [{"zipcode": (data.get("cep") or OLX_CEP).replace("-", "")}],
            "images": fotos[:10]
        }
        if _sku:
            payload["custom_id"] = str(_sku)
        try:
            _r = requests.put("https://apps.olx.com.br/autoupload/import",
                              headers={"Authorization": f"Bearer {_olx_token_mem['access_token']}", "Content-Type": "application/json"},
                              json={"ad_list": [payload]}, timeout=20)
            if _r.status_code in (200, 201, 202):
                _resp = _r.json()
                _ad = (_resp.get("ad_list") or [{}])[0]
                return jsonify({"ok": True, "status": _ad.get("status", ""), "id": _ad.get("id", ""), "raw": _resp})
            return jsonify({"ok": False, "erro": _r.text[:300]}), _r.status_code
        except Exception as _e:
            return jsonify({"ok": False, "erro": str(_e)}), 500

    # ─── Shopee ───────────────────────────────────────────────────────────────────
    import hmac as _hmac

    _SHOPEE_REDIRECT_URI = os.environ.get(
        "SHOPEE_REDIRECT_URI",
        "https://wrx-api-production.up.railway.app/integracoes/shopee/oauth/callback"
    )
    _SHOPEE_BASE = "https://partner.shopeemobile.com"
    _shopee_tokens_mem = {}
    _SHOPEE_TOKENS_FILE = os.path.join(_INTEG_DIR, "wrx_shopee_tokens.json")

    def _shopee_sign(path, timestamp, access_token="", shop_id=0):
        # Shopee API v2: base = partner_id + path + timestamp [+ access_token + shop_id]
        # access_token e shop_id só entram em chamadas autenticadas (não na URL de auth)
        base = f"{SHOPEE_PARTNER_ID}{path}{timestamp}"
        if access_token:
            base += access_token
        if shop_id:
            base += str(shop_id)
        return _hmac.new(
            SHOPEE_PARTNER_KEY.encode(), base.encode(), _hashlib.sha256
        ).hexdigest()

    def _shopee_load_tokens():
        global _shopee_tokens_mem
        if _shopee_tokens_mem:
            return _shopee_tokens_mem
        # Tenta arquivo local primeiro
        try:
            with open(_SHOPEE_TOKENS_FILE) as _f:
                loaded = json.load(_f)
                if loaded:
                    _shopee_tokens_mem = loaded
                    print(f"[SHOPEE-TOKENS] Carregado do arquivo local. Shops: {list(loaded.keys())}")
                    return _shopee_tokens_mem
        except Exception:
            pass
        # Fallback: Supabase
        jwt = _ph_get_jwt()
        if jwt:
            try:
                _r = requests.get(
                    f"https://{_PH_HOST}/auth/v1/user",
                    headers={"apikey": _PH_ANON, "Authorization": f"Bearer {jwt}"},
                    timeout=10
                )
                if _r.status_code == 200:
                    remote = _r.json().get("user_metadata", {}).get("wrx_shopee_tokens", {})
                    if remote:
                        _shopee_tokens_mem = remote
                        print(f"[SHOPEE-TOKENS] Carregado do Supabase. Shops: {list(remote.keys())}")
                        return _shopee_tokens_mem
            except Exception as e:
                print(f"[SHOPEE-TOKENS] Erro ao carregar do Supabase: {e}")
        print("[SHOPEE-TOKENS] Sem tokens salvos.")
        return _shopee_tokens_mem

    def _shopee_save_tokens(tokens):
        global _shopee_tokens_mem
        _shopee_tokens_mem = tokens
        try:
            with open(_SHOPEE_TOKENS_FILE, "w") as _f:
                json.dump(tokens, _f)
            print(f"[SHOPEE-TOKENS] Salvo localmente. Shops: {list(tokens.keys())}")
        except Exception as e:
            print(f"[SHOPEE-TOKENS] Erro ao salvar local: {e}")
        # Persiste no Supabase
        jwt = _ph_get_jwt()
        if jwt:
            try:
                r = requests.put(
                    f"https://{_PH_HOST}/auth/v1/user",
                    json={"data": {"wrx_shopee_tokens": tokens}},
                    headers={"apikey": _PH_ANON, "Authorization": f"Bearer {jwt}", "Content-Type": "application/json"},
                    timeout=15
                )
                if r.status_code == 200:
                    print(f"[SHOPEE-TOKENS] Supabase: tokens salvos. Shops: {list(tokens.keys())}")
                else:
                    print(f"[SHOPEE-TOKENS] ERRO Supabase: HTTP {r.status_code} — {r.text[:200]}")
            except Exception as e:
                print(f"[SHOPEE-TOKENS] ERRO Supabase (excecao): {e}")

    def _shopee_get_token(shop_id=None):
        tokens = _shopee_load_tokens()
        key = str(shop_id) if shop_id else (list(tokens.keys())[0] if tokens else None)
        if not key:
            return None, None
        t = tokens.get(key, {})
        if not t.get("access_token"):
            print(f"[SHOPEE-TOKENS] Shop '{key}' nao autorizado.")
            return None, None
        # Refresh se expira em menos de 10 min
        if t.get("expires_at", 0) - time.time() < 600:
            print(f"[SHOPEE-TOKENS] Token shop '{key}' expirando. Refresh...")
            ts = int(time.time())
            path = "/api/v2/auth/access_token/get"
            sign = _shopee_sign(path, ts)
            try:
                _r = requests.post(
                    f"{_SHOPEE_BASE}{path}",
                    params={
                        "partner_id": SHOPEE_PARTNER_ID,
                        "timestamp": ts,
                        "sign": sign,
                    },
                    json={
                        "partner_id": SHOPEE_PARTNER_ID,
                        "refresh_token": t.get("refresh_token", ""),
                        "shop_id": int(key),
                    },
                    timeout=15
                )
                if _r.status_code == 200 and not _r.json().get("error"):
                    _d = _r.json()
                    t["access_token"] = _d["access_token"]
                    t["refresh_token"] = _d.get("refresh_token", t["refresh_token"])
                    t["expires_at"] = time.time() + _d.get("expire_in", 14400)
                    tokens[key] = t
                    _shopee_save_tokens(tokens)
                    print(f"[SHOPEE-TOKENS] Token shop '{key}' renovado.")
                else:
                    print(f"[SHOPEE-TOKENS] Refresh falhou: {_r.text[:200]}")
                    return None, None
            except Exception as e:
                print(f"[SHOPEE-TOKENS] Erro no refresh: {e}")
                return None, None
        return t.get("access_token"), int(key)

    @app.route("/integracoes/shopee/config", methods=["GET", "OPTIONS"])
    def shopee_config():
        if request.method == "OPTIONS":
            return _options_resp()
        tokens = _shopee_load_tokens()
        em_sandbox = SHOPEE_PARTNER_ID == 1234546
        ts = int(time.time())
        path = "/api/v2/shop/auth_partner"
        sign = _shopee_sign(path, ts)
        auth_url = (
            f"{_SHOPEE_BASE}{path}"
            f"?partner_id={SHOPEE_PARTNER_ID}"
            f"&timestamp={ts}"
            f"&sign={sign}"
            f"&redirect={_urlparse.quote(_SHOPEE_REDIRECT_URI, safe='')}"
        )
        return jsonify({
            "configured": True,
            "partner_id": SHOPEE_PARTNER_ID,
            "modo": "sandbox" if em_sandbox else "producao",
            "tokenSaved": bool(tokens),
            "shops": list(tokens.keys()),
            "authUrl": auth_url,
            "aviso": "Credenciais sandbox. Configure SHOPEE_PARTNER_ID e SHOPEE_PARTNER_KEY reais no Railway para publicar em producao." if em_sandbox else None,
        })

    @app.route("/integracoes/shopee/oauth")
    def shopee_oauth():
        from flask import redirect as _redir
        ts = int(time.time())
        path = "/api/v2/shop/auth_partner"
        sign = _shopee_sign(path, ts)
        url = (
            f"{_SHOPEE_BASE}{path}"
            f"?partner_id={SHOPEE_PARTNER_ID}"
            f"&timestamp={ts}"
            f"&sign={sign}"
            f"&redirect={_urlparse.quote(_SHOPEE_REDIRECT_URI, safe='')}"
        )
        return _redir(url)

    @app.route("/integracoes/shopee/oauth/callback")
    def shopee_oauth_callback():
        code = request.args.get("code", "")
        shop_id = request.args.get("shop_id", "")
        if not code or not shop_id:
            return jsonify({"erro": "code ou shop_id ausente"}), 400
        ts = int(time.time())
        path = "/api/v2/auth/token/get"
        sign = _shopee_sign(path, ts)
        try:
            _r = requests.post(
                f"{_SHOPEE_BASE}{path}",
                params={
                    "partner_id": SHOPEE_PARTNER_ID,
                    "timestamp": ts,
                    "sign": sign,
                },
                json={
                    "code": code,
                    "shop_id": int(shop_id),
                    "partner_id": SHOPEE_PARTNER_ID,
                },
                timeout=15
            )
            if _r.status_code != 200 or _r.json().get("error"):
                return jsonify({"erro": f"Shopee {_r.status_code}: {_r.text[:300]}"}), 400
            _d = _r.json()
            tokens = _shopee_load_tokens()
            tokens[str(shop_id)] = {
                "access_token": _d["access_token"],
                "refresh_token": _d.get("refresh_token", ""),
                "expires_at": time.time() + _d.get("expire_in", 14400),
                "shop_id": int(shop_id),
            }
            _shopee_save_tokens(tokens)
            print(f"[SHOPEE-OAUTH] Shop '{shop_id}' autorizado.")
            return (
                "<html><body style='font-family:sans-serif;text-align:center;padding:40px;"
                "background:#0f172a;color:#fff'>"
                "<h2 style='color:#ee4d2d'>&#10003; Shopee conectada!</h2>"
                f"<p>Shop ID: <strong>{shop_id}</strong></p>"
                "<p style='color:#9ca3af'>Pode fechar esta janela.</p>"
                "</body></html>"
            )
        except Exception as _e:
            return jsonify({"erro": str(_e)}), 500

    # Itens de segurança que devem ser sempre publicados como NOVO na Shopee
    import re as _re
    _SHOPEE_SOMENTE_NOVO = _re.compile(
        r"airbag|air.?bag|cinto[\s\w]{0,5}segur|cinto seguranca|freio|pastilha|disco.?fre|pinça|cilindro.?mestre"
        r"|bomba.?fre|sensor.?abs|modulo.?abs|amortecedor|bandeja|suspensao|mola.?suspens"
        r"|pivo|cubo.?roda|manga.?eixo|caixa.?direcao|coluna.?direcao|bomba.?direcao"
        r"|hidrovacuo|servo.?fre|abs|veiculo.?freio",
        _re.IGNORECASE
    )
    _SHOPEE_CAT_SOMENTE_NOVO = _re.compile(
        r"freio|abs|suspensao|amortecedor|direcao|airbag|seguranca|cinto",
        _re.IGNORECASE
    )

    def _shopee_item_somente_novo(nome, categoria=""):
        if _SHOPEE_SOMENTE_NOVO.search(nome):
            excecoes = _re.compile(r"tablier|suporte.?airbag|capa|cobertura|suporte", _re.IGNORECASE)
            if not excecoes.search(nome):
                return True
        if _SHOPEE_CAT_SOMENTE_NOVO.search(categoria):
            return True
        return False

    @app.route("/integracoes/shopee/verificar-seguranca", methods=["POST", "OPTIONS"])
    def shopee_verificar_seguranca():
        if request.method == "OPTIONS":
            return _options_resp()
        data = request.get_json(force=True) or {}
        titulo = data.get("titulo", "")
        categoria = data.get("categoria", "")
        condicao = data.get("condicao", "used")
        somente_novo = _shopee_item_somente_novo(titulo, categoria)
        condicao_final = "new" if somente_novo else condicao
        return jsonify({
            "titulo": titulo,
            "condicao_enviada": condicao,
            "condicao_aplicada": condicao_final,
            "somente_novo_por_seguranca": somente_novo,
            "mensagem": "Item de segurança: será publicado como NOVO mesmo que selecionado USADO." if somente_novo and condicao == "used" else "Condição mantida conforme selecionado.",
        })

    @app.route("/integracoes/shopee/publicar", methods=["POST", "OPTIONS"])
    def shopee_publicar():
        if request.method == "OPTIONS":
            return _options_resp()
        data = request.get_json(force=True) or {}
        tokens = _shopee_load_tokens()
        em_sandbox = SHOPEE_PARTNER_ID == 1234546
        if not tokens:
            if em_sandbox:
                return jsonify({"ok": False, "erro": "Shopee em modo sandbox. Configure SHOPEE_PARTNER_ID e SHOPEE_PARTNER_KEY reais no Railway e reautorize."}), 401
            return jsonify({"ok": False, "erro": "Shopee nao autorizada.", "authUrl": f"{request.host_url}integracoes/shopee/oauth"}), 401

        sku = data.get("sku", "")
        titulo = data.get("titulo", "")
        preco = data.get("preco", 0)
        descricao = data.get("descricao", titulo)
        condicao_recebida = data.get("condicao", "new")
        categoria = data.get("categoria", "")
        fotos = data.get("fotos", [])
        if not sku or not titulo:
            return jsonify({"ok": False, "erro": "sku e titulo sao obrigatorios"}), 400

        # Regra de segurança: forçar NOVO independente do que o frontend enviou
        somente_novo = _shopee_item_somente_novo(titulo, categoria)
        condicao_final = "new" if somente_novo else condicao_recebida
        condicao_shopee = "NEW" if condicao_final == "new" else "USED"
        if somente_novo and condicao_recebida == "used":
            print(f"[SHOPEE] Item de segurança '{titulo}' convertido para NOVO automaticamente.")

        # Publica em todos os shops autorizados
        shop_id_param = data.get("shop_id")
        shop_ids = [str(shop_id_param)] if shop_id_param else list(tokens.keys())
        resultados = []
        erros = []
        for sid in shop_ids:
            access_token, shop_id_int = _shopee_get_token(sid)
            if not access_token:
                erros.append(f"shop {sid}: token invalido")
                continue
            ts = int(time.time())
            path = "/api/v2/product/add_item"
            sign = _shopee_sign(path, ts, access_token, shop_id_int)
            payload_shopee = {
                "partner_id": SHOPEE_PARTNER_ID,
                "shop_id": shop_id_int,
                "sign": sign,
                "timestamp": ts,
                "access_token": access_token,
                "item_name": titulo[:120],
                "description": descricao[:2000],
                "price_info": [{"currency": "BRL", "original_price": float(preco)}],
                "stock_info_v2": {"seller_stock": [{"stock": 1}]},
                "condition": condicao_shopee,
                "category_id": 100644,
                "image": {"image_url_list": fotos[:9]},
                "logistics_info": [{"logistic_id": 10038, "enabled": True}],
                "weight": 1.0,
            }
            try:
                _r = requests.post(
                    f"{_SHOPEE_BASE}{path}",
                    params={"partner_id": SHOPEE_PARTNER_ID, "timestamp": ts, "access_token": access_token, "shop_id": shop_id_int, "sign": sign},
                    json=payload_shopee,
                    timeout=20
                )
                _d = _r.json()
                if _r.status_code == 200 and not _d.get("error"):
                    item_id = _d.get("response", {}).get("item_id", "")
                    print(f"[SHOPEE] SKU '{sku}' publicado no shop {sid}. item_id={item_id}, condicao={condicao_shopee}")
                    resultados.append({"shop_id": sid, "item_id": item_id, "condicao": condicao_shopee})
                else:
                    msg = _d.get("message", _r.text[:200])
                    print(f"[SHOPEE] Erro shop {sid} SKU '{sku}': {msg}")
                    erros.append(f"shop {sid}: {msg}")
            except Exception as _e:
                erros.append(f"shop {sid}: {str(_e)}")

        if not resultados and erros:
            return jsonify({"ok": False, "erro": " | ".join(erros)}), 400
        item_id = resultados[0]["item_id"] if resultados else ""
        return jsonify({
            "ok": True,
            "item_id": item_id,
            "sku": sku,
            "shops": resultados,
            "erros": erros,
            "condicao_aplicada": condicao_shopee,
            "somente_novo_por_seguranca": somente_novo,
        })

    # ── Shopee: helpers de listagem e detalhes ───────────────────────────────────

    def _shopee_list_items_all(access_token, shop_id_int, status="NORMAL"):
        items = []
        offset = 0
        for _ in range(50):
            ts = int(time.time())
            path = "/api/v2/product/get_item_list"
            sign = _shopee_sign(path, ts, access_token, shop_id_int)
            r = requests.get(
                f"{_SHOPEE_BASE}{path}",
                params={"partner_id": SHOPEE_PARTNER_ID, "timestamp": ts,
                        "access_token": access_token, "shop_id": shop_id_int,
                        "sign": sign, "offset": offset, "page_size": 100,
                        "item_status": status},
                timeout=20
            )
            d = r.json()
            if d.get("error") or r.status_code != 200:
                print(f"[SHOPEE-LIST] Erro: {d.get('message', r.text[:200])}")
                break
            resp = d.get("response", {})
            items.extend(resp.get("item", []))
            if not resp.get("has_next_page", False):
                break
            offset = resp.get("next_offset", offset + 100)
        return items

    def _shopee_get_item_details(access_token, shop_id_int, item_ids):
        results = []
        for i in range(0, len(item_ids), 50):
            lote = item_ids[i:i+50]
            ts = int(time.time())
            path = "/api/v2/product/get_item_base_info"
            sign = _shopee_sign(path, ts, access_token, shop_id_int)
            r = requests.get(
                f"{_SHOPEE_BASE}{path}",
                params={"partner_id": SHOPEE_PARTNER_ID, "timestamp": ts,
                        "access_token": access_token, "shop_id": shop_id_int,
                        "sign": sign, "item_id_list": ",".join(str(x) for x in lote)},
                timeout=30
            )
            d = r.json()
            if r.status_code == 200 and not d.get("error"):
                results.extend(d.get("response", {}).get("item_list", []))
        return results

    def _shopee_extract_sku(item):
        sku = (item.get("seller_sku") or "").strip()
        if sku:
            return sku
        for attr in item.get("attribute_list", []):
            if attr.get("attribute_name", "").lower() in ("seller sku", "sku"):
                vals = attr.get("attribute_value_list", [])
                if vals:
                    return vals[0].get("value", "").strip()
        return ""

    def _shopee_extract_preco(item):
        pi = item.get("price_info", [])
        return float((pi[0].get("current_price") or pi[0].get("original_price") or 0) if pi else 0)

    def _shopee_extract_estoque(item):
        ss = item.get("stock_info_v2", {}).get("seller_stock", [{}])
        return int(ss[0].get("stock", 0)) if ss else 0

    # ── Shopee: listar anúncios ativos direto da API ──────────────────────────────

    @app.route("/integracoes/shopee/listar-anuncios", methods=["GET", "OPTIONS"])
    def shopee_listar_anuncios():
        if request.method == "OPTIONS":
            return _options_resp()
        tokens = _shopee_load_tokens()
        if not tokens:
            return jsonify({"ok": False, "erro": "Shopee nao autorizada"}), 401
        shop_id_param = request.args.get("shop_id")
        shop_ids = [str(shop_id_param)] if shop_id_param else list(tokens.keys())
        todos = []
        for sid in shop_ids:
            access_token, shop_id_int = _shopee_get_token(sid)
            if not access_token:
                continue
            item_refs = _shopee_list_items_all(access_token, shop_id_int, status="NORMAL")
            item_ids = [x["item_id"] for x in item_refs if x.get("item_id")]
            if not item_ids:
                continue
            for item in _shopee_get_item_details(access_token, shop_id_int, item_ids):
                todos.append({
                    "shop_id": sid,
                    "item_id": item.get("item_id"),
                    "titulo": item.get("item_name", ""),
                    "sku": _shopee_extract_sku(item),
                    "preco": _shopee_extract_preco(item),
                    "estoque": _shopee_extract_estoque(item),
                    "status": item.get("item_status", "NORMAL"),
                    "fotos": item.get("image", {}).get("image_url_list", [])[:3],
                })
        return jsonify({"ok": True, "total": len(todos), "itens": todos})

    # ── Shopee: cache local (arquivo JSON) + Supabase opcional ───────────────────

    _SHOPEE_CACHE_FILE = os.path.join(_INTEG_DIR, "wrx_shopee_anuncios.json")
    _shopee_anuncios_mem = []

    def _shopee_cache_load():
        global _shopee_anuncios_mem
        if _shopee_anuncios_mem:
            return _shopee_anuncios_mem
        try:
            with open(_SHOPEE_CACHE_FILE, encoding="utf-8") as f:
                _shopee_anuncios_mem = json.load(f)
        except Exception:
            _shopee_anuncios_mem = []
        return _shopee_anuncios_mem

    def _shopee_cache_save(itens):
        global _shopee_anuncios_mem
        _shopee_anuncios_mem = itens
        try:
            with open(_SHOPEE_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(itens, f, ensure_ascii=False)
        except Exception as e:
            print(f"[SHOPEE-CACHE] Aviso: nao salvou arquivo ({e})")
        # Tenta Supabase silenciosamente (sem parar se a tabela nao existir)
        try:
            for i in range(0, len(itens), 100):
                lote = itens[i:i+100]
                r_up = requests.post(
                    f"{_WRX_SB_URL}/rest/v1/shopee_anuncios",
                    headers={**_wrx_headers(), "Prefer": "resolution=merge-duplicates"},
                    json=lote, timeout=30
                )
                if r_up.status_code not in (200, 201, 204):
                    break  # tabela nao existe, para silenciosamente
        except Exception:
            pass

    @app.route("/integracoes/shopee/sincronizar", methods=["POST", "OPTIONS"])
    def shopee_sincronizar():
        if request.method == "OPTIONS":
            return _options_resp()
        tokens = _shopee_load_tokens()
        if not tokens:
            return jsonify({"ok": False, "erro": "Shopee nao autorizada"}), 401
        data = request.get_json(force=True) or {}
        shop_id_param = data.get("shop_id")
        shop_ids = [str(shop_id_param)] if shop_id_param else list(tokens.keys())

        r_pecas = requests.get(
            f"{_WRX_SB_URL}/rest/v1/pecas_estoque?select=sku&limit=20000",
            headers=_wrx_headers(), timeout=15
        )
        skus_sistema = set()
        if r_pecas.status_code == 200:
            for p in r_pecas.json():
                if p.get("sku"):
                    skus_sistema.add(str(p["sku"]).strip().upper())

        total_importados = 0
        total_vinculados = 0
        erros = []
        todos_itens = []

        for sid in shop_ids:
            access_token, shop_id_int = _shopee_get_token(sid)
            if not access_token:
                erros.append(f"shop {sid}: token invalido")
                continue
            print(f"[SHOPEE-SYNC] Listando shop {sid}...")
            item_refs = _shopee_list_items_all(access_token, shop_id_int, status="NORMAL")
            item_ids = [x["item_id"] for x in item_refs if x.get("item_id")]
            if not item_ids:
                continue
            detalhes = _shopee_get_item_details(access_token, shop_id_int, item_ids)
            print(f"[SHOPEE-SYNC] Shop {sid}: {len(detalhes)} itens")
            now_iso = _datetime.utcnow().isoformat() + "Z"
            for item in detalhes:
                est = _shopee_extract_estoque(item)
                if est <= 0:
                    continue
                sku_raw = _shopee_extract_sku(item)
                sku_up = sku_raw.upper() if sku_raw else ""
                sku_vinculado = sku_raw if sku_up in skus_sistema else None
                if sku_vinculado:
                    total_vinculados += 1
                fotos = item.get("image", {}).get("image_url_list", [])[:9]
                todos_itens.append({
                    "shop_id": str(sid),
                    "item_id": str(item.get("item_id", "")),
                    "sku": sku_vinculado or sku_raw,
                    "titulo": (item.get("item_name") or "")[:500],
                    "preco": _shopee_extract_preco(item),
                    "estoque": est,
                    "status": item.get("item_status", "NORMAL"),
                    "fotos": json.dumps(fotos),
                    "sync_at": now_iso,
                })
            total_importados += len(todos_itens)

        _shopee_cache_save(todos_itens)
        return jsonify({"ok": True, "importados": total_importados, "vinculados": total_vinculados, "erros": erros})

    @app.route("/integracoes/shopee/anuncios-db", methods=["GET", "OPTIONS"])
    def shopee_anuncios_db():
        if request.method == "OPTIONS":
            return _options_resp()
        # Tenta Supabase primeiro
        try:
            r = requests.get(
                f"{_WRX_SB_URL}/rest/v1/shopee_anuncios?select=*&order=sync_at.desc&limit=5000",
                headers=_wrx_headers(), timeout=10
            )
            if r.status_code == 200:
                dados = r.json()
                return jsonify({"ok": True, "itens": dados, "total": len(dados), "fonte": "supabase"})
        except Exception:
            pass
        # Fallback: cache local
        dados = _shopee_cache_load()
        return jsonify({"ok": True, "itens": dados, "total": len(dados), "fonte": "cache"})

    # ── Shopee: atualizar estoque ─────────────────────────────────────────────────

    @app.route("/integracoes/shopee/atualizar-estoque", methods=["POST", "OPTIONS"])
    def shopee_atualizar_estoque():
        if request.method == "OPTIONS":
            return _options_resp()
        tokens = _shopee_load_tokens()
        if not tokens:
            return jsonify({"ok": False, "erro": "Shopee nao autorizada"}), 401
        data = request.get_json(force=True) or {}
        item_id = data.get("item_id")
        shop_id = str(data.get("shop_id", ""))
        estoque = int(data.get("estoque", 0))
        sku = data.get("sku", "")
        if not item_id or not shop_id:
            return jsonify({"ok": False, "erro": "item_id e shop_id obrigatorios"}), 400
        access_token, shop_id_int = _shopee_get_token(shop_id)
        if not access_token:
            return jsonify({"ok": False, "erro": "token invalido"}), 401
        ts = int(time.time())
        path = "/api/v2/product/update_stock"
        sign = _shopee_sign(path, ts, access_token, shop_id_int)
        r = requests.post(
            f"{_SHOPEE_BASE}{path}",
            params={"partner_id": SHOPEE_PARTNER_ID, "timestamp": ts,
                    "access_token": access_token, "shop_id": shop_id_int, "sign": sign},
            json={"item_id": int(item_id), "stock_list": [{"model_id": 0, "seller_stock": [{"stock": max(0, estoque)}]}]},
            timeout=20
        )
        d = r.json()
        ok = r.status_code == 200 and not d.get("error")
        print(f"[SHOPEE-ESTOQUE] item {item_id} SKU '{sku}' -> {estoque}: {'OK' if ok else d.get('message','?')}")
        if ok:
            requests.patch(
                f"{_WRX_SB_URL}/rest/v1/shopee_anuncios?shop_id=eq.{shop_id}&item_id=eq.{item_id}",
                headers=_wrx_headers(),
                json={"estoque": estoque, "sync_at": _datetime.utcnow().isoformat() + "Z"},
                timeout=10
            )
        return jsonify({"ok": ok, "erro": d.get("message", "") if not ok else ""})

    # ── Shopee: atualizar preço ────────────────────────────────────────────────────

    @app.route("/integracoes/shopee/atualizar-preco", methods=["POST", "OPTIONS"])
    def shopee_atualizar_preco():
        if request.method == "OPTIONS":
            return _options_resp()
        tokens = _shopee_load_tokens()
        if not tokens:
            return jsonify({"ok": False, "erro": "Shopee nao autorizada"}), 401
        data = request.get_json(force=True) or {}
        item_id = data.get("item_id")
        shop_id = str(data.get("shop_id", ""))
        preco = float(data.get("preco", 0))
        if not item_id or not shop_id:
            return jsonify({"ok": False, "erro": "item_id e shop_id obrigatorios"}), 400
        access_token, shop_id_int = _shopee_get_token(shop_id)
        if not access_token:
            return jsonify({"ok": False, "erro": "token invalido"}), 401
        ts = int(time.time())
        path = "/api/v2/product/update_price"
        sign = _shopee_sign(path, ts, access_token, shop_id_int)
        r = requests.post(
            f"{_SHOPEE_BASE}{path}",
            params={"partner_id": SHOPEE_PARTNER_ID, "timestamp": ts,
                    "access_token": access_token, "shop_id": shop_id_int, "sign": sign},
            json={"item_id": int(item_id), "price_list": [{"model_id": 0, "original_price": preco}]},
            timeout=20
        )
        d = r.json()
        ok = r.status_code == 200 and not d.get("error")
        if ok:
            requests.patch(
                f"{_WRX_SB_URL}/rest/v1/shopee_anuncios?shop_id=eq.{shop_id}&item_id=eq.{item_id}",
                headers=_wrx_headers(),
                json={"preco": preco, "sync_at": _datetime.utcnow().isoformat() + "Z"},
                timeout=10
            )
        return jsonify({"ok": ok, "erro": d.get("message", "") if not ok else ""})

    # ── Shopee: pausar/despausar item ─────────────────────────────────────────────

    @app.route("/integracoes/shopee/pausar-item", methods=["POST", "OPTIONS"])
    def shopee_pausar_item():
        if request.method == "OPTIONS":
            return _options_resp()
        tokens = _shopee_load_tokens()
        if not tokens:
            return jsonify({"ok": False, "erro": "Shopee nao autorizada"}), 401
        data = request.get_json(force=True) or {}
        item_id = data.get("item_id")
        shop_id = str(data.get("shop_id", ""))
        pausar = data.get("pausar", True)
        if not item_id or not shop_id:
            return jsonify({"ok": False, "erro": "item_id e shop_id obrigatorios"}), 400
        access_token, shop_id_int = _shopee_get_token(shop_id)
        if not access_token:
            return jsonify({"ok": False, "erro": "token invalido"}), 401
        ts = int(time.time())
        path = "/api/v2/product/unlist_item"
        sign = _shopee_sign(path, ts, access_token, shop_id_int)
        r = requests.post(
            f"{_SHOPEE_BASE}{path}",
            params={"partner_id": SHOPEE_PARTNER_ID, "timestamp": ts,
                    "access_token": access_token, "shop_id": shop_id_int, "sign": sign},
            json={"item_list": [{"item_id": int(item_id), "unlist": bool(pausar)}]},
            timeout=20
        )
        d = r.json()
        ok = r.status_code == 200 and not d.get("error")
        novo_status = "UNLIST" if pausar else "NORMAL"
        if ok:
            requests.patch(
                f"{_WRX_SB_URL}/rest/v1/shopee_anuncios?shop_id=eq.{shop_id}&item_id=eq.{item_id}",
                headers=_wrx_headers(),
                json={"status": novo_status, "sync_at": _datetime.utcnow().isoformat() + "Z"},
                timeout=10
            )
        return jsonify({"ok": ok, "status": novo_status if ok else None, "erro": d.get("message", "") if not ok else ""})

    # ── Shopee: vendas (pedidos) ──────────────────────────────────────────────────

    @app.route("/integracoes/shopee/vendas", methods=["GET", "OPTIONS"])
    def shopee_vendas():
        if request.method == "OPTIONS":
            return _options_resp()
        tokens = _shopee_load_tokens()
        if not tokens:
            return jsonify({"ok": False, "erro": "Shopee nao autorizada"}), 401
        shop_id_param = request.args.get("shop_id")
        dias = int(request.args.get("dias", "7"))
        shop_ids = [str(shop_id_param)] if shop_id_param else list(tokens.keys())
        todas = []
        for sid in shop_ids:
            access_token, shop_id_int = _shopee_get_token(sid)
            if not access_token:
                continue
            time_to = int(time.time())
            time_from = time_to - dias * 86400
            ts = int(time.time())
            path = "/api/v2/order/get_order_list"
            sign = _shopee_sign(path, ts, access_token, shop_id_int)
            r = requests.get(
                f"{_SHOPEE_BASE}{path}",
                params={"partner_id": SHOPEE_PARTNER_ID, "timestamp": ts,
                        "access_token": access_token, "shop_id": shop_id_int, "sign": sign,
                        "time_range_field": "create_time", "time_from": time_from,
                        "time_to": time_to, "page_size": 100},
                timeout=20
            )
            d = r.json()
            if r.status_code == 200 and not d.get("error"):
                for o in d.get("response", {}).get("order_list", []):
                    todas.append({
                        "marketplace": "shopee",
                        "shop_id": sid,
                        "order_sn": o.get("order_sn", ""),
                        "status": o.get("order_status", ""),
                        "criar_em": o.get("create_time", 0),
                        "total": o.get("total_amount", 0),
                        "itens": o.get("item_list", []),
                    })
        todas.sort(key=lambda x: x.get("criar_em", 0), reverse=True)
        return jsonify({"ok": True, "vendas": todas, "total": len(todas)})

    # ── Shopee: mensagens (chat) ──────────────────────────────────────────────────

    @app.route("/integracoes/shopee/mensagens", methods=["GET", "OPTIONS"])
    def shopee_mensagens():
        if request.method == "OPTIONS":
            return _options_resp()
        tokens = _shopee_load_tokens()
        if not tokens:
            return jsonify({"ok": False, "erro": "Shopee nao autorizada"}), 401
        shop_id_param = request.args.get("shop_id")
        shop_ids = [str(shop_id_param)] if shop_id_param else list(tokens.keys())
        todas = []
        for sid in shop_ids:
            access_token, shop_id_int = _shopee_get_token(sid)
            if not access_token:
                continue
            ts = int(time.time())
            path = "/api/v2/sellerchat/get_conversation_list"
            sign = _shopee_sign(path, ts, access_token, shop_id_int)
            r = requests.get(
                f"{_SHOPEE_BASE}{path}",
                params={"partner_id": SHOPEE_PARTNER_ID, "timestamp": ts,
                        "access_token": access_token, "shop_id": shop_id_int,
                        "sign": sign, "filter": "all", "page_size": 25},
                timeout=20
            )
            d = r.json()
            if r.status_code == 200 and not d.get("error"):
                for c in d.get("response", {}).get("conversations", []):
                    todas.append({
                        "marketplace": "shopee",
                        "shop_id": sid,
                        "conversation_id": c.get("conversation_id", ""),
                        "comprador": c.get("to_name", ""),
                        "ultima_msg": c.get("last_message", {}).get("content", {}).get("text", ""),
                        "nao_lidas": c.get("unread_count", 0),
                        "timestamp": c.get("last_message", {}).get("created_timestamp", 0),
                    })
        todas.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
        return jsonify({"ok": True, "mensagens": todas, "total": len(todas)})

    # ── Shopee: webhook push notifications ───────────────────────────────────────

    @app.route("/integracoes/shopee/webhook", methods=["POST", "OPTIONS"])
    def shopee_webhook():
        if request.method == "OPTIONS":
            return _options_resp()
        data = request.get_json(force=True) or {}
        code = data.get("code", 0)
        shop_id = str(data.get("shop_id", ""))
        print(f"[SHOPEE-WEBHOOK] code={code} shop={shop_id}")
        if code == 3:
            order_status = data.get("data", {}).get("status", "")
            order_sn = data.get("data", {}).get("ordersn", "")
            if order_status in ("READY_TO_SHIP", "COMPLETED"):
                try:
                    _shopee_processar_venda_webhook(shop_id, order_sn)
                except Exception as _we:
                    print(f"[SHOPEE-WEBHOOK] Erro: {_we}")
        return jsonify({"ok": True})

    def _shopee_processar_venda_webhook(shop_id, order_sn):
        access_token, shop_id_int = _shopee_get_token(str(shop_id))
        if not access_token:
            return
        ts = int(time.time())
        path = "/api/v2/order/get_order_detail"
        sign = _shopee_sign(path, ts, access_token, shop_id_int)
        r = requests.get(
            f"{_SHOPEE_BASE}{path}",
            params={"partner_id": SHOPEE_PARTNER_ID, "timestamp": ts,
                    "access_token": access_token, "shop_id": shop_id_int,
                    "sign": sign, "order_sn_list": order_sn,
                    "response_optional_fields": "item_list"},
            timeout=20
        )
        d = r.json()
        if r.status_code != 200 or d.get("error"):
            return
        for order in d.get("response", {}).get("order_list", []):
            for item in order.get("item_list", []):
                sku = (item.get("model_sku") or "").strip()
                qty = int(item.get("model_quantity_purchased", 1))
                if not sku:
                    continue
                r_p = requests.get(
                    f"{_WRX_SB_URL}/rest/v1/pecas_estoque?sku=eq.{sku}&select=sku,qtd",
                    headers=_wrx_headers(), timeout=10
                )
                if r_p.status_code == 200 and r_p.json():
                    peca = r_p.json()[0]
                    nova_qtd = max(0, int(peca.get("qtd", 0)) - qty)
                    requests.patch(
                        f"{_WRX_SB_URL}/rest/v1/pecas_estoque?sku=eq.{sku}",
                        headers=_wrx_headers(),
                        json={"qtd": nova_qtd, "atualizado": _datetime.utcnow().isoformat() + "Z"},
                        timeout=10
                    )
                    print(f"[SHOPEE-WEBHOOK] SKU {sku}: {peca.get('qtd')} -> {nova_qtd}")
                    if nova_qtd == 0:
                        _shopee_pausar_por_sku(sku)

    def _shopee_pausar_por_sku(sku):
        tokens = _shopee_load_tokens()
        if not tokens:
            return
        r = requests.get(
            f"{_WRX_SB_URL}/rest/v1/shopee_anuncios?sku=eq.{sku}&select=shop_id,item_id",
            headers=_wrx_headers(), timeout=10
        )
        if r.status_code != 200:
            return
        for row in r.json():
            sid = row.get("shop_id", "")
            iid = row.get("item_id", "")
            if not sid or not iid:
                continue
            access_token, shop_id_int = _shopee_get_token(sid)
            if not access_token:
                continue
            ts = int(time.time())
            path = "/api/v2/product/unlist_item"
            sign = _shopee_sign(path, ts, access_token, shop_id_int)
            requests.post(
                f"{_SHOPEE_BASE}{path}",
                params={"partner_id": SHOPEE_PARTNER_ID, "timestamp": ts,
                        "access_token": access_token, "shop_id": shop_id_int, "sign": sign},
                json={"item_list": [{"item_id": int(iid), "unlist": True}]},
                timeout=15
            )
            print(f"[SHOPEE] SKU {sku} pausado shop {sid}")

    # ── Shopee: venda manual (cascata) ───────────────────────────────────────────

    @app.route("/integracoes/shopee/venda-cascata", methods=["POST", "OPTIONS"])
    def shopee_venda_cascata():
        if request.method == "OPTIONS":
            return _options_resp()
        data = request.get_json(force=True) or {}
        sku = (data.get("sku") or "").strip()
        qty = int(data.get("qty", 1))
        if not sku:
            return jsonify({"ok": False, "erro": "sku obrigatorio"}), 400
        r_p = requests.get(
            f"{_WRX_SB_URL}/rest/v1/pecas_estoque?sku=eq.{sku}&select=sku,qtd",
            headers=_wrx_headers(), timeout=10
        )
        if r_p.status_code != 200 or not r_p.json():
            return jsonify({"ok": False, "erro": f"SKU {sku} nao encontrado"}), 404
        peca = r_p.json()[0]
        nova_qtd = max(0, int(peca.get("qtd", 0)) - qty)
        requests.patch(
            f"{_WRX_SB_URL}/rest/v1/pecas_estoque?sku=eq.{sku}",
            headers=_wrx_headers(),
            json={"qtd": nova_qtd, "atualizado": _datetime.utcnow().isoformat() + "Z"},
            timeout=10
        )
        if nova_qtd == 0:
            _shopee_pausar_por_sku(sku)
        return jsonify({"ok": True, "sku": sku, "qtd_anterior": peca.get("qtd"), "qtd_nova": nova_qtd, "zerado": nova_qtd == 0})

    # ── Shopee: criar tabela shopee_anuncios (requer service_role) ────────────────

    @app.route("/integracoes/shopee/setup-tabela", methods=["POST", "GET", "OPTIONS"])
    def shopee_setup_tabela():
        if request.method == "OPTIONS":
            return _options_resp()
        # Tenta criar via SQL usando service_role se disponível
        service_key = os.environ.get("SUPABASE_SERVICE_KEY", "")
        sql = """
CREATE TABLE IF NOT EXISTS shopee_anuncios (
  id BIGSERIAL PRIMARY KEY,
  shop_id TEXT NOT NULL,
  item_id TEXT NOT NULL,
  sku TEXT,
  titulo TEXT,
  preco NUMERIC(10,2) DEFAULT 0,
  estoque INTEGER DEFAULT 0,
  status TEXT DEFAULT 'NORMAL',
  fotos JSONB DEFAULT '[]',
  sync_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(shop_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_shopee_anuncios_sku ON shopee_anuncios(sku);
"""
        if service_key:
            r = requests.post(
                f"{_WRX_SB_URL}/rest/v1/rpc/exec",
                headers={"apikey": service_key, "Authorization": f"Bearer {service_key}", "Content-Type": "application/json"},
                json={"sql": sql}, timeout=15
            )
            if r.status_code in (200, 201, 204):
                return jsonify({"ok": True, "msg": "Tabela shopee_anuncios criada com sucesso"})
        # Sem service_role: retorna SQL para execução manual
        return jsonify({
            "ok": False,
            "msg": "Configure SUPABASE_SERVICE_KEY no Railway, ou execute o SQL abaixo no Supabase Dashboard > SQL Editor",
            "sql": sql.strip()
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
