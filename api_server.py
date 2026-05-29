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

# ─── IA: prompt + ajuste + chamada ────────────────────────────────────────────
def _build_prompt(codigo, titles, prices, compatibilidade_oem=None,
                  nome_peca_confirmado=None):
    tlist = "\n".join(f"- {t}" for t in titles) if titles else "(nenhum anúncio coletado)"
    plist = ", ".join(f"R$ {p:.2f}" for p in prices) if prices else "(sem preços coletados — estime com base em concorrentes reais)"

    if compatibilidade_oem:
        linhas_oem = "\n".join(
            f"  {i+1}. {c.get('veiculo','?')} | anos: {c.get('anos','?')}"
            for i, c in enumerate(compatibilidade_oem)
        )
        bloco_compat = (
            "╔══════════════════════════════════════════╗\n"
            "║  COMPATIBILIDADE OEM CONFIRMADA          ║\n"
            "╚══════════════════════════════════════════╝\n\n"
            "Fonte: catálogo oficial / base interna validada.\n\n"
            f"{linhas_oem}\n\n"
            "REGRA ABSOLUTA: use SOMENTE estes veículos em 'compatibilidades_confirmadas'.\n"
            "PROIBIDO adicionar qualquer outro veículo — mesmo que apareça em anúncios do ML,\n"
            "títulos, descrições ou seja sugerido por SEO, IA ou inferência própria.\n"
        )
        regra_compat_json = (
            "- compatibilidades_confirmadas: SOMENTE os veículos da lista OEM acima, sem adicionar nem remover"
        )
    else:
        bloco_compat = (
            "╔══════════════════════════════════════════╗\n"
            "║  SEM COMPATIBILIDADE OEM FORNECIDA       ║\n"
            "╚══════════════════════════════════════════╝\n\n"
            "Nenhuma compatibilidade confirmada foi recebida para este código.\n"
            "REGRA ABSOLUTA: NÃO deduza compatibilidade a partir de:\n"
            "  - anúncios do Mercado Livre\n"
            "  - títulos ou descrições de vendedores\n"
            "  - SEO, palavras semelhantes, modelos parecidos\n"
            "  - conhecimento geral ou inferência própria\n\n"
            "Se não há confirmação OEM, retorne 'compatibilidades_confirmadas' como lista vazia.\n"
        )
        regra_compat_json = (
            "- compatibilidades_confirmadas: lista VAZIA [] quando não há confirmação OEM"
        )

    if nome_peca_confirmado:
        bloco_nome = (
            "╔══════════════════════════════════════════╗\n"
            "║  NOME DA PEÇA CONFIRMADO POR OEM EXATO   ║\n"
            "╚══════════════════════════════════════════╝\n\n"
            f"Nome confirmado pelo Mercado Livre via correspondência OEM exata:\n"
            f"  → {nome_peca_confirmado}\n\n"
            "REGRA ABSOLUTA: use ESTE nome no campo 'nome_peca'. NÃO altere, NÃO substitua,\n"
            "NÃO use conhecimento próprio para mudar a identificação da peça.\n"
            "Os títulos gerados devem usar este nome como base.\n"
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

ANÚNCIOS COLETADOS DO MERCADO LIVRE (referência de preço SOMENTE):
{tlist}

PREÇOS ENCONTRADOS:
{plist}

Ignorar como referência de preço: concessionárias, montadoras, fabricantes oficiais.
Usar: vendedores independentes com boa reputação.
Calcular 4 faixas: médio mercado, competitivo, venda rápida, premium.

═══════════════════════════════════════
ETAPA 3 — TÍTULOS
═══════════════════════════════════════

MERCADO LIVRE — exatamente 4 títulos, entre 55 e 60 caracteres cada:
- Prioridade: PEÇA > VEÍCULO CONFIRMADO > MOTOR > ANOS > LADO > CÓDIGO
- Use somente veículos da lista OEM confirmada nos títulos
- Se não há compatibilidade confirmada, use apenas peça + variações (Original, Novo, OEM, etc.)
- PROIBIDO: título que seja APENAS o código OEM (ex: "4605885905" é inválido como título)
- PROIBIDO: repetir o mesmo código OEM em todos os 4 títulos
- O código OEM pode aparecer no máximo em 1 dos 4 títulos, como complemento no final
- Não repetir palavras entre títulos; não usar emojis
- Anos: resumir como 2021/2026 ou 21/26
- Se você não souber o nome da peça, use "Peça Automotiva" + descrição genérica

SHOPEE — 1 título, até 100 caracteres, mais descritivo que ML.

OLX — 1 título, até 100 caracteres, tom comercial.

═══════════════════════════════════════
VALIDAÇÃO FINAL ANTES DE RESPONDER
═══════════════════════════════════════

Verifique cada item antes de retornar:
1. Todos os veículos em compatibilidades_confirmadas têm confirmação OEM?
2. Existe algum veículo inferido de anúncio ou dedução própria? Se sim, REMOVA.
3. Os títulos usam somente veículos confirmados?
4. O campo grau_de_confianca reflete corretamente a certeza sobre nome e compatibilidade?

Se qualquer dúvida sobre compatibilidade: retorne compatibilidades_confirmadas vazio e grau_de_confianca abaixo de 90.

═══════════════════════════════════════
SAÍDA — JSON PURO
═══════════════════════════════════════

Retorne SOMENTE o JSON abaixo, sem texto antes ou depois:

{{
  "nome_peca": "Nome comercial da peça identificada pelo código OEM",
  "oem": "{codigo}",
  "compatibilidades_confirmadas": [
    {{"veiculo": "Marca Modelo Versão Motor", "anos": "2021 2022 2023 2024 2025 2026", "detalhes": "1.6 Flex Automático"}}
  ],
  "grau_de_confianca": 95,
  "mercado_livre": [
    "Título ML 1 (55-60 chars)",
    "Título ML 2 (55-60 chars)",
    "Título ML 3 (55-60 chars)",
    "Título ML 4 (55-60 chars)"
  ],
  "shopee": "Título Shopee (máx 100 chars)",
  "olx": "Título OLX (máx 100 chars)",
  "titulos_otimizados": ["cópia de mercado_livre[0]", "cópia [1]", "cópia [2]", "cópia [3]"],
  "titulo_ia": "Cópia de mercado_livre[1] com código {codigo} no final (máx 60 chars)",
  "preco_sugerido": 0.00,
  "preco_medio_mercado": 0.00,
  "preco_competitivo": 0.00,
  "preco_venda_rapida": 0.00,
  "preco_premium": 0.00,
  "explicacao": "O que é a peça e para que serve (2 linhas máximo)",
  "funcao": "Função técnica resumida em 1 linha",
  "categoria": "Categoria ML",
  "ncm": "",
  "seo_palavras_chave": ["palavra1", "palavra2"],
  "observacoes": "Observações sobre lado, fabricante, revisão necessária se confiança < 90"
}}

REGRAS DO JSON:
- mercado_livre: EXATAMENTE 4 títulos entre 55 e 60 chars, todos diferentes
- shopee: máximo 100 chars
- olx: máximo 100 chars
- titulos_otimizados: cópia de mercado_livre (compatibilidade legado)
- titulo_ia: máximo 60 chars, código {codigo} SEMPRE no final
{regra_compat_json}
- compatibilidades_confirmadas[].anos: cada ano INDIVIDUAL separado por espaço (NUNCA "a" ou "-")
- grau_de_confianca: 0-100; abaixo de 90 indica necessidade de revisão manual
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
    # ── ETAPA 1: busca no ML pelo código OEM exato ─────────────────────────────
    titles, novos, usados = buscar_ml(codigo)
    ml_achou = bool(titles or novos or usados)

    # Verifica se algum título ML contém o OEM exato
    titulos_oem_exato = _titulos_com_oem(titles, codigo)
    oem_confirmado_ml = bool(titulos_oem_exato)

    # nome_peca_fixo (enviado pelo frontend) tem prioridade absoluta — nunca sobrescrever
    nome_peca_confirmado = nome_peca_fixo or None

    if oem_confirmado_ml:
        if not nome_peca_fixo:
            nome_peca_confirmado = _extrair_nome_oem_do_titulo(titulos_oem_exato[0], codigo)
        fonte_resultado = "oem_exato_ml"
        grau_confianca  = 99
        print(f"[WRX] OEM pesquisado: {codigo}")
        print(f"[WRX] OEM encontrado: {nome_peca_confirmado!r} | Título ML: {titulos_oem_exato[0]!r}")
        print(f"[WRX] Fonte: {fonte_resultado} | Confiança: {grau_confianca}")
    else:
        print(f"[WRX] OEM pesquisado: {codigo}")
        print(f"[WRX] OEM não encontrado por correspondência exata no ML ({len(titles)} títulos sem match)")

        # ── ETAPA 2: OEM não encontrado → tenta por nome da peça (IA identifica) ──
        if not ml_achou:
            nome_peca_ia = _identificar_nome_peca(codigo)
            if nome_peca_ia:
                print(f"[WRX] Buscando ML por nome (IA): {nome_peca_ia}")
                t2, n2, u2 = buscar_ml(nome_peca_ia)
                if t2 or n2 or u2:
                    titles, novos, usados = t2, n2, u2
                    ml_achou = True

        fonte_resultado = "ml_sem_oem_exato" if ml_achou else "ia_pura"
        grau_confianca  = 50 if ml_achou else 30
        print(f"[WRX] Fonte: {fonte_resultado} | Confiança: {grau_confianca}")

    if compatibilidade_oem:
        print(f"[WRX] Compatibilidade OEM confirmada: {len(compatibilidade_oem)} veículos")

    # ── ETAPA 3: chama IA (somente para títulos/preço; nome já confirmado se OEM exato) ──
    prices    = novos or usados
    preco_ref = calcular_preco_sugerido(prices)
    prompt    = _build_prompt(
        codigo, titles, prices[:10],
        compatibilidade_oem=compatibilidade_oem,
        nome_peca_confirmado=nome_peca_confirmado
    )
    data = _chamar_ia(prompt)

    if not data:
        # IA falhou — tenta identificar nome da peça e monta fallback
        nome_fallback = nome_peca_confirmado
        if not nome_fallback and titles:
            nome_fallback = _extrair_nome_oem_do_titulo(titles[0], codigo) or titles[0]
        if not nome_fallback:
            # Última tentativa: pede só o nome à IA via texto puro
            nome_fallback = _identificar_nome_peca(codigo)
        titulo_base = nome_fallback or f"Peça Automotiva {codigo}"
        titulos_gerados = [
            titulo_base[:60],
            f"{titulo_base} {codigo}".strip()[:60],
            f"{titulo_base} Original".strip()[:60],
            f"{titulo_base} Usado".strip()[:60],
        ]
        sem_ml = not ml_achou and not oem_confirmado_ml
        data = {
            "nome_peca": titulo_base,
            "oem": codigo,
            "codigo": codigo,
            "titulos_otimizados": titulos_gerados,
            "mercado_livre": titulos_gerados,
            "titulo_ia": titulos_gerados[0],
            "preco_sugerido": preco_ref,
            "compatibilidade": [],
            "compatibilidades_confirmadas": [],
            "versoes": [],
            "explicacao": "IA indisponível — sem dados ML para este código." if sem_ml else "IA indisponível. Dados coletados do Mercado Livre.",
            "funcao": titulo_base,
            "sem_ia": True,
            "sem_ml": sem_ml,
        }

    # Garante que nome_peca_confirmado não seja sobrescrito pela IA
    if nome_peca_confirmado:
        data["nome_peca"] = nome_peca_confirmado

    # Força preço calculado pelo Python
    if preco_ref > 0:
        data["preco_sugerido"] = preco_ref

    # Força grau_de_confianca para OEM exato (IA pode ter retornado valor menor)
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
    data["oem_titulo_ml"]          = titulos_oem_exato[0] if titulos_oem_exato else None
    data["fonte_resultado"]        = fonte_resultado
    data["grau_de_confianca"]      = data.get("grau_de_confianca") or grau_confianca
    data["ml_titulos_encontrados"] = len(titles)
    data["precos_novos"]           = novos
    data["precos_usados"]          = usados
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
    SHOPEE_PARTNER_ID = int(os.environ.get("SHOPEE_PARTNER_ID", "1234546"))
    SHOPEE_PARTNER_KEY = os.environ.get("SHOPEE_PARTNER_KEY", "shpk76666558496143524c7a474e416c59517651744a49766976425459796265")

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
            return
        try:
            requests.put(
                f"https://{_PH_HOST}/auth/v1/user",
                json={"data": {"wrx_ml_tokens": ml_tokens}},
                headers={"apikey": _PH_ANON, "Authorization": f"Bearer {jwt}", "Content-Type": "application/json"},
                timeout=10
            )
        except Exception:
            pass

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
                    return _ml_tokens_mem
        except Exception:
            pass
        # Arquivo local vazio/inexistente — busca no Supabase (sobrevive a redeployments)
        remote = _ph_load_tokens_remote()
        if remote:
            _ml_tokens_mem = remote
            try:
                with open(_ML_TOKENS_FILE, "w") as _f:
                    json.dump(remote, _f)
            except Exception:
                pass
        return _ml_tokens_mem

    def _ml_save_tokens(tokens):
        global _ml_tokens_mem
        _ml_tokens_mem = tokens
        try:
            with open(_ML_TOKENS_FILE, "w") as _f:
                json.dump(tokens, _f)
        except Exception:
            pass
        # Persiste no Supabase em background (tokens sobrevivem a redeployments)
        threading.Thread(target=_ph_save_tokens_remote, args=(tokens,), daemon=True).start()

    def _ml_get_user_token(conta="default"):
        tokens = _ml_load_tokens()
        t = tokens.get(conta, {})
        if not t.get("access_token"):
            return None
        if t.get("expires_at", 0) - time.time() < 300:
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
                else:
                    del tokens[conta]
                    _ml_save_tokens(tokens)
                    return None
            except Exception:
                return None
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
            _ml_save_tokens(tokens)
            return (
                "<html><body style='font-family:sans-serif;text-align:center;padding:40px;"
                "background:#0f172a;color:#fff'>"
                "<h2 style='color:#22c55e'>&#10003; Mercado Livre conectado!</h2>"
                f"<p>Conta: <strong>{conta}</strong></p>"
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
    @app.route("/integracoes/shopee/config", methods=["GET", "OPTIONS"])
    def shopee_config():
        if request.method == "OPTIONS":
            return _options_resp()
        return jsonify({
            "configured": True,
            "partner_id": SHOPEE_PARTNER_ID,
            "modo": "sandbox" if SHOPEE_PARTNER_ID == 1234546 else "producao",
            "aviso": "App em modo Developing (sandbox). Solicite Go-Live no open.shopee.com.br para publicar de verdade."
        })

    @app.route("/integracoes/shopee/publicar", methods=["POST", "OPTIONS"])
    def shopee_publicar():
        if request.method == "OPTIONS":
            return _options_resp()
        return jsonify({
            "ok": False,
            "erro": "Shopee em modo sandbox (Developing). OAuth nao disponivel ate aprovacao do app. Solicite Go-Live em open.shopee.com.br > seu app > Versao ao vivo."
        }), 501

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
