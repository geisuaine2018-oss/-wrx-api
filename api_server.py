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
from datetime import datetime as _datetime

# Cache em memória dos atributos de categoria do ML (evita rechamar a cada anúncio do mesmo SKU)
_ML_CAT_ATTRS_CACHE = {}

def _ml_categoria_attrs(cat_id):
    """Retorna {attr_id: attr} dos atributos da categoria ML. Usado para tratar
    atributos de VALOR FIXO (ex: VEHICLE_TYPE em algumas categorias), que dão
    'Validation error' se mandarmos um valor/id divergente do fixado pela categoria."""
    if not cat_id:
        return {}
    cached = _ML_CAT_ATTRS_CACHE.get(cat_id)
    if cached is not None:
        return cached
    m = {}
    try:
        rr = requests.get(f"https://api.mercadolibre.com/categories/{cat_id}/attributes", timeout=15)
        if rr.status_code == 200:
            for a in (rr.json() or []):
                if isinstance(a, dict) and a.get("id"):
                    m[a["id"]] = a
    except Exception:
        pass
    _ML_CAT_ATTRS_CACHE[cat_id] = m
    return m

def _ml_preferir_categoria_carro(candidatos):
    """Entre as categorias candidatas do predictor do ML, prefere a de VEHICLE_TYPE
    'Carro/Caminhonete'. Muitas categorias de autopeça são iguais e só mudam o tipo de
    veículo (Agrícola, Linha Pesada, Carro/Caminhonete); como é desmonte de CARRO,
    sempre queremos a de carro. Retorna o candidato escolhido (ou None p/ usar o 1º)."""
    fallback = None
    for c in (candidatos or []):
        cid = c.get("category_id")
        if not cid:
            continue
        vt = _ml_categoria_attrs(cid).get("VEHICLE_TYPE")
        if vt is None:
            # categoria sem VEHICLE_TYPE (autopeça genérica) serve de bom fallback
            if fallback is None:
                fallback = c
            continue
        nomes = [(v.get("name") or "").lower() for v in (vt.get("values") or [])]
        if any("carro" in n or "caminhon" in n for n in nomes):
            return c
    return fallback

# Cache de fotos já enviadas ao ML: (conta, chave_foto) -> picture_id.
# Evita reenviar a MESMA foto a cada um dos 10 anúncios do mesmo produto.
_ML_PIC_CACHE = {}

def _ml_upload_bytes(token, raw, mime="image/jpeg", ext="jpg"):
    """Sobe bytes de imagem pro serviço de imagens do ML e retorna o picture id (ou None)."""
    try:
        rr = requests.post(
            "https://api.mercadolibre.com/pictures/items/upload",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": (f"foto.{ext}", raw, mime)}, timeout=40
        )
        if rr.status_code in (200, 201):
            return (rr.json() or {}).get("id")
    except Exception:
        pass
    return None

def _ml_quadrar_bytes(raw, lado=1600):
    """Centraliza a imagem num quadrado branco lado×lado (padrão ML). Compositа a
    transparência sobre branco (não vira preto). Retorna JPEG bytes, ou None se falhar."""
    try:
        from PIL import Image
        import io as _io
        im = Image.open(_io.BytesIO(raw)).convert("RGBA")
        im.thumbnail((lado, lado), Image.LANCZOS)
        fundo = Image.new("RGBA", (lado, lado), (255, 255, 255, 255))
        fundo.paste(im, ((lado - im.width) // 2, (lado - im.height) // 2), im)
        buf = _io.BytesIO()
        fundo.convert("RGB").save(buf, format="JPEG", quality=90)
        return buf.getvalue()
    except Exception:
        return None

def _ml_foto_para_pic(token, conta_nome, foto):
    """Converte uma foto (URL http ou data:base64) num picture do ML, quadrada em
    1600 branco e enviada como arquivo. Cacheia por (conta, foto). Fallback: se algo
    falhar e a foto for URL http, devolve {'source': url} (o ML busca direto)."""
    import hashlib as _hl
    try:
        if foto.startswith("data:image"):
            import base64 as _b64
            raw = _b64.b64decode(foto.split(",", 1)[1])
            chave = "b64:" + _hl.md5(raw).hexdigest()
        elif foto.startswith("http"):
            chave = "url:" + foto
            raw = None
        else:
            return None
        ckey = (conta_nome, chave)
        if ckey in _ML_PIC_CACHE:
            return {"id": _ML_PIC_CACHE[ckey]}
        if raw is None:
            rr = requests.get(foto, timeout=25)
            if rr.status_code != 200:
                return {"source": foto}
            raw = rr.content
        quadrada = _ml_quadrar_bytes(raw)
        pid = _ml_upload_bytes(token, quadrada, "image/jpeg", "jpg") if quadrada else None
        if pid:
            _ML_PIC_CACHE[ckey] = pid
            return {"id": pid}
        return {"source": foto} if foto.startswith("http") else None
    except Exception:
        return {"source": foto} if foto.startswith("http") else None

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

# Playwright DESATIVADO no Railway: ML bloqueia datacenter (403), retornava vazio.
# Busca OEM real vai pela extensão Chrome / servidor local. Não instala mais o Chromium
# (economiza ~400MB, RAM e tempo de build). A função fica disponível mas não é chamada.
# if os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"):
#     threading.Thread(target=_ensure_playwright_chromium, daemon=True).start()

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
def buscar_ml(codigo, usar_playwright=True):
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

    # Camada 3b: Playwright Python (Railway/Linux) — desabilitado quando chamado de executar_busca()
    # para evitar dupla chamada asyncio.run() no mesmo thread (ETAPA 3 já usa Playwright)
    if usar_playwright and not novos and not usados:
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
    FLUXO OBRIGATÓRIO:
    OEM → Mercado Livre → Raspagem → Validação → Score → Compatibilidades aprovadas → IA
    A IA NÃO infere compatibilidade — somente formata dados aprovados pela raspagem.
    """
    _t0 = time.time()
    print(f"\n{'='*60}")
    print(f"[OEM BUSCA] codigo={codigo!r} | nome_fixo={nome_peca_fixo!r} | compat_oem={bool(compatibilidade_oem)}")
    print(f"{'='*60}")

    # Rastreamento de fonte por dado
    fonte_dados = {
        "titulos":        "nenhum",
        "compatibilidade":"nenhum",
        "precos":         "nenhum",
        "nome_peca":      "nenhum",
    }

    # ── ETAPA 1: ML API busca rápida com atributos básicos ───────────────────
    print(f"[OEM BUSCA] ETAPA 1 — ML API search: {codigo}")
    items_api = _buscar_api_ml_detalhado(codigo)
    titulos_ml   = [i["titulo"] for i in items_api if i["titulo"]]
    precos_novos = [i["preco"] for i in items_api if i.get("condicao") == "new"  and i["preco"] > 5]
    precos_usados= [i["preco"] for i in items_api if i.get("condicao") == "used" and i["preco"] > 5]
    items_com_oem = [i for i in items_api if codigo.upper().replace(" ","") in i["titulo"].upper().replace(" ","")]
    print(f"[OEM BUSCA] API ML: {len(items_api)} resultados | {len(items_com_oem)} com OEM no título")
    if items_api:
        fonte_dados["titulos"] = "api_ml"
        fonte_dados["precos"]  = "api_ml"
    if items_com_oem:
        print(f"[OEM CONFIRMADO] via API ML — título: {items_com_oem[0]['titulo'][:70]}")

    # Fallback 1: se API não retornou nada, tenta buscar_ml() — sem Playwright aqui
    # (ETAPA 3 já usará Playwright; dois asyncio.run() no mesmo thread causam falha silenciosa)
    if not items_api:
        print(f"[OEM BUSCA] API sem resultados — fallback buscar_ml() sem Playwright...")
        titulos_fb, novos_fb, usados_fb = buscar_ml(codigo, usar_playwright=False)
        if titulos_fb:
            titulos_ml.extend(t for t in titulos_fb if t not in titulos_ml)
            precos_novos.extend(novos_fb)
            precos_usados.extend(usados_fb)
            fonte_dados["titulos"] = "requests_html"
            print(f"[OEM BUSCA] buscar_ml() encontrou {len(titulos_fb)} títulos")

    # Fallback 2 (seguro): busca "nome + OEM" juntos na API — nunca só pelo nome
    if not items_api and nome_peca_fixo:
        query_segura = f"{nome_peca_fixo} {codigo}"
        print(f"[OEM BUSCA] Fallback seguro: '{query_segura}'...")
        items_por_nome = _buscar_api_ml_detalhado(query_segura)
        if items_por_nome:
            items_api = items_por_nome
            items_com_oem = [i for i in items_por_nome if codigo.upper().replace(" ","") in i["titulo"].upper().replace(" ","")]
            titulos_ml.extend(i["titulo"] for i in items_por_nome if i["titulo"] and i["titulo"] not in titulos_ml)
            precos_novos.extend(i["preco"] for i in items_por_nome if i.get("condicao") == "new"  and i["preco"] > 5)
            precos_usados.extend(i["preco"] for i in items_por_nome if i.get("condicao") == "used" and i["preco"] > 5)
            fonte_dados["titulos"] = "api_ml_nome_oem"
            fonte_dados["precos"]  = "api_ml_nome_oem"
            print(f"[OEM BUSCA] Fallback nome+OEM: {len(items_por_nome)} resultados | {len(items_com_oem)} com OEM")

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
        print(f"[OEM BUSCA] ETAPA 3 — Playwright lista + PDPs (sem atributos suficientes)...")
        urls_lista = [
            f"https://lista.mercadolivre.com.br/{codigo}",
            f"https://lista.mercadolivre.com.br/acessorios-veiculos/{codigo}",
        ]
        # URLs de fallback: permalinks da API
        extra_pdp = [i["permalink"] for i in (items_com_oem or items_api)[:3]
                     if i.get("permalink")]

        # Uma única sessão Playwright: lista → extrai URLs → abre PDPs
        try:
            import asyncio
            from playwright.async_api import async_playwright

            async def _scrape_lista_e_pdps():
                resultados = []
                async with async_playwright() as p:
                    browser = await p.chromium.launch(headless=True, args=[
                        "--no-sandbox","--disable-dev-shm-usage","--disable-gpu",
                        "--disable-setuid-sandbox","--disable-blink-features=AutomationControlled",
                        "--window-size=1280,800",
                    ])
                    ctx = await browser.new_context(
                        locale="pt-BR", user_agent=_UA_ML,
                        viewport={"width": 1280, "height": 800}, java_script_enabled=True,
                    )
                    await ctx.add_init_script(_STEALTH_JS)
                    page = await ctx.new_page()

                    # Passo 1: carregar página de lista e extrair URLs via JS (mais confiável que BS4)
                    urls_pdp = list(extra_pdp)
                    for url_lista in urls_lista:
                        try:
                            print(f"[PW3] Lista: {url_lista}")
                            await page.goto(url_lista, timeout=35000, wait_until="domcontentloaded")
                            try:
                                await page.wait_for_selector(
                                    "li.ui-search-layout__item, .poly-card, .poly-component__title",
                                    timeout=20000
                                )
                            except Exception:
                                pass
                            # Extrai URLs via JS — retorna mais URLs, ordenação feita em Python
                            js_urls = await page.evaluate("""() => {
                                const urls = [];
                                const vistos = new Set();
                                document.querySelectorAll(
                                    'li.ui-search-layout__item a, .poly-card a, a[href*="mercadolivre.com.br"]'
                                ).forEach(a => {
                                    try {
                                        const h = a.href.split('?')[0].split('#')[0];
                                        if (/mercadolivre\\.com\\.br/.test(h) && /MLB[A-Z]?\\d+/.test(h) && !vistos.has(h)) {
                                            vistos.add(h);
                                            urls.push(h);
                                        }
                                    } catch(e) {}
                                });
                                return urls.slice(0, 20);
                            }""")
                            # Ordenação em Python: URLs com OEM no slug vêm primeiro
                            js_urls = sorted(js_urls or [], key=lambda u: (0 if codigo in u else 1))[:8]
                            print(f"[PW3] URLs via JS: {len(js_urls)}")
                            for u in js_urls:
                                if u not in urls_pdp:
                                    urls_pdp.append(u)
                            if urls_pdp:
                                break
                        except Exception as e:
                            print(f"[PW3] Erro lista {url_lista}: {e}")
                            continue

                    urls_pdp = list(dict.fromkeys(urls_pdp))[:5]
                    print(f"[PW3] Total PDPs para scraping: {len(urls_pdp)}")

                    # Passo 2: abrir cada PDP na MESMA sessão
                    for url_pdp in urls_pdp:
                        try:
                            await page.goto(url_pdp, timeout=30000, wait_until="domcontentloaded")
                            try:
                                await page.wait_for_selector(
                                    "h1.ui-pdp-title, .ui-pdp-title, .andes-table",
                                    timeout=10000
                                )
                            except Exception:
                                pass
                            html_pdp = await page.content()
                            if len(html_pdp) > 5000:
                                resultados.append({"url": url_pdp, "html": html_pdp})
                                print(f"[PW3] PDP OK: {url_pdp[:60]} → {len(html_pdp)} bytes")
                        except Exception as e:
                            print(f"[PW3] Erro PDP {url_pdp[:60]}: {e}")
                            continue

                    await browser.close()
                return resultados

            pdp_results = asyncio.run(_scrape_lista_e_pdps())
            for item in pdp_results:
                parsed = _parse_pagina_anuncio(item["html"], item["url"])
                if parsed.get("atributos") or parsed.get("titulo"):
                    anuncios.append(parsed)
                    fonte_dados["titulos"] = "raspagem_playwright"
                    if parsed.get("atributos"):
                        fonte_dados["compatibilidade"] = "raspagem_playwright"
                    print(f"[OEM BUSCA] PDP raspado: {len(parsed.get('atributos',{}))} attrs | {parsed.get('titulo','')[:60]}")
        except Exception as e:
            print(f"[OEM BUSCA] ETAPA 3 ERRO: {e}")
            # Fallback: requests para PDPs com permalinks da API
            for h in _buscar_requests_html(urls_lista):
                for url in _extrair_urls_da_lista_html(h):
                    if url not in [a.get("url","") for a in anuncios]:
                        break

        if not anuncios:
            print(f"[WRX] ETAPA 3: Sem dados de PDPs")

    # ── ETAPA 4: Consolida e cria score ──────────────────────────────────────
    print(f"[OEM BUSCA] ETAPA 4 — Consolidando {len(anuncios)} anúncios...")
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
                print(f"[OEM CONFIRMADO] via raspagem PDP — título: {a.get('titulo','')[:70]}")
                break

    if not oem_confirmado_ml:
        print(f"[OEM NAO CONFIRMADO] codigo={codigo!r} — não encontrado em nenhum anúncio ML")

    compat_consolidada = (consolidado or {}).get("compatibilidade", [])

    # Log de score por compatibilidade
    if compat_consolidada:
        print(f"[SCORE] {len(compat_consolidada)} compatibilidades consolidadas:")
        for c in compat_consolidada:
            score_pct = int(c.get("confianca", 0) * 100)
            print(f"  [COMPATIBILIDADE APROVADA] {c['veiculo']} | anos={c['anos']} | score={score_pct}% | x{c.get('ocorrencias',1)}")
        if fonte_dados["compatibilidade"] == "nenhum":
            fonte_dados["compatibilidade"] = "consolidacao_anuncios"
    else:
        print(f"[SCORE] Nenhuma compatibilidade consolidada dos anúncios")

    # Compatibilidade fornecida pelo frontend tem prioridade absoluta
    if compatibilidade_oem:
        print(f"[COMPATIBILIDADE APROVADA] {len(compatibilidade_oem)} veículos do frontend (usuário validou)")
        fonte_dados["compatibilidade"] = "frontend_usuario"
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

    print(f"[SCORE] fonte_resultado={fonte_resultado} | grau_confianca={grau_confianca}%")

    # OEM não confirmado E sem compat do frontend → bloquear IA
    if not oem_confirmado_ml and not compat_final:
        print(f"[OEM NAO CONFIRMADO] BLOQUEANDO IA — OEM {codigo!r} não confirmado, sem compat do frontend")
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
        if nome_peca_confirmado:
            fonte_dados["nome_peca"] = "api_ml_oem_exato"
    if not nome_peca_confirmado and titulos_ml:
        nome_peca_confirmado = _extrair_nome_oem_do_titulo(titulos_ml[0], codigo) or titulos_ml[0]
        if nome_peca_confirmado:
            fonte_dados["nome_peca"] = "api_ml_titulo"
    if nome_peca_fixo:
        fonte_dados["nome_peca"] = "cadastro_usuario"

    print(f"[OEM BUSCA] Nome peça: {nome_peca_confirmado!r} | fonte_nome={fonte_dados['nome_peca']}")

    # ── ETAPA 5: IA apenas para formatação — nunca para inferir compat ────────
    precos = sorted(set(round(p,2) for p in precos_novos))[:15] or \
             sorted(set(round(p,2) for p in precos_usados))[:15]
    if consolidado and consolidado.get("precos"):
        precos = precos or consolidado["precos"]
    if precos:
        if fonte_dados["precos"] == "nenhum":
            fonte_dados["precos"] = "consolidacao_anuncios"
    preco_ref = calcular_preco_sugerido(precos)

    print(f"[IA GERANDO ANUNCIO] titulos_ml={len(titulos_ml)} | precos={len(precos)} | compat={len(compat_final or [])} | fonte_compat={fonte_dados['compatibilidade']}")
    print(f"[IA GERANDO ANUNCIO] REGRA: IA formata SOMENTE — NÃO infere compatibilidade")
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
    data["fonte_dados"]            = fonte_dados
    # legado
    data["fonte"] = fonte_resultado

    _elapsed = round(time.time() - _t0, 1)
    print(f"[OEM BUSCA] CONCLUÍDO em {_elapsed}s | nome={data.get('nome_peca')!r} | fonte={fonte_resultado} | conf={data['grau_de_confianca']}%")
    print(f"[OEM BUSCA] fonte_dados={fonte_dados}")
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
            print(f"[OEM BUSCA] nome_peca_fixo recebido: {nome_peca_fixo!r}")
        print(f"[OEM BUSCA] Request: codigo={codigo!r}" + (f" | compat_oem={len(compatibilidade_oem)} veículos" if compatibilidade_oem else ""))
        resultado = executar_busca(codigo, compatibilidade_oem=compatibilidade_oem, nome_peca_fixo=nome_peca_fixo)
        return jsonify(resultado)

    @app.route("/limpar-cache", methods=["GET", "POST", "OPTIONS"])
    def limpar_cache():
        if request.method == "OPTIONS":
            return _cors(app.response_class(status=204))
        import glob as _glob
        removidos = []
        erros = []
        _cache_files = [
            os.path.join(_INTEG_DIR, "wrx_ml_anuncios.json"),
            os.path.join(_INTEG_DIR, "wrx_shopee_anuncios.json"),
        ] if "_INTEG_DIR" in dir() else []
        for f in _cache_files:
            try:
                if os.path.exists(f):
                    os.remove(f)
                    removidos.append(os.path.basename(f))
                    print(f"[LIMPAR-CACHE] Removido: {f}")
            except Exception as e:
                erros.append(f"{os.path.basename(f)}: {e}")
        print(f"[LIMPAR-CACHE] Concluído — removidos={removidos} | erros={erros}")
        return jsonify({"ok": True, "removidos": removidos, "erros": erros,
                        "mensagem": f"{len(removidos)} arquivo(s) de cache removido(s)"})

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
            # modelo isnet-general-use: recorta autopeças muito melhor que o u2net padrão
            MODELO_REMBG = "isnet-general-use"
            # 1. rembg direto (com sessão do modelo melhor, cacheada)
            try:
                import rembg
                global _rembg_session
                try:
                    _rembg_session
                except NameError:
                    _rembg_session = rembg.new_session(MODELO_REMBG)
                resultado = rembg.remove(img_bytes, session=_rembg_session)
            except Exception as _e_rembg:
                print(f"[REMBG] direto falhou ({_e_rembg}); tentando subprocess/fallback")
            # 2. rembg via subprocess Python 3.12 (também com o modelo melhor)
            if not resultado:
                for py_path in [
                    r"C:\Users\cauav\AppData\Local\Programs\Python\Python312\python.exe",
                    r"C:\Users\Geisuane\AppData\Local\Programs\Python\Python312\python.exe",
                ]:
                    if os.path.exists(py_path):
                        try:
                            script = ("import sys,rembg; s=rembg.new_session('" + MODELO_REMBG +
                                      "'); sys.stdout.buffer.write(rembg.remove(sys.stdin.buffer.read(), session=s))")
                            r = subprocess.run([py_path, "-c", script], input=img_bytes,
                                               capture_output=True, timeout=60,
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
    import threading as _threading
    _ml_refresh_lock = _threading.Lock()  # serializa o refresh do token ML (uso único)
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

    # ── WhatsApp (WAHA) ─────────────────────────────────────────────────────────
    WAHA_BASE        = os.environ.get("WAHA_BASE",        "https://evo.dominiodaspecas.com.br")
    WAHA_API_KEY     = os.environ.get("WAHA_API_KEY",     "DsmX@2026#Waha")
    WAHA_SESSION     = os.environ.get("WAHA_SESSION",     "default")
    WAHA_WEBHOOK_URL = os.environ.get("WAHA_WEBHOOK_URL", "https://n8n.dominiodaspecas.com.br/webhook/marcelo")

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
        global _ml_tokens_mem
        tokens = _ml_load_tokens()
        t = tokens.get(conta, {})
        if not t.get("access_token"):
            print(f"[ML-TOKENS] Conta '{conta}' nao autorizada. Contas disponiveis: {list(tokens.keys())}")
            return None
        # token ainda válido (>5min): usa direto, sem refresh
        if t.get("expires_at", 0) - time.time() >= 300:
            return t.get("access_token")
        # precisa renovar — SERIALIZA com lock. O refresh_token do ML é de USO ÚNICO;
        # dois refresh concorrentes invalidam um ao outro (invalid_grant) e derrubam a
        # conta. Com o lock, só uma thread renova; as outras esperam e usam o token novo.
        with _ml_refresh_lock:
            tokens = _ml_load_tokens()
            t = tokens.get(conta, {})
            if not t.get("access_token"):
                return None
            if t.get("expires_at", 0) - time.time() >= 300:
                return t.get("access_token")  # outra thread já renovou enquanto eu esperava
            print(f"[ML-TOKENS] Token da conta '{conta}' expirando. Refresh (lock)...")
            for tentativa in (1, 2):
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
                        return t.get("access_token")
                    print(f"[ML-TOKENS] ERRO refresh '{conta}' (tent.{tentativa}): HTTP {_r.status_code} — {_r.text[:200]}")
                    if "invalid_grant" in _r.text.lower():
                        # invalid_grant pode ser FALSO ALARME: durante um deploy o Railway roda
                        # 2 instâncias ao mesmo tempo (overlap). A outra instância pode já ter
                        # renovado e invalidado este refresh_token. Antes de derrubar a conta,
                        # recarrega do Supabase: se lá tiver um refresh_token DIFERENTE do que
                        # falhou, é porque outra instância renovou — adoto o token novo.
                        remoto = _ph_load_tokens_remote()
                        rt = remoto.get(conta, {})
                        rt_remoto = rt.get("refresh_token")
                        if rt.get("access_token") and rt_remoto and rt_remoto != t.get("refresh_token"):
                            # outra instância renovou no overlap do deploy — adota o token novo
                            tokens[conta] = rt
                            _ml_tokens_mem = tokens
                            print(f"[ML-TOKENS] invalid_grant, mas Supabase tem token NOVO (outra instancia renovou no deploy). Conta '{conta}' MANTIDA.")
                            return rt.get("access_token")
                        if rt_remoto and rt_remoto == t.get("refresh_token"):
                            # Supabase confirma: é o MESMO refresh_token que falhou → inválido de verdade
                            print(f"[ML-TOKENS] refresh_token invalido (confirmado no Supabase) — conta '{conta}' precisa reconectar.")
                            del tokens[conta]
                            _ml_save_tokens(tokens)
                            return None
                        # Não consegui confirmar no Supabase (inacessível/vazio) → NÃO derruba a
                        # conta; mantém e tenta de novo depois. Evita perder a conta por falha
                        # temporária de rede no Supabase.
                        print(f"[ML-TOKENS] invalid_grant sem confirmacao do Supabase — conta '{conta}' MANTIDA (tenta depois).")
                        return None
                    # erro temporário (500/429/etc): tenta de novo 1x, senão mantém a conta
                    time.sleep(1)
                except Exception as e:
                    print(f"[ML-TOKENS] ERRO (excecao) refresh '{conta}' (tent.{tentativa}): {e}")
                    time.sleep(1)
            print(f"[ML-TOKENS] refresh '{conta}' falhou (temporario) — conta mantida, tenta depois.")
            return None

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
        # scope=offline_access é OBRIGATORIO p/ o ML devolver refresh_token.
        # Sem ele, vem so o access_token de 6h e a conta "cai" toda hora (sem como renovar).
        url = (
            "https://auth.mercadolivre.com.br/authorization"
            f"?response_type=code&client_id={ML_CLIENT_ID.strip()}"
            f"&redirect_uri={_urlparse.quote(ML_REDIRECT_URI, safe='')}"
            f"&scope={_urlparse.quote('offline_access read write')}"
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
            user_id_real = str(me.get("id", ""))
            cats = requests.get(f"https://api.mercadolibre.com/users/{user_id_real}/items/search",
                                params={"status": "active", "limit": 5},
                                headers={"Authorization": f"Bearer {token}"}, timeout=10)
            cats_all = requests.get(f"https://api.mercadolibre.com/users/{user_id_real}/items/search",
                                params={"limit": 5},
                                headers={"Authorization": f"Bearer {token}"}, timeout=10)
            cats_json = cats.json()
            cats_all_json = cats_all.json()
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
                "active_items_search": cats_json,
                "all_items_search": cats_all_json,
                "active_items_http": cats.status_code
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
        # Busca mais candidatas que o pedido (mín. 6) p/ poder preferir a categoria de CARRO
        try:
            _fetch_limit = max(int(limit or 1), 6)
        except Exception:
            _fetch_limit = 6
        try:
            _r = requests.get(
                f"https://api.mercadolibre.com/sites/{site_id}/domain_discovery/search",
                params={"limit": _fetch_limit, "q": q}, headers=hdrs, timeout=10
            )
            if _r.status_code == 200:
                _data = _r.json() or []
                if not _data:
                    return jsonify({"erro": "sem categoria"}), 404
                # Desmonte de carro: prefere a candidata com VEHICLE_TYPE Carro/Caminhonete
                _escolhida = _ml_preferir_categoria_carro(_data) or _data[0]
                return jsonify(_escolhida)
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

    @app.route("/integracoes/mercadolivre/pausar-item", methods=["POST", "OPTIONS"])
    def ml_pausar_item():
        # Pausa, reativa ou encerra ("exclui") um anuncio ML por ml_id.
        # status: paused | active | closed. ML nao deleta itens — "excluir" = closed.
        if request.method == "OPTIONS":
            return _options_resp()
        data = request.get_json(force=True) or {}
        ml_id = (data.get("ml_id") or data.get("mlId") or "").strip()
        conta = data.get("nome", "default")
        novo = (data.get("status") or "paused").strip()
        if not ml_id:
            return jsonify({"ok": False, "erro": "ml_id obrigatorio"}), 400
        if novo not in ("paused", "active", "closed"):
            return jsonify({"ok": False, "erro": "status deve ser paused, active ou closed"}), 400
        token = _ml_get_user_token(conta)
        if not token:
            return jsonify({"ok": False, "erro": f"conta '{conta}' sem token ML"}), 401
        try:
            r = requests.put(
                f"https://api.mercadolibre.com/items/{ml_id}",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"status": novo}, timeout=15)
            if r.status_code == 200:
                return jsonify({"ok": True, "ml_id": ml_id, "status": novo})
            return jsonify({"ok": False, "erro": f"ML {r.status_code}: {r.text[:200]}"}), 502
        except Exception as e:
            return jsonify({"ok": False, "erro": str(e)}), 500

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
        # Fotos: todas viram quadrado branco 1600×1600 e sobem pro ML (cache por conta).
        # Fallback p/ {'source': url} se quadrar/subir falhar (não quebra a publicação).
        _pics = []
        for _f in (data.get("fotos", []) or [])[:10]:
            if not _f:
                continue
            _p = _ml_foto_para_pic(token, conta_nome, _f)
            if _p:
                _pics.append(_p)
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
        if _pics:
            ml_payload["pictures"] = _pics
        attrs = list(data.get("attributes") or [])
        attr_ids = {a.get("id") for a in attrs}
        # Atributos obrigatórios: PART_NUMBER, BRAND, MODEL, dimensões de embalagem
        if "PART_NUMBER" not in attr_ids and sku:
            attrs.append({"id": "PART_NUMBER", "value_name": sku})
        # BRAND: usa a marca do formulário; se não vier, tenta deduzir do título
        marca_form = (data.get("marca") or "").strip()
        if "BRAND" not in attr_ids:
            if marca_form:
                brand_val = marca_form
            else:
                import re as _re
                brand_m = _re.search(r'\b([A-Za-záéíóúãõç]{3,})\s+\d{4}', _titulo)
                brand_val = brand_m.group(1).capitalize() if brand_m else "Genérico"
            attrs.append({"id": "BRAND", "value_name": brand_val})
        # MODEL: obrigatório em várias categorias de autopeça (ex: MLB3530)
        modelo_form = (data.get("modelo") or "").strip()
        if "MODEL" not in attr_ids:
            attrs.append({"id": "MODEL", "value_name": modelo_form or marca_form or _titulo[:60] or "Universal"})
        # VEHICLE_TYPE: obrigatório em muitas categorias de autopeça, MAS algumas o definem
        # como valor FIXO (ex: MLB457680 -> "Agrícola"). Mandar valor divergente dá "Validation error".
        # Consultamos a categoria: fixo -> não manda (o ML preenche); lista -> manda o value_id certo.
        if "VEHICLE_TYPE" not in attr_ids:
            _cat_attrs = _ml_categoria_attrs(ml_payload["category_id"])
            _vt = _cat_attrs.get("VEHICLE_TYPE")
            if _vt is None:
                # categoria não lista VEHICLE_TYPE: mantém o comportamento antigo (autopeças genéricas)
                if not _cat_attrs:
                    attrs.append({"id": "VEHICLE_TYPE", "value_name": "Carro/Caminhonete"})
            else:
                _tags = _vt.get("tags") or {}
                _vals = _vt.get("values") or []
                if _tags.get("fixed"):
                    pass  # ML define sozinho -> não enviar
                elif _vals:
                    _esc = next((v for v in _vals if "carro" in (v.get("name") or "").lower()), _vals[0])
                    attrs.append({"id": "VEHICLE_TYPE", "value_id": _esc.get("id"), "value_name": _esc.get("name")})
                else:
                    attrs.append({"id": "VEHICLE_TYPE", "value_name": "Carro/Caminhonete"})
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
                # Ficha fiscal (NCM/CEST/CSOSN) — Simples Nacional, origem nacional. Permite emissao de nota.
                _ncm_ml = str(data.get("ncm") or "").strip()
                if _ncm_ml and _ncm_ml not in ("00000000", "0000000", "0"):
                    try:
                        _fi = {"sku": sku, "title": _titulo, "type": "single", "measurement_unit": "UN",
                               "tax_information": {"ncm": _ncm_ml, "origin_type": "reseller",
                                                   "origin_detail": "0", "csosn": str(data.get("csosn") or "102")}}
                        _cest_ml = str(data.get("cest") or "").strip()
                        if _cest_ml and _cest_ml not in ("0000000", "00000000", "0"):
                            _fi["tax_information"]["cest"] = _cest_ml
                        requests.post("https://api.mercadolibre.com/items/fiscal_information",
                                      headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                                      json=_fi, timeout=15)
                    except Exception:
                        pass
                # Grava no banco (ml_anuncios) com o SKU BASE p/ o card do estoque
                # refletir NA HORA (sem esperar o sync). Service key prevalece (evita RLS).
                try:
                    _sku_base = (data.get("skuInterno") or sku.split("-")[0] or sku).strip()
                    _thumb = item.get("thumbnail") or ""
                    if not _thumb:
                        _pp = item.get("pictures") or []
                        if _pp:
                            _thumb = _pp[0].get("secure_url") or _pp[0].get("url") or ""
                    _sb_key = os.environ.get("SUPABASE_SERVICE_KEY") or _WRX_SB_KEY
                    requests.post(
                        f"{_WRX_SB_URL}/rest/v1/ml_anuncios?on_conflict=ml_id,conta",
                        headers={"apikey": _sb_key, "Authorization": f"Bearer {_sb_key}",
                                 "Content-Type": "application/json",
                                 "Prefer": "resolution=merge-duplicates"},
                        json={
                            "ml_id": item_id, "conta": conta_nome, "sku": _sku_base,
                            "titulo": _titulo, "preco": preco,
                            "estoque": int(data.get("quantidade", 1) or 1),
                            "status": item.get("status", "active"),
                            "thumbnail": _thumb, "vinculado": True,
                        }, timeout=8
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
        """Busca todos os IDs de anúncios ML do vendedor.

        Usa search_type=scan (scroll_id) porque a paginação por offset da API
        do ML trava em 1000 — vendedores maiores perdiam anúncios acima disso.
        """
        ids = []
        scroll_id = None
        while True:
            params = {"status": status, "search_type": "scan", "limit": 100}
            if scroll_id:
                params["scroll_id"] = scroll_id
            r = requests.get(
                f"https://api.mercadolibre.com/users/{user_id}/items/search",
                params=params,
                headers={"Authorization": f"Bearer {token}"}, timeout=15
            )
            if r.status_code != 200:
                break
            d = r.json()
            batch = d.get("results", [])
            if not batch:
                break
            ids.extend(batch)
            scroll_id = d.get("scroll_id")
            if not scroll_id:
                break
        return ids

    def _ml_buscar_detalhes_lote(token, ids):
        """Busca detalhes de até 20 itens por vez.
        IMPORTANTE: NÃO usar projeção `attributes=` aqui — o multiget com projeção
        devolve o array `attributes` SEM os value_name, então o SELLER_SKU (onde o
        SKU fica escondido) some. Sem projeção, vem o item completo e o SKU aparece."""
        itens = []
        for i in range(0, len(ids), 20):
            lote = ids[i:i+20]
            r = requests.get(
                "https://api.mercadolibre.com/items",
                params={"ids": ",".join(lote)},
                headers={"Authorization": f"Bearer {token}"}, timeout=25
            )
            if r.status_code != 200:
                continue
            for entry in r.json():
                if entry.get("code") == 200:
                    itens.append(entry.get("body", {}))
        return itens

    def _ml_extrair_sku(item):
        """SKU do anuncio ML: tenta nivel do item, depois variacoes, depois atributo SELLER_SKU."""
        s = str(item.get("seller_sku") or item.get("seller_custom_field") or "").strip()
        if s:
            return s
        for v in (item.get("variations") or []):
            vs = str(v.get("seller_sku") or v.get("seller_custom_field") or "").strip()
            if vs:
                return vs
        for a in (item.get("attributes") or []):
            if a.get("id") == "SELLER_SKU":
                av = str(a.get("value_name") or a.get("value_id") or "").strip()
                if av:
                    return av
        return ""

    @app.route("/integracoes/mercadolivre/anuncios-db", methods=["GET", "OPTIONS"])
    def ml_anuncios_db():
        if request.method == "OPTIONS":
            return _options_resp()
        # Tenta Supabase primeiro
        try:
            r = requests.get(
                f"{_WRX_SB_URL}/rest/v1/ml_anuncios?select=*&order=sync_at.desc&limit=5000",
                headers=_wrx_headers(), timeout=10
            )
            if r.status_code == 200:
                dados = r.json()
                # Agrupa por SKU para manter compatibilidade com o formato do cache
                por_sku = {}
                for d in dados:
                    sku = (d.get("sku") or "").upper()
                    if sku not in por_sku:
                        por_sku[sku] = []
                    por_sku[sku].append({
                        "mlId": d.get("ml_id", ""),
                        "externalListingId": d.get("ml_id", ""),
                        "titulo": d.get("titulo", ""),
                        "preco": d.get("preco", 0),
                        "estoque": d.get("estoque", 0),
                        "status": d.get("status", "active"),
                        "thumbnail": d.get("thumbnail", ""),
                        "integrationId": d.get("conta", ""),
                        "marketplace": "ml",
                        "vinculado": d.get("vinculado", False),
                    })
                return jsonify({"ok": True, "anunciosPorSku": por_sku, "totalSkus": len(por_sku), "totalAnuncios": len(dados), "fonte": "supabase"})
        except Exception:
            pass
        # Fallback: cache local
        cache = _ml_anuncios_cache_load()
        total = sum(len(v) for v in cache.values())
        return jsonify({"ok": True, "anunciosPorSku": cache, "totalSkus": len(cache), "totalAnuncios": total, "fonte": "cache"})

    @app.route("/integracoes/mercadolivre/setup-tabela", methods=["POST", "GET", "OPTIONS"])
    def ml_setup_tabela():
        if request.method == "OPTIONS":
            return _options_resp()
        service_key = os.environ.get("SUPABASE_SERVICE_KEY", "")
        sql = """
CREATE TABLE IF NOT EXISTS ml_anuncios (
  id BIGSERIAL PRIMARY KEY,
  ml_id TEXT NOT NULL,
  conta TEXT NOT NULL DEFAULT 'default',
  sku TEXT,
  titulo TEXT,
  preco FLOAT DEFAULT 0,
  estoque INTEGER DEFAULT 0,
  status TEXT DEFAULT 'active',
  thumbnail TEXT,
  vinculado BOOLEAN DEFAULT FALSE,
  sync_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(ml_id, conta)
);
CREATE INDEX IF NOT EXISTS idx_ml_anuncios_sku ON ml_anuncios(sku);
"""
        if service_key:
            r = requests.post(
                f"{_WRX_SB_URL}/rest/v1/rpc/exec",
                headers={"apikey": service_key, "Authorization": f"Bearer {service_key}", "Content-Type": "application/json"},
                json={"sql": sql}, timeout=15
            )
            if r.status_code in (200, 201, 204):
                return jsonify({"ok": True, "msg": "Tabela ml_anuncios criada com sucesso"})
        return jsonify({"ok": False, "msg": "Execute o SQL manualmente no Supabase", "sql": sql})

    @app.route("/integracoes/mercadolivre/sincronizar-anuncios", methods=["POST", "GET", "OPTIONS"])
    def ml_sincronizar_anuncios():
        if request.method == "OPTIONS":
            return _options_resp()
        tokens_data = _ml_load_tokens()
        if not tokens_data:
            return jsonify({"ok": False, "erro": "Mercado Livre nao autorizado"}), 401

        # Filtro opcional por conta (?conta=default ou body {"conta": "geisa"})
        filtro_conta = request.args.get("conta") or (request.get_json(silent=True) or {}).get("conta") or ""
        # ?incluir_encerrados=1 também puxa os anúncios 'closed' (mais lento, roda sob demanda)
        incluir_enc = (request.args.get("incluir_encerrados") == "1") or bool((request.get_json(silent=True) or {}).get("incluir_encerrados"))

        # Carrega SKUs do sistema para fazer vínculo
        skus_sistema = set()
        try:
            r_pecas = requests.get(
                f"{_WRX_SB_URL}/rest/v1/pecas_estoque?select=sku&limit=20000",
                headers=_wrx_headers(), timeout=15
            )
            if r_pecas.status_code == 200:
                for p in r_pecas.json():
                    if p.get("sku"):
                        skus_sistema.add(str(p["sku"]).strip().upper())
        except Exception:
            pass

        por_sku = {}
        total_anuncios = 0
        total_vinculados = 0
        erros = []
        todos_itens_sb = []  # para salvar no Supabase
        now_iso = _datetime.utcnow().isoformat() + "Z"

        contas_alvo = [filtro_conta] if filtro_conta and filtro_conta in tokens_data else list(tokens_data.keys())
        for conta_nome in contas_alvo:
            token = _ml_get_user_token(conta_nome)
            if not token:
                erros.append(f"{conta_nome}: token invalido")
                continue
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
            ids_pausados = _ml_buscar_todos_ids(token, user_id, "paused")
            ids_fechados = _ml_buscar_todos_ids(token, user_id, "closed") if incluir_enc else []
            todos_ids = ids_ativos + ids_pausados + ids_fechados
            print(f"[ML-SYNC] {conta_nome}: {len(ids_ativos)} ativos + {len(ids_pausados)} pausados + {len(ids_fechados)} encerrados")
            itens = _ml_buscar_detalhes_lote(token, todos_ids)
            for item in itens:
                # SKU: nivel do item -> variacoes -> atributo SELLER_SKU
                sku_raw = _ml_extrair_sku(item)
                sku = sku_raw.upper()
                estoque = item.get("available_quantity", 0)
                status_item = item.get("status", "active")
                vinculado = bool(sku) and sku in skus_sistema
                # Cache local + contagem de vinculados: só ATIVOS com estoque e SKU
                # (comportamento antigo). Mas o Supabase recebe TODOS (ativos+pausados)
                # com status/estoque/sku REAIS — pra puxar o SKU dos pausados e corrigir
                # o status "active" falso que poluia o anti-venda-dupla.
                if status_item == "active" and estoque > 0 and vinculado:
                    total_vinculados += 1
                # Cache local é indexado por SKU; só ativos com estoque e com SKU
                if sku and status_item == "active" and estoque > 0:
                    if sku not in por_sku:
                        por_sku[sku] = []
                    por_sku[sku].append({
                        "externalListingId": item.get("id", ""),
                        "mlId": item.get("id", ""),
                        "titulo": item.get("title", ""),
                        "preco": item.get("price", 0),
                        "estoque": estoque,
                        "status": status_item,
                        "thumbnail": item.get("thumbnail", ""),
                        "integrationId": conta_nome,
                        "marketplace": "ml",
                        "vinculado": vinculado,
                    })
                # Supabase recebe TODOS (ativos + pausados, com e sem SKU) — status real
                todos_itens_sb.append({
                    "ml_id": item.get("id", ""),
                    "conta": conta_nome,
                    "sku": sku_raw,
                    "titulo": (item.get("title") or "")[:500],
                    "preco": float(item.get("price") or 0),
                    "estoque": estoque,
                    "status": status_item,
                    "thumbnail": item.get("thumbnail", ""),
                    "vinculado": vinculado,
                    "sync_at": now_iso,
                })
                total_anuncios += 1

        _ml_anuncios_cache_save(por_sku)

        # Salva no Supabase (silencioso se tabela nao existir)
        try:
            for i in range(0, len(todos_itens_sb), 100):
                lote = todos_itens_sb[i:i+100]
                # on_conflict=ml_id,conta é OBRIGATÓRIO: sem ele o PostgREST resolve o
                # upsert pela PK (id) e bate na unique (ml_id,conta) → 409 e o lote aborta.
                # Era a raiz de TUDO: SKUs nunca atualizavam, pausados/encerrados nunca entravam.
                r_up = requests.post(
                    f"{_WRX_SB_URL}/rest/v1/ml_anuncios?on_conflict=ml_id,conta",
                    headers={**_wrx_headers(), "Prefer": "resolution=merge-duplicates"},
                    json=lote, timeout=30
                )
                if r_up.status_code not in (200, 201, 204):
                    break
        except Exception:
            pass

        return jsonify({
            "ok": True,
            "totalSkus": len(por_sku),
            "totalAnuncios": total_anuncios,
            "totalVinculados": total_vinculados,
            "erros": erros,
        })

    @app.route("/integracoes/mercadolivre/debug-sync", methods=["GET", "OPTIONS"])
    def ml_debug_sync():
        """Diagnóstico: conta itens antes dos filtros de SKU/estoque."""
        if request.method == "OPTIONS":
            return _options_resp()
        tokens_data = _ml_load_tokens()
        if not tokens_data:
            return jsonify({"erro": "sem tokens"}), 401
        resultado = []
        for conta_nome in list(tokens_data.keys()):
            token = _ml_get_user_token(conta_nome)
            if not token:
                resultado.append({"conta": conta_nome, "erro": "token invalido"})
                continue
            user_id = tokens_data.get(conta_nome, {}).get("user_id", "")
            if not user_id:
                try:
                    _r = requests.get("https://api.mercadolibre.com/users/me",
                                      headers={"Authorization": f"Bearer {token}"}, timeout=10)
                    if _r.status_code == 200:
                        user_id = str(_r.json().get("id", ""))
                except Exception:
                    pass
            if not user_id:
                resultado.append({"conta": conta_nome, "erro": "user_id nao encontrado"})
                continue
            ids = _ml_buscar_todos_ids(token, user_id, "active")
            # Busca amostra de 5 itens com todos os campos incluindo seller_custom_field
            amostra_ids = ids[:5]
            amostra_raw = []
            if amostra_ids:
                _r2 = requests.get(
                    "https://api.mercadolibre.com/items",
                    params={"ids": ",".join(amostra_ids),
                            "attributes": "id,title,price,available_quantity,seller_sku,seller_custom_field,status,thumbnail,variations,attributes"},
                    headers={"Authorization": f"Bearer {token}"}, timeout=20
                )
                if _r2.status_code == 200:
                    for entry in _r2.json():
                        if entry.get("code") == 200:
                            amostra_raw.append(entry.get("body", {}))
            sem_sku = sum(1 for a in amostra_raw if not _ml_extrair_sku(a))
            sem_estoque = sum(1 for a in amostra_raw if (a.get("available_quantity") or 0) <= 0)
            resultado.append({
                "conta": conta_nome,
                "user_id": user_id,
                "total_ids_ativos": len(ids),
                "amostra_5_itens": [
                    {"id": a.get("id"),
                     "sku_item": a.get("seller_sku") or a.get("seller_custom_field"),
                     "sku_extraido": _ml_extrair_sku(a),
                     "tem_variacoes": len(a.get("variations") or []),
                     "estoque": a.get("available_quantity"), "titulo": a.get("title","")[:50]}
                    for a in amostra_raw
                ],
                "sem_sku_e_custom_na_amostra": sem_sku,
                "sem_estoque_na_amostra": sem_estoque,
            })
        return jsonify({"resultado": resultado})

    # ── ML: helpers de vínculo/painel ────────────────────────────────────────
    def _sb_count(r):
        try:
            return int(r.headers.get("Content-Range", "").split("/")[-1])
        except Exception:
            return 0

    def _ml_norm_txt(s):
        return " ".join(str(s or "").lower().replace("*", " ").split())

    def _ml_conta_user_id(conta, token):
        tokens = _ml_load_tokens()
        user_id = tokens.get(conta, {}).get("user_id", "")
        if not user_id:
            try:
                _r = requests.get("https://api.mercadolibre.com/users/me",
                                  headers={"Authorization": f"Bearer {token}"}, timeout=10)
                if _r.status_code == 200:
                    user_id = str(_r.json().get("id", ""))
            except Exception:
                pass
        return user_id

    def _sb_get_all(path_query, page=1000, teto=50000):
        """Busca TODAS as linhas do Supabase paginando (PostgREST limita 1000/req)."""
        out = []
        offset = 0
        sep = "&" if "?" in path_query else "?"
        while offset < teto:
            url = f"{_WRX_SB_URL}/rest/v1/{path_query}{sep}limit={page}&offset={offset}"
            try:
                r = requests.get(url, headers=_wrx_headers(), timeout=30)
            except Exception:
                break
            if r.status_code != 200:
                break
            lote = r.json()
            if not lote:
                break
            out.extend(lote)
            if len(lote) < page:
                break
            offset += page
        return out

    @app.route("/integracoes/mercadolivre/status", methods=["GET", "OPTIONS"])
    def ml_status():
        if request.method == "OPTIONS":
            return _options_resp()
        conta = request.args.get("conta", "default")
        token = _ml_get_user_token(conta)
        if not token:
            return jsonify({"ok": False, "conectado": False, "erro": "conta nao autorizada"}), 404
        return jsonify({"ok": True, "conectado": True, "conta": conta})

    @app.route("/integracoes/mercadolivre/totais", methods=["GET", "OPTIONS"])
    def ml_totais():
        if request.method == "OPTIONS":
            return _options_resp()
        conta = request.args.get("conta", "default")
        token = _ml_get_user_token(conta)
        if not token:
            return jsonify({"ok": False, "erro": "conta nao autorizada"}), 404
        user_id = _ml_conta_user_id(conta, token)
        total_ml = 0
        ativos = 0
        try:
            ra = requests.get(f"https://api.mercadolibre.com/users/{user_id}/items/search",
                              params={"status": "active", "limit": 1},
                              headers={"Authorization": f"Bearer {token}"}, timeout=15)
            if ra.status_code == 200:
                ativos = ra.json().get("paging", {}).get("total", 0)
            rt = requests.get(f"https://api.mercadolibre.com/users/{user_id}/items/search",
                              params={"limit": 1},
                              headers={"Authorization": f"Bearer {token}"}, timeout=15)
            if rt.status_code == 200:
                total_ml = rt.json().get("paging", {}).get("total", 0)
        except Exception:
            pass
        sincronizados = 0
        com_sku = 0
        try:
            rs = requests.get(f"{_WRX_SB_URL}/rest/v1/ml_anuncios?select=ml_id&conta=eq.{conta}",
                              headers={**_wrx_headers(), "Prefer": "count=exact", "Range": "0-0"}, timeout=15)
            sincronizados = _sb_count(rs)
            rv = requests.get(f"{_WRX_SB_URL}/rest/v1/ml_anuncios?select=ml_id&conta=eq.{conta}&vinculado=eq.true",
                              headers={**_wrx_headers(), "Prefer": "count=exact", "Range": "0-0"}, timeout=15)
            com_sku = _sb_count(rv)
        except Exception:
            pass
        return jsonify({"ok": True, "totalML": total_ml, "ativos": ativos,
                        "comSku": com_sku, "sincronizados": sincronizados})

    @app.route("/integracoes/mercadolivre/anuncios", methods=["GET", "OPTIONS"])
    def ml_anuncios():
        if request.method == "OPTIONS":
            return _options_resp()
        conta = request.args.get("conta", "default")
        try:
            limit = int(request.args.get("limit", 80))
        except Exception:
            limit = 80
        try:
            offset = int(request.args.get("offset", 0))
        except Exception:
            offset = 0
        token = _ml_get_user_token(conta)
        if not token:
            return jsonify({"ok": False, "erro": "conta nao autorizada"}), 404
        user_id = _ml_conta_user_id(conta, token)
        status_filtro = request.args.get("status", "")  # "", "active", "paused", "closed"
        grupos = []
        ids = []
        total = 0
        try:
            params = {"limit": min(limit, 50), "offset": offset}
            if status_filtro:
                params["status"] = status_filtro
            r = requests.get(f"https://api.mercadolibre.com/users/{user_id}/items/search",
                             params=params,
                             headers={"Authorization": f"Bearer {token}"}, timeout=15)
            if r.status_code == 200:
                d = r.json()
                ids = d.get("results", [])
                total = d.get("paging", {}).get("total", 0)
        except Exception:
            pass
        itens = []
        if ids:
            for it in _ml_buscar_detalhes_lote(token, ids[:limit]):
                # peso dos atributos (PACKAGE_WEIGHT / WEIGHT) — nem todo anúncio tem
                peso = ""
                for a in (it.get("attributes") or []):
                    if a.get("id") in ("PACKAGE_WEIGHT", "WEIGHT", "GROSS_WEIGHT"):
                        peso = a.get("value_name") or ""
                        break
                shipping = it.get("shipping") or {}
                # frete grátis: ML marca de 3 jeitos diferentes
                frete_gratis = bool(
                    shipping.get("free_shipping")
                    or ("free_shipping" in (shipping.get("tags") or []))
                    or any(m.get("free_shipping") for m in (shipping.get("free_methods") or []))
                )
                itens.append({
                    "mlId": it.get("id", ""),
                    "titulo": it.get("title", ""),
                    "preco": it.get("price", 0),
                    "estoque": it.get("available_quantity", 0),
                    "status": it.get("status", "active"),
                    "grupo": it.get("status", "active"),
                    "thumbnail": it.get("thumbnail", ""),
                    "sku": _ml_extrair_sku(it),
                    "condicao": it.get("condition", ""),        # new / used
                    "freteGratis": frete_gratis,
                    "peso": peso,
                    "data": it.get("date_created", ""),         # pra ordenar antigo/novo
                    "permalink": it.get("permalink", ""),
                })
        grupos.append({"key": "active", "label": "Ativos", "total": total, "itens": itens})
        return jsonify({"ok": True, "grupos": grupos})

    # cache de IDs por conta (scroll_id traz TODOS, passa do limite de 1000 do offset)
    _ml_ids_cache = {}  # conta -> {"ids": [...], "ts": epoch}

    def _ml_montar_item(it):
        peso = ""
        for a in (it.get("attributes") or []):
            if a.get("id") in ("PACKAGE_WEIGHT", "WEIGHT", "GROSS_WEIGHT"):
                peso = a.get("value_name") or ""
                break
        shipping = it.get("shipping") or {}
        frete_gratis = bool(
            shipping.get("free_shipping")
            or ("free_shipping" in (shipping.get("tags") or []))
            or any(m.get("free_shipping") for m in (shipping.get("free_methods") or []))
        )
        return {
            "mlId": it.get("id", ""), "titulo": it.get("title", ""),
            "preco": it.get("price", 0), "estoque": it.get("available_quantity", 0),
            "status": it.get("status", "active"), "grupo": it.get("status", "active"),
            "thumbnail": it.get("thumbnail", ""), "sku": _ml_extrair_sku(it),
            "condicao": it.get("condition", ""), "freteGratis": frete_gratis,
            "peso": peso, "data": it.get("date_created", ""),
            "permalink": it.get("permalink", ""),
        }

    @app.route("/integracoes/mercadolivre/anuncios-pagina", methods=["GET", "OPTIONS"])
    def ml_anuncios_pagina():
        """Página de anúncios usando scroll_id (pega TODOS, sem limite de 1000).
        ?conta=&pagina=0&tamanho=50 — a 1ª chamada escaneia todos os IDs e cacheia."""
        if request.method == "OPTIONS":
            return _options_resp()
        conta = request.args.get("conta", "default")
        try:
            pagina = int(request.args.get("pagina", 0))
            tamanho = min(int(request.args.get("tamanho", 50)), 50)
        except Exception:
            pagina, tamanho = 0, 50
        token = _ml_get_user_token(conta)
        if not token:
            return jsonify({"ok": False, "erro": "conta nao autorizada"}), 404
        user_id = _ml_conta_user_id(conta, token)
        # cache de IDs por 5 min
        cache = _ml_ids_cache.get(conta)
        if not cache or (time.time() - cache.get("ts", 0)) > 300:
            ids = _ml_buscar_todos_ids(token, user_id, status="active")
            _ml_ids_cache[conta] = {"ids": ids, "ts": time.time()}
        else:
            ids = cache["ids"]
        total = len(ids)
        ini = pagina * tamanho
        fim = ini + tamanho
        ids_pagina = ids[ini:fim]
        itens = [_ml_montar_item(it) for it in _ml_buscar_detalhes_lote(token, ids_pagina)] if ids_pagina else []
        return jsonify({
            "ok": True, "total": total, "pagina": pagina,
            "tem_mais": fim < total, "itens": itens
        })

    @app.route("/integracoes/mercadolivre/sem-vinculo", methods=["GET", "OPTIONS"])
    def ml_sem_vinculo():
        if request.method == "OPTIONS":
            return _options_resp()
        conta = request.args.get("conta", "default")
        itens = []
        try:
            dados = _sb_get_all(
                f"ml_anuncios?select=ml_id,titulo,sku,preco,estoque"
                f"&conta=eq.{conta}&vinculado=eq.false&status=eq.active"
            )
            for d in dados:
                itens.append({
                    "mlId": d.get("ml_id", ""),
                    "titulo": d.get("titulo", ""),
                    "sku": d.get("sku", ""),
                    "preco": d.get("preco", 0),
                })
        except Exception:
            pass
        return jsonify({"ok": True, "itens": itens})

    @app.route("/integracoes/mercadolivre/buscar-produto", methods=["GET", "OPTIONS"])
    def ml_buscar_produto():
        if request.method == "OPTIONS":
            return _options_resp()
        q = request.args.get("q", "").strip()
        if not q:
            return jsonify([])
        out = []
        try:
            r = requests.get(
                f"{_WRX_SB_URL}/rest/v1/pecas_estoque",
                params={"select": "sku,titulo",
                        "or": f"(titulo.ilike.*{q}*,sku.ilike.*{q}*)",
                        "limit": 20},
                headers=_wrx_headers(), timeout=15
            )
            if r.status_code == 200:
                for p in r.json():
                    out.append({"titulo": p.get("titulo", ""), "sku": p.get("sku", "")})
        except Exception:
            pass
        return jsonify(out)

    @app.route("/integracoes/mercadolivre/associar", methods=["POST", "OPTIONS"])
    def ml_associar():
        if request.method == "OPTIONS":
            return _options_resp()
        data = request.get_json(silent=True) or {}
        ml_id = data.get("mlId", "")
        sku = data.get("skuInterno", "")
        conta = data.get("nome", "default")
        if not ml_id or not sku:
            return jsonify({"ok": False, "erro": "mlId e skuInterno obrigatorios"}), 400
        try:
            r = requests.patch(
                f"{_WRX_SB_URL}/rest/v1/ml_anuncios?ml_id=eq.{ml_id}&conta=eq.{conta}",
                headers=_wrx_headers(),
                json={"sku": sku, "vinculado": True}, timeout=15
            )
            if r.status_code in (200, 204):
                return jsonify({"ok": True})
            return jsonify({"ok": False, "erro": r.text[:200]}), r.status_code
        except Exception as e:
            return jsonify({"ok": False, "erro": str(e)}), 500

    @app.route("/integracoes/mercadolivre/vincular-titulo", methods=["POST", "GET", "OPTIONS"])
    def ml_vincular_titulo():
        if request.method == "OPTIONS":
            return _options_resp()
        conta = request.args.get("conta", "default")
        # Peças do sistema (somente liga a peças que JÁ existem) — paginado
        pecas = []
        try:
            for p in _sb_get_all("pecas_estoque?select=sku,titulo"):
                nome = _ml_norm_txt(p.get("titulo"))
                if nome and len(nome) >= 4:
                    pecas.append((p.get("sku", ""), nome))
        except Exception:
            pass
        # Anúncios ativos ainda sem vínculo — paginado
        ads = []
        try:
            ads = _sb_get_all(
                f"ml_anuncios?select=ml_id,titulo"
                f"&conta=eq.{conta}&vinculado=eq.false&status=eq.active"
            )
        except Exception:
            pass
        vinculados = 0
        ambiguos = 0
        sem_match = 0
        for ad in ads:
            t = _ml_norm_txt(ad.get("titulo"))
            if not t:
                sem_match += 1
                continue
            achados = {sku for (sku, nome) in pecas if nome in t or t in nome}
            if len(achados) == 1:
                sku = next(iter(achados))
                try:
                    requests.patch(
                        f"{_WRX_SB_URL}/rest/v1/ml_anuncios?ml_id=eq.{ad['ml_id']}&conta=eq.{conta}",
                        headers=_wrx_headers(),
                        json={"sku": sku, "vinculado": True}, timeout=15
                    )
                    vinculados += 1
                except Exception:
                    sem_match += 1
            elif len(achados) > 1:
                ambiguos += 1
            else:
                sem_match += 1
        ja_vinculados = 0
        try:
            rj = requests.get(
                f"{_WRX_SB_URL}/rest/v1/ml_anuncios?select=ml_id&conta=eq.{conta}&vinculado=eq.true",
                headers={**_wrx_headers(), "Prefer": "count=exact", "Range": "0-0"}, timeout=15
            )
            ja_vinculados = _sb_count(rj)
        except Exception:
            pass
        return jsonify({"ok": True, "vinculados": vinculados, "jaVinculados": ja_vinculados,
                        "ambiguos": ambiguos, "semMatch": sem_match})

    @app.route("/integracoes/mercadolivre/criar-e-vincular", methods=["POST", "OPTIONS"])
    def ml_criar_e_vincular():
        if request.method == "OPTIONS":
            return _options_resp()
        data = request.get_json(silent=True) or {}
        sku = str(data.get("sku", "")).strip()
        nome = str(data.get("nome", "")).strip()
        if not sku or not nome:
            return jsonify({"ok": False, "erro": "nome e sku obrigatorios"}), 400
        ml_id = data.get("mlId", "")
        conta = data.get("nomeConta", "default")
        # Quantidade nasce com o estoque do anúncio no ML (busca na tabela ml_anuncios)
        qtd = int(data.get("qtd") or 1)
        if ml_id:
            try:
                rq = requests.get(
                    f"{_WRX_SB_URL}/rest/v1/ml_anuncios"
                    f"?select=estoque&ml_id=eq.{ml_id}&conta=eq.{conta}&limit=1",
                    headers=_wrx_headers(), timeout=10
                )
                if rq.status_code == 200 and rq.json():
                    qtd = int(rq.json()[0].get("estoque") or qtd)
            except Exception:
                pass
        # Cria/atualiza a peça (SKU duplicado -> atualiza via merge-duplicates)
        peca = {
            "sku": sku,
            "titulo": nome,
            "preco": float(data.get("preco") or 0),
            "qtd": qtd,
            "atualizado": _datetime.utcnow().isoformat() + "Z",
        }
        try:
            rc = requests.post(
                f"{_WRX_SB_URL}/rest/v1/pecas_estoque",
                headers={**_wrx_headers(), "Prefer": "resolution=merge-duplicates"},
                json=peca, timeout=15
            )
            if rc.status_code not in (200, 201, 204):
                return jsonify({"ok": False, "erro": f"falha ao criar peça: {rc.text[:200]}"}), rc.status_code
        except Exception as e:
            return jsonify({"ok": False, "erro": str(e)}), 500
        # Vincula o anúncio à peça
        if ml_id:
            try:
                requests.patch(
                    f"{_WRX_SB_URL}/rest/v1/ml_anuncios?ml_id=eq.{ml_id}&conta=eq.{conta}",
                    headers=_wrx_headers(),
                    json={"sku": sku, "vinculado": True}, timeout=15
                )
            except Exception:
                pass
        return jsonify({"ok": True})

    @app.route("/integracoes/mercadolivre/video", methods=["POST", "OPTIONS"])
    def ml_video():
        if request.method == "OPTIONS":
            return _options_resp()
        data = request.get_json(force=True) or {}
        item_id = data.get("mlId") or data.get("item_id", "")
        video_id = data.get("videoId") or data.get("video_id", "")
        conta_nome = data.get("conta") or data.get("nome") or "default"
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
        conta_req = request.args.get("conta", "").strip()
        status = request.args.get("status", "UNANSWERED")
        tokens = _ml_load_tokens()
        if not tokens:
            return jsonify({"ok": False, "erro": "Mercado Livre nao autorizado"}), 401
        contas = [conta_req] if conta_req else list(tokens.keys())
        perguntas = []
        erros = []
        for conta in contas:
            token = _ml_get_user_token(conta)
            if not token:
                continue
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
                continue
            try:
                _r = requests.get(
                    "https://api.mercadolibre.com/questions/search",
                    params={"seller_id": user_id, "status": status,
                            "sort_fields": "date_created", "sort_types": "DESC", "limit": 50},
                    headers={"Authorization": f"Bearer {token}"}, timeout=15
                )
                if _r.status_code != 200:
                    erros.append(f"{conta}: HTTP {_r.status_code}")
                    continue
                d = _r.json()
                for q in d.get("questions", []):
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
                        "conta": conta,
                        "id": q.get("id"),
                        "texto": q.get("text", ""),
                        "status": q.get("status", ""),
                        "item_id": item_id,
                        "item_titulo": item_title,
                        "comprador_id": q.get("from", {}).get("id", ""),
                        "data": q.get("date_created", ""),
                        "resposta": q.get("answer", {}).get("text", "") if q.get("answer") else None,
                    })
            except Exception as _e:
                erros.append(f"{conta}: {_e}")
        perguntas.sort(key=lambda x: x.get("data", ""), reverse=True)
        return jsonify({"ok": True, "total": len(perguntas), "perguntas": perguntas, "erros": erros})

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

    @app.route("/integracoes/mercadolivre/visitas", methods=["GET", "OPTIONS"])
    def ml_visitas():
        """Visitas por dia dos anúncios da conta (últimos N dias)."""
        if request.method == "OPTIONS":
            return _options_resp()
        conta = request.args.get("conta", "default")
        try:
            dias = min(int(request.args.get("dias", "30")), 60)
        except Exception:
            dias = 30
        token = _ml_get_user_token(conta)
        if not token:
            return jsonify({"ok": False, "erro": "conta nao autorizada"}), 401
        user_id = _ml_conta_user_id(conta, token)
        if not user_id:
            return jsonify({"ok": False, "erro": "user_id nao encontrado"}), 400
        try:
            # visitas totais do usuário por dia (time_window: last N days, unit day)
            r = requests.get(
                f"https://api.mercadolibre.com/users/{user_id}/items_visits/time_window",
                params={"last": dias, "unit": "day"},
                headers={"Authorization": f"Bearer {token}"}, timeout=20
            )
            if r.status_code != 200:
                # fallback: endpoint alternativo (alguns users usam /visits/)
                r = requests.get(
                    f"https://api.mercadolibre.com/users/{user_id}/visits/time_window",
                    params={"last": dias, "unit": "day"},
                    headers={"Authorization": f"Bearer {token}"}, timeout=20
                )
            if r.status_code != 200:
                return jsonify({"ok": False, "erro": r.text[:200], "conta": conta}), r.status_code
            d = r.json()
            # normaliza: lista de {data, visitas}
            results = d.get("results", []) if isinstance(d, dict) else []
            por_dia = [{"data": x.get("date", ""), "visitas": x.get("total", 0)} for x in results]
            total = d.get("total_visits", sum(p["visitas"] for p in por_dia))
            return jsonify({"ok": True, "conta": conta, "total": total, "por_dia": por_dia})
        except Exception as _e:
            return jsonify({"ok": False, "erro": str(_e), "conta": conta}), 500

    @app.route("/integracoes/mercadolivre/reclamacoes", methods=["GET", "OPTIONS"])
    def ml_reclamacoes():
        """Reclamações/claims abertas da conta (precisam de resolução)."""
        if request.method == "OPTIONS":
            return _options_resp()
        conta_req = request.args.get("conta", "").strip()
        tokens = _ml_load_tokens()
        if not tokens:
            return jsonify({"ok": False, "erro": "Mercado Livre nao autorizado"}), 401
        contas = [conta_req] if conta_req else list(tokens.keys())
        reclamacoes = []
        for conta in contas:
            token = _ml_get_user_token(conta)
            if not token:
                continue
            try:
                r = requests.get(
                    "https://api.mercadolibre.com/post-purchase/v1/claims/search",
                    params={"status": "opened", "limit": 50},
                    headers={"Authorization": f"Bearer {token}"}, timeout=15
                )
                if r.status_code != 200:
                    continue
                for c in r.json().get("data", []):
                    reclamacoes.append({
                        "conta": conta,
                        "id": c.get("id"),
                        "tipo": c.get("type", ""),
                        "status": c.get("status", ""),
                        "stage": c.get("stage", ""),
                        "motivo": c.get("reason_id", ""),
                        "order_id": (c.get("resource_id") if c.get("resource") == "order" else ""),
                        "data": c.get("date_created", ""),
                    })
            except Exception as _e:
                print(f"[CLAIMS] erro conta {conta}: {_e}")
        return jsonify({"ok": True, "total": len(reclamacoes), "reclamacoes": reclamacoes})

    @app.route("/integracoes/mercadolivre/vendas-recentes", methods=["GET", "OPTIONS"])
    def ml_vendas_recentes():
        if request.method == "OPTIONS":
            return _options_resp()
        conta_req = request.args.get("conta", "").strip()
        dias = int(request.args.get("dias", "30"))
        tokens = _ml_load_tokens()
        if not tokens:
            return jsonify({"ok": False, "erro": "Mercado Livre nao autorizado"}), 401
        contas = [conta_req] if conta_req else list(tokens.keys())
        from datetime import timedelta
        date_from = (_datetime.utcnow() - timedelta(days=dias)).strftime("%Y-%m-%dT00:00:00.000-00:00")
        vendas = []
        erros = []
        for conta in contas:
            token = _ml_get_user_token(conta)
            if not token:
                continue
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
                continue
            try:
                _r = requests.get(
                    "https://api.mercadolibre.com/orders/search",
                    params={"seller": user_id, "sort": "date_desc", "limit": 50,
                            "date_created.from": date_from},
                    headers={"Authorization": f"Bearer {token}"}, timeout=15
                )
                if _r.status_code != 200:
                    erros.append(f"{conta}: HTTP {_r.status_code}")
                    continue
                d = _r.json()
                for o in d.get("results", []):
                    itens = [{"titulo": i.get("item", {}).get("title", ""),
                               "sku": i.get("item", {}).get("seller_sku", ""),
                               "qty": i.get("quantity", 1),
                               "preco": i.get("unit_price", 0)} for i in o.get("order_items", [])]
                    vendas.append({
                        "marketplace": "ml",
                        "conta": conta,
                        "order_id": o.get("id"),
                        "status": o.get("status", ""),
                        "data": o.get("date_created", ""),
                        "total": o.get("total_amount", 0),
                        "comprador": o.get("buyer", {}).get("nickname", ""),
                        "itens": itens,
                    })
            except Exception as _e:
                erros.append(f"{conta}: {_e}")
        vendas.sort(key=lambda x: x.get("data", ""), reverse=True)
        return jsonify({"ok": True, "vendas": vendas, "total": len(vendas), "erros": erros})

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
        forcar = request.args.get("forcar") in ("1", "true", "sim")
        auth_url = None
        # gera o link de autorização quando não há token OU quando forçado (reconectar)
        if configured and (not token_saved or forcar):
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
            _olx_token_mem = {
                "access_token": _d.get("access_token", ""),
                "refresh_token": _d.get("refresh_token", ""),
                "expires_at": time.time() + _d.get("expires_in", 3600),
            }
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
            _olx_token_mem = {
                "access_token": _d.get("access_token", ""),
                "refresh_token": _d.get("refresh_token", ""),
                "expires_at": time.time() + _d.get("expires_in", 3600),
            }
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
        # Renova o token se está expirando e há refresh_token (OLX expira em ~1h)
        if _olx_token_mem.get("expires_at", 0) - time.time() < 120 and _olx_token_mem.get("refresh_token"):
            try:
                _rt = requests.post("https://auth.olx.com.br/oauth/token", data={
                    "grant_type": "refresh_token",
                    "client_id": OLX_CLIENT_ID,
                    "client_secret": OLX_CLIENT_SECRET,
                    "refresh_token": _olx_token_mem.get("refresh_token", ""),
                }, timeout=15)
                if _rt.status_code == 200:
                    _rd = _rt.json()
                    _olx_token_mem = {
                        "access_token": _rd.get("access_token", ""),
                        "refresh_token": _rd.get("refresh_token", _olx_token_mem.get("refresh_token", "")),
                        "expires_at": time.time() + _rd.get("expires_in", 3600),
                    }
                    try:
                        with open(_OLX_TOKENS_FILE, "w") as _f:
                            json.dump(_olx_token_mem, _f)
                    except Exception:
                        pass
                    print("[OLX] token renovado via refresh_token")
                else:
                    print(f"[OLX] refresh falhou: {_rt.status_code} {_rt.text[:150]}")
            except Exception as _e:
                print(f"[OLX] erro refresh: {_e}")
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
                # A OLX devolve HTTP 200 MESMO com erro interno (ex.: statusCode -6 "Without
                # permission", token null). Só é sucesso de verdade se statusCode >= 0 e veio token.
                _sc = _resp.get("statusCode")
                _tok = _resp.get("token")
                if (_sc is not None and _sc < 0) or (not _tok and not _resp.get("ad_list")):
                    msg = _resp.get("statusMessage") or "OLX recusou o anuncio"
                    erros_int = _resp.get("errors") or []
                    if erros_int:
                        msg += " — " + "; ".join(str(e) for e in erros_int)[:200]
                    if "permission" in msg.lower():
                        msg += " (token expirado/sem permissao — reconecte a OLX)"
                    return jsonify({"ok": False, "erro": msg, "raw": _resp}), 400
                _ad = (_resp.get("ad_list") or [{}])[0]
                return jsonify({"ok": True, "status": _ad.get("status", ""), "id": _ad.get("id", "") or _tok, "raw": _resp})
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

    # ── WhatsApp (WAHA) — status / QR / start / logout ──────────────────────────
    def _waha_h(extra=None):
        h = {"X-Api-Key": WAHA_API_KEY}
        if extra:
            h.update(extra)
        return h

    def _waha_webhook_body():
        return {
            "name": WAHA_SESSION,
            "config": {"webhooks": [{
                "url": WAHA_WEBHOOK_URL,
                "events": ["message"],
                "retries": {"policy": "linear", "delaySeconds": 2, "attempts": 3},
            }]},
        }

    @app.route("/integracoes/whatsapp/status", methods=["GET", "OPTIONS"])
    def whatsapp_status():
        if request.method == "OPTIONS":
            return _options_resp()
        try:
            r = requests.get(f"{WAHA_BASE}/api/sessions/{WAHA_SESSION}", headers=_waha_h(), timeout=15)
            d = r.json() if r.status_code == 200 else {}
        except Exception as e:
            return jsonify({"configured": True, "status": "ERROR", "connected": False, "erro": str(e)})
        status = d.get("status", "UNKNOWN")
        me = d.get("me") or {}
        webhooks = (((d.get("config") or {}).get("webhooks")) or [])
        return jsonify({
            "configured": True,
            "status": status,                       # WORKING / SCAN_QR_CODE / STARTING / STOPPED / FAILED
            "connected": status == "WORKING",
            "precisaQr": status in ("SCAN_QR_CODE", "STOPPED", "FAILED"),
            "numero": (me.get("id") or "").split("@")[0],
            "nome": me.get("pushName") or "",
            "webhookOk": any(WAHA_WEBHOOK_URL in (w.get("url") or "") for w in webhooks),
        })

    @app.route("/integracoes/whatsapp/start", methods=["POST", "GET", "OPTIONS"])
    def whatsapp_start():
        if request.method == "OPTIONS":
            return _options_resp()
        body = _waha_webhook_body()
        out = {"ok": True}
        try:
            # Atualiza config (cria/garante webhook). Se a sessao nao existe, cria com start.
            r = requests.put(f"{WAHA_BASE}/api/sessions/{WAHA_SESSION}",
                             headers=_waha_h({"Content-Type": "application/json"}), json=body, timeout=40)
            if r.status_code in (400, 404):
                r = requests.post(f"{WAHA_BASE}/api/sessions",
                                  headers=_waha_h({"Content-Type": "application/json"}),
                                  json={**body, "start": True}, timeout=40)
            out["http"] = r.status_code
        except Exception as e:
            return jsonify({"ok": False, "erro": str(e)})
        # Garante que esta iniciada (gera QR se nao autenticada)
        try:
            requests.post(f"{WAHA_BASE}/api/sessions/{WAHA_SESSION}/start", headers=_waha_h(), timeout=20)
        except Exception:
            pass
        return jsonify(out)

    @app.route("/integracoes/whatsapp/qr", methods=["GET", "OPTIONS"])
    def whatsapp_qr():
        if request.method == "OPTIONS":
            return _options_resp()
        last = None
        for url in (f"{WAHA_BASE}/api/{WAHA_SESSION}/auth/qr",
                    f"{WAHA_BASE}/api/sessions/{WAHA_SESSION}/auth/qr"):
            try:
                r = requests.get(url, headers=_waha_h({"Accept": "image/png"}),
                                 params={"format": "image"}, timeout=20)
                ct = r.headers.get("Content-Type", "")
                if r.status_code == 200 and ct.startswith("image"):
                    return Response(r.content, mimetype=ct,
                                    headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"})
                last = {"http": r.status_code, "ct": ct, "body": r.text[:200]}
            except Exception as e:
                last = {"erro": str(e)}
        return jsonify({"erro": "qr_indisponivel", "detalhe": last})

    @app.route("/integracoes/whatsapp/logout", methods=["POST", "OPTIONS"])
    def whatsapp_logout():
        if request.method == "OPTIONS":
            return _options_resp()
        try:
            requests.post(f"{WAHA_BASE}/api/sessions/{WAHA_SESSION}/logout", headers=_waha_h(), timeout=20)
            requests.post(f"{WAHA_BASE}/api/sessions/{WAHA_SESSION}/start", headers=_waha_h(), timeout=20)
        except Exception as e:
            return jsonify({"ok": False, "erro": str(e)})
        return jsonify({"ok": True})

    # ── Disparo de pedidos aos funcionários, respeitando a janela de cada um ─────
    #    Centraliza TODO o aviso (o n8n nao envia mais). Roda no cron 2min:
    #    cada um recebe no horario dele; o que cai fora da janela entra na
    #    proxima (fim de semana -> segunda 9h). Controla quem ja recebeu cada pedido.
    FUNCS_PEDIDO = {
        "robson": "5521971396951",
        "rafael": "5521993745355",
        "geisa":  "5521985243301",
    }

    def _func_em_janela(emp, wd, h):
        # wd: Mon=0 .. Sun=6 ; h: hora 0-23 (America/Sao_Paulo)
        if 0 <= wd <= 4 and 9 <= h < 17:          # seg-sex 9-17: todos
            return True
        if wd == 5 and 9 <= h < 13:               # sabado 9-13: rafael e geisa
            return emp in ("rafael", "geisa")
        return False                              # domingo: ninguem

    @app.route("/integracoes/whatsapp/pedidos-manha", methods=["GET", "POST", "OPTIONS"])
    def whatsapp_pedidos_manha():
        if request.method == "OPTIONS":
            return _options_resp()
        import datetime as _dt, json as _json
        # Diagnóstico read-only: onde o estado é gravado e quantos pedidos já avisados
        if request.args.get("info") == "1":
            _sf = os.path.join(_INTEG_DIR, "avisos_func.json")
            _ex = os.path.exists(_sf)
            _cnt = 0
            try:
                if _ex:
                    _cnt = len(_json.load(open(_sf, encoding="utf-8")))
            except Exception:
                pass
            return jsonify({"ok": True, "integ_dir": _INTEG_DIR, "tem_volume": bool(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")), "state_existe": _ex, "pedidos_rastreados": _cnt})
        SP = _dt.timezone(_dt.timedelta(hours=-3))   # Brasília (sem horário de verão)
        now = _dt.datetime.now(SP)
        wd, h = now.weekday(), now.hour
        abertos = [e for e in FUNCS_PEDIDO if _func_em_janela(e, wd, h)]
        if not abertos:
            return jsonify({"ok": True, "skip": "ninguem em janela", "wd": wd, "h": h})
        desde = (now - _dt.timedelta(hours=100)).astimezone(_dt.timezone.utc).isoformat()
        try:
            rows = requests.get(
                f"{_WRX_SB_URL}/rest/v1/pedidos"
                f"?select=id,peca,veiculo,ano,lado&status=in.(aguardando,verificando)"
                f"&criado_em=gte.{_urlparse.quote(desde)}&order=criado_em.asc",
                headers={"apikey": _WRX_SB_KEY, "Authorization": f"Bearer {_WRX_SB_KEY}"},
                timeout=20
            ).json()
        except Exception as e:
            return jsonify({"ok": False, "erro": str(e)})
        if not isinstance(rows, list):
            rows = []
        state_file = os.path.join(_INTEG_DIR, "avisos_func.json")
        primeira_vez = not os.path.exists(state_file)
        if primeira_vez:
            # 1º deploy: nao dispara o historico (evita blast); marca tudo como avisado.
            seed = {str(p.get("id")): list(FUNCS_PEDIDO.keys()) for p in rows if p.get("id")}
            try:
                with open(state_file, "w", encoding="utf-8") as f:
                    _json.dump(seed, f)
            except Exception:
                pass
            return jsonify({"ok": True, "seed": len(seed), "msg": "primeira execucao - historico marcado, novos pedidos a partir de agora"})
        try:
            estado = _json.load(open(state_file, encoding="utf-8")) if os.path.exists(state_file) else {}
        except Exception:
            estado = {}
        ids_atuais = {str(p.get("id")) for p in rows if p.get("id")}
        estado = {k: v for k, v in estado.items() if k in ids_atuais}   # prune antigos

        # ── 1 disparo por NÚMERO enquanto tiver pedido aberto ──────────────────
        #   rows vem ordenado por criado_em.asc. Para cada telefone só UM pedido
        #   pode disparar: o que já começou a ser avisado (está no estado) ou,
        #   se nenhum começou, o mais antigo aberto. Pedidos extras do mesmo
        #   número ficam de fora até o ativo ser resolvido (sair de
        #   aguardando/verificando), aí o próximo do cliente volta a disparar.
        def _fone_de(p):
            f = "".join(ch for ch in str(p.get("phone") or "") if ch.isdigit())
            return f or ("id:" + str(p.get("id") or ""))
        ativo_por_fone = {}
        for p in rows:                                   # 1ª passada: já em andamento
            pid = str(p.get("id") or "")
            if pid and pid in estado:
                ativo_por_fone.setdefault(_fone_de(p), pid)
        for p in rows:                                   # 2ª passada: mais antigo aberto
            pid = str(p.get("id") or "")
            if pid:
                ativo_por_fone.setdefault(_fone_de(p), pid)
        pids_ativos = set(ativo_por_fone.values())

        novos = 0
        for p in rows:
            pid = str(p.get("id") or "")
            peca = (p.get("peca") or "").strip()
            if not pid or not peca:
                continue
            if pid not in pids_ativos:                   # pedido repetido do mesmo número
                continue
            ja = set(estado.get(pid, []))
            alvos = [e for e in abertos if e not in ja]
            if not alvos:
                continue
            veic = (p.get("veiculo") or "").strip()
            ano = (p.get("ano") or "").strip()
            lado = (p.get("lado") or "").strip()
            msg = ("🔔 *Novo pedido*\nPeça: " + peca
                   + (("\nVeículo: " + veic + ((" " + ano) if ano else "")) if veic else "")
                   + (("\nLado: " + lado) if lado else "")
                   + "\n\nQuem tiver, responde aqui com *foto e valor* que eu encaminho pro cliente.")
            for e in alvos:
                ok, _ = _waha_enviar(FUNCS_PEDIDO[e], msg)
                if ok:
                    ja.add(e)
            estado[pid] = list(ja)
            novos += 1
        try:
            with open(state_file, "w", encoding="utf-8") as f:
                _json.dump(estado, f)
        except Exception:
            pass
        return jsonify({"ok": True, "abertos": abertos, "pedidos_disparados": novos})

    def _waha_numero_sessao():
        """Número do WhatsApp logado na sessão WAHA (pra mandar aviso pra si mesmo)."""
        try:
            r = requests.get(f"{WAHA_BASE}/api/sessions/{WAHA_SESSION}", headers=_waha_h(), timeout=10)
            if r.status_code == 200:
                return (r.json().get("me") or {}).get("id", "").split("@")[0]
        except Exception:
            pass
        return ""

    def _waha_enviar(numero, texto):
        """Envia texto via WAHA. numero: só dígitos (ex 5521999998888)."""
        num = "".join(ch for ch in str(numero) if ch.isdigit())
        if not num or not texto:
            return False, "numero ou texto vazio"
        try:
            r = requests.post(
                f"{WAHA_BASE}/api/sendText",
                headers=_waha_h({"Content-Type": "application/json"}),
                json={"session": WAHA_SESSION, "chatId": f"{num}@c.us", "text": texto},
                timeout=20
            )
            return (r.status_code in (200, 201)), r.text[:200]
        except Exception as e:
            return False, str(e)

    @app.route("/integracoes/whatsapp/enviar", methods=["POST", "OPTIONS"])
    def whatsapp_enviar():
        if request.method == "OPTIONS":
            return _options_resp()
        data = request.get_json(force=True) or {}
        numero = data.get("numero") or _waha_numero_sessao()
        texto = (data.get("texto") or "").strip()
        ok, msg = _waha_enviar(numero, texto)
        return jsonify({"ok": ok, "detalhe": msg, "numero": numero})

    # Memória simples (em arquivo) do que já foi avisado, pra não repetir
    _AVISADOS_FILE = os.path.join(_INTEG_DIR, "wrx_whatsapp_avisados.json")
    def _avisados_load():
        try:
            with open(_AVISADOS_FILE) as f:
                return set(json.load(f))
        except Exception:
            return set()
    def _avisados_save(s):
        try:
            with open(_AVISADOS_FILE, "w") as f:
                json.dump(list(s)[-500:], f)  # guarda só os últimos 500
        except Exception:
            pass

    @app.route("/integracoes/whatsapp/checar-novidades", methods=["GET", "POST", "OPTIONS"])
    def whatsapp_checar_novidades():
        """Checa perguntas/reclamações/vendas novas e avisa no WhatsApp da sessão.
        Idempotente: só avisa o que ainda não avisou (memória em arquivo)."""
        if request.method == "OPTIONS":
            return _options_resp()
        numero = _waha_numero_sessao()
        if not numero:
            return jsonify({"ok": False, "erro": "WhatsApp nao conectado"}), 400
        tokens = _ml_load_tokens()
        avisados = _avisados_load()
        # Se a memória está vazia (1ª vez / pós-restart), NÃO dispara avalanche:
        # marca o que existe como avisado sem enviar; só envia o que surgir depois.
        primeira_vez = (len(avisados) == 0)
        enviados = []
        from datetime import timedelta
        # só considera coisas das últimas 24h (evita perguntas/vendas antigas)
        limite = _datetime.utcnow() - timedelta(hours=24)
        date_from = limite.strftime("%Y-%m-%dT00:00:00.000-00:00")
        def _recente(data_str):
            """True se a data ISO está dentro das últimas 24h."""
            try:
                s = str(data_str or "")[:19]  # YYYY-MM-DDTHH:MM:SS
                d = _datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
                return d >= limite
            except Exception:
                return False
        def _processar(key, texto, data_item):
            """Marca como avisado e envia (se não for 1ª vez e for recente)."""
            if key in avisados:
                return
            avisados.add(key)
            if primeira_vez:
                return  # pós-restart: só registra, não envia
            if data_item is not None and not _recente(data_item):
                return  # antigo demais
            ok, _ = _waha_enviar(numero, texto)
            if ok:
                enviados.append(key)
        for conta in list(tokens.keys()):
            token = _ml_get_user_token(conta)
            if not token:
                continue
            user_id = tokens.get(conta, {}).get("user_id", "")
            if not user_id:
                try:
                    _r = requests.get("https://api.mercadolibre.com/users/me",
                                      headers={"Authorization": f"Bearer {token}"}, timeout=10)
                    if _r.status_code == 200:
                        user_id = str(_r.json().get("id", ""))
                except Exception:
                    pass
            # PERGUNTAS novas
            try:
                _r = requests.get("https://api.mercadolibre.com/questions/search",
                    params={"seller_id": user_id, "status": "UNANSWERED",
                            "sort_fields": "date_created", "sort_types": "DESC", "limit": 10},
                    headers={"Authorization": f"Bearer {token}"}, timeout=15)
                for q in (_r.json().get("questions", []) if _r.status_code == 200 else []):
                    txt = f"❓ *Pergunta nova* (ML {conta})\n{q.get('text','')}\n\nResponda no painel."
                    _processar(f"perg:{q.get('id')}", txt, q.get("date_created"))
            except Exception:
                pass
            # VENDAS novas
            try:
                _r = requests.get("https://api.mercadolibre.com/orders/search",
                    params={"seller": user_id, "sort": "date_desc", "limit": 10,
                            "date_created.from": date_from},
                    headers={"Authorization": f"Bearer {token}"}, timeout=15)
                for o in (_r.json().get("results", []) if _r.status_code == 200 else []):
                    itens = ", ".join(i.get("item", {}).get("title", "")[:40] for i in o.get("order_items", []))
                    total = o.get("total_amount", 0)
                    txt = f"🛒 *VENDA!* (ML {conta})\n{itens}\nTotal: R$ {total}"
                    _processar(f"venda:{o.get('id')}", txt, o.get("date_created"))
            except Exception:
                pass
            # RECLAMAÇÕES novas
            try:
                _r = requests.get("https://api.mercadolibre.com/post-purchase/v1/claims/search",
                    params={"status": "opened", "limit": 10},
                    headers={"Authorization": f"Bearer {token}"}, timeout=15)
                for c in (_r.json().get("data", []) if _r.status_code == 200 else []):
                    txt = f"⚠️ *RECLAMAÇÃO* (ML {conta})\nPedido #{c.get('resource_id','')}\nResolva no Mercado Livre."
                    _processar(f"recl:{c.get('id')}", txt, c.get("date_created"))
            except Exception:
                pass
        _avisados_save(avisados)
        return jsonify({"ok": True, "enviados": len(enviados), "detalhe": enviados})

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

    def _shopee_categoria_recomendada(access_token, shop_id_int, nome_produto):
        """Pergunta à Shopee qual a categoria-folha certa pro produto (recommend_category).
        Retorna o category_id (int) ou None."""
        try:
            ts = int(time.time())
            path = "/api/v2/product/category_recommend"
            sign = _shopee_sign(path, ts, access_token, shop_id_int)
            r = requests.get(
                f"{_SHOPEE_BASE}{path}",
                params={"partner_id": SHOPEE_PARTNER_ID, "timestamp": ts,
                        "access_token": access_token, "shop_id": shop_id_int,
                        "sign": sign, "item_name": nome_produto[:120]},
                timeout=15
            )
            d = r.json()
            cats = (d.get("response", {}) or {}).get("category_id", [])
            # a API devolve uma LISTA RANQUEADA de categorias-folha recomendadas;
            # a 1ª (mais relevante) é a folha certa pra usar no add_item
            if cats:
                return cats[0]
        except Exception as _e:
            print(f"[SHOPEE-CAT] erro recommend: {_e}")
        return None

    def _shopee_canais_habilitados(access_token, shop_id_int):
        """Lista os logistics_channel_id habilitados da loja (cada loja tem IDs diferentes)."""
        try:
            ts = int(time.time())
            path = "/api/v2/logistics/get_channel_list"
            sign = _shopee_sign(path, ts, access_token, shop_id_int)
            r = requests.get(f"{_SHOPEE_BASE}{path}",
                params={"partner_id": SHOPEE_PARTNER_ID, "timestamp": ts,
                        "access_token": access_token, "shop_id": shop_id_int, "sign": sign},
                timeout=20)
            d = r.json()
            canais = (d.get("response", {}) or {}).get("logistics_channel_list", [])
            # ignora canais de retirada/auto-coleta (não são entrega real e quebram a validação)
            entrega = [c for c in canais
                       if c.get("enabled") and c.get("logistics_channel_id")
                       and "retirada" not in (c.get("logistics_channel_name") or "").lower()]
            return [c.get("logistics_channel_id") for c in entrega]
        except Exception as _e:
            print(f"[SHOPEE-LOG] erro canais: {_e}")
            return []

    def _shopee_upload_imagens(access_token, shop_id_int, urls):
        """Baixa cada foto da URL e sobe pro media space da Shopee.
        Retorna lista de image_id (a Shopee exige image_id, não aceita URL externa de forma confiável)."""
        ids = []
        for u in urls[:9]:
            if not u:
                continue
            u = u.replace("http://", "https://")
            try:
                img = requests.get(u, timeout=20)
                if img.status_code != 200 or not img.content:
                    print(f"[SHOPEE-IMG] falha baixar {u[:60]} status={img.status_code}")
                    continue
                ts = int(time.time())
                path = "/api/v2/media_space/upload_image"
                sign = _shopee_sign(path, ts, access_token, shop_id_int)
                r = requests.post(
                    f"{_SHOPEE_BASE}{path}",
                    params={"partner_id": SHOPEE_PARTNER_ID, "timestamp": ts,
                            "access_token": access_token, "shop_id": shop_id_int, "sign": sign},
                    files={"image": ("foto.jpg", img.content, "image/jpeg")},
                    timeout=30
                )
                d = r.json()
                resp = d.get("response", {}) or {}
                # formato pode vir como image_info.image_id OU image_info_list[].image_info.image_id
                iid = (resp.get("image_info", {}) or {}).get("image_id")
                if not iid:
                    lst = resp.get("image_info_list", []) or []
                    if lst:
                        iid = (lst[0].get("image_info", {}) or {}).get("image_id")
                if iid:
                    ids.append(iid)
                else:
                    print(f"[SHOPEE-IMG] sem image_id na resposta: {str(d)[:200]}")
            except Exception as _e:
                print(f"[SHOPEE-IMG] erro upload: {_e}")
        return ids

    @app.route("/integracoes/shopee/diag-logistica", methods=["GET"])
    def shopee_diag_logistica():
        """Diagnóstico: canais de logística habilitados da loja."""
        sid = request.args.get("shop_id", "")
        tokens = _shopee_load_tokens()
        if not sid:
            sid = list(tokens.keys())[0] if tokens else ""
        access_token, shop_id_int = _shopee_get_token(sid)
        if not access_token:
            return jsonify({"erro": "sem token", "shop": sid}), 400
        try:
            ts = int(time.time())
            path = "/api/v2/logistics/get_channel_list"
            sign = _shopee_sign(path, ts, access_token, shop_id_int)
            r = requests.get(f"{_SHOPEE_BASE}{path}",
                params={"partner_id": SHOPEE_PARTNER_ID, "timestamp": ts,
                        "access_token": access_token, "shop_id": shop_id_int, "sign": sign},
                timeout=20)
            d = r.json()
            canais = (d.get("response", {}) or {}).get("logistics_channel_list", [])
            # só o essencial pra escolher
            resumo = [{"logistics_channel_id": c.get("logistics_channel_id"),
                       "name": c.get("logistics_channel_name"),
                       "enabled": c.get("enabled"),
                       "fee_type": c.get("fee_type"),
                       "weight_limit": c.get("weight_limit"),
                       "item_max_dimension": c.get("item_max_dimension"),
                       "size_list": c.get("size_list"),
                       "mask_channel_id": c.get("mask_channel_id")} for c in canais]
            return jsonify({"shop_id": sid, "total": len(resumo), "canais": resumo, "erro_api": d.get("message")})
        except Exception as e:
            return jsonify({"erro": str(e)}), 500

    @app.route("/integracoes/shopee/diag-atributos", methods=["GET"])
    def shopee_diag_atributos():
        """Diagnóstico: atributos obrigatórios de uma categoria."""
        cat = request.args.get("category_id", "")
        sid = request.args.get("shop_id", "")
        tokens = _shopee_load_tokens()
        if not sid:
            sid = list(tokens.keys())[0] if tokens else ""
        access_token, shop_id_int = _shopee_get_token(sid)
        if not access_token:
            return jsonify({"erro": "sem token"}), 400
        if not cat:
            cat = _shopee_categoria_recomendada(access_token, shop_id_int,
                       request.args.get("nome", "Farol Dianteiro Direito Jeep Compass 2022"))
        # tenta os dois endpoints (a Shopee mudou o nome em algumas versões)
        for path in ("/api/v2/product/get_attribute_tree", "/api/v2/product/get_attributes"):
            try:
                ts = int(time.time())
                sign = _shopee_sign(path, ts, access_token, shop_id_int)
                r = requests.get(f"{_SHOPEE_BASE}{path}",
                    params={"partner_id": SHOPEE_PARTNER_ID, "timestamp": ts,
                            "access_token": access_token, "shop_id": shop_id_int, "sign": sign,
                            "category_id": cat, "language": "pt-br"}, timeout=20)
                d = r.json()
                resp = d.get("response", {}) or {}
                attrs = resp.get("attribute_list") or resp.get("attribute_tree") or []
                if attrs:
                    resumo = [{"id": a.get("attribute_id"),
                               "nome": a.get("original_attribute_name") or a.get("display_attribute_name") or a.get("attribute_name"),
                               "obrigatorio": a.get("is_mandatory") or a.get("mandatory"),
                               "input_type": a.get("input_type") or a.get("input_validation_type"),
                               "n_valores": len(a.get("attribute_value_list") or [])} for a in attrs]
                    obrig = [a for a in resumo if a["obrigatorio"]]
                    return jsonify({"category_id": cat, "endpoint": path, "total": len(resumo),
                                    "obrigatorios": obrig, "todos": resumo})
                ultimo = {"endpoint": path, "erro_api": d.get("message"), "raw": str(d)[:300]}
            except Exception as e:
                ultimo = {"endpoint": path, "erro": str(e)}
        return jsonify({"category_id": cat, "falhou": True, "detalhe": ultimo})

    @app.route("/integracoes/shopee/diag-categoria", methods=["GET"])
    def shopee_diag_categoria():
        """Diagnóstico: resposta crua do category_recommend pra um nome de produto."""
        nome = request.args.get("nome", "Farol Dianteiro Direito Jeep Compass 2022")
        sid = request.args.get("shop_id", "")
        tokens = _shopee_load_tokens()
        if not sid:
            sid = list(tokens.keys())[0] if tokens else ""
        access_token, shop_id_int = _shopee_get_token(sid)
        if not access_token:
            return jsonify({"erro": "sem token", "shop": sid}), 400
        out = {"shop_id": sid, "nome": nome}
        try:
            ts = int(time.time())
            path = "/api/v2/product/category_recommend"
            sign = _shopee_sign(path, ts, access_token, shop_id_int)
            r = requests.get(f"{_SHOPEE_BASE}{path}",
                params={"partner_id": SHOPEE_PARTNER_ID, "timestamp": ts,
                        "access_token": access_token, "shop_id": shop_id_int,
                        "sign": sign, "item_name": nome[:120]}, timeout=15)
            out["status"] = r.status_code
            out["recommend_raw"] = r.json()
        except Exception as e:
            out["recommend_err"] = str(e)
        return jsonify(out)

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
        sku_interno = data.get("skuInterno") or sku  # SKU real da peca (quando o sku vem sufixado -SH1..-SH4 p/ multi-variacao)
        titulo = data.get("titulo", "")
        preco = data.get("preco", 0)
        descricao = data.get("descricao") or titulo  # Shopee exige descricao; se vazia, usa o titulo
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
            # Categoria-folha CORRETA: pergunta à Shopee pelo nome do produto
            cat_id = _shopee_categoria_recomendada(access_token, shop_id_int, titulo)
            if not cat_id:
                erros.append(f"shop {sid}: nao achou categoria para '{titulo[:30]}'")
                continue
            # Sobe as fotos pro media space (Shopee exige image_id, não aceita URL externa confiável)
            image_ids = _shopee_upload_imagens(access_token, shop_id_int, fotos)
            if not image_ids:
                erros.append(f"shop {sid}: nao conseguiu subir nenhuma foto")
                continue
            # Canais de envio habilitados (cada loja tem IDs próprios)
            canais = _shopee_canais_habilitados(access_token, shop_id_int)
            if not canais:
                erros.append(f"shop {sid}: nenhum canal de envio habilitado")
                continue
            logistics = [{"logistic_id": cid, "enabled": True, "is_free": False} for cid in canais]
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
                "original_price": float(preco),
                "seller_stock": [{"stock": 1}],
                "condition": condicao_shopee,
                "category_id": cat_id,
                "brand": {"brand_id": 0, "original_brand_name": "NoBrand"},  # autopeça usada: sem marca
                "image": {"image_id_list": image_ids},
                "logistic_info": logistics,
                # Atributo obrigatório da categoria de autopeça: "Auto-Part Number" (id 102293).
                # Usa o SKU como número da peça (referência real do vendedor).
                "attribute_list": [
                    {"attribute_id": 102293,
                     "attribute_value_list": [{"value_id": 0, "original_value_name": str(sku_interno)[:80]}]},
                ],
                "weight": float(data.get("peso") or 1.0),
                "dimension": {
                    "package_length": int(data.get("comprimento") or 30),
                    "package_width": int(data.get("largura") or 20),
                    "package_height": int(data.get("altura") or 15),
                },
            }
            # NCM + CEST (info fiscal) — Shopee exige os dois juntos em lojas que emitem nota fiscal
            _ncm = str(data.get("ncm") or "").strip()
            _cest = str(data.get("cest") or "").strip()
            if _ncm and _ncm not in ("00000000", "0000000", "0"):
                _ti = {"ncm": _ncm}
                if _cest and _cest not in ("0000000", "00000000", "0"):
                    _ti["cest"] = _cest
                payload_shopee["tax_info"] = _ti
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
                    print(f"[SHOPEE] Erro shop {sid} SKU '{sku}': {msg} | RAW={_d}")
                    detalhe = ""
                    if data.get("debug"):
                        detalhe = f" | RAW={_d} | LOG={logistics}"
                    erros.append(f"shop {sid}: {msg}{detalhe}")
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

    @app.route("/integracoes/shopee/deletar-item", methods=["POST", "OPTIONS"])
    def shopee_deletar_item():
        if request.method == "OPTIONS":
            return _options_resp()
        data = request.get_json(force=True) or {}
        item_id = data.get("item_id")
        sid = str(data.get("shop_id") or "")
        if not item_id:
            return jsonify({"ok": False, "erro": "item_id obrigatorio"}), 400
        tokens = _shopee_load_tokens()
        if not sid:
            sid = list(tokens.keys())[0] if tokens else ""
        access_token, shop_id_int = _shopee_get_token(sid)
        if not access_token:
            return jsonify({"ok": False, "erro": "sem token"}), 401
        try:
            ts = int(time.time())
            path = "/api/v2/product/delete_item"
            sign = _shopee_sign(path, ts, access_token, shop_id_int)
            r = requests.post(f"{_SHOPEE_BASE}{path}",
                params={"partner_id": SHOPEE_PARTNER_ID, "timestamp": ts,
                        "access_token": access_token, "shop_id": shop_id_int, "sign": sign},
                json={"item_id": int(item_id)}, timeout=20)
            d = r.json()
            return jsonify({"ok": not d.get("error"), "item_id": item_id, "resposta": d})
        except Exception as e:
            return jsonify({"ok": False, "erro": str(e)}), 500

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

    def _shopee_get_models(access_token, shop_id_int, item_id):
        """Variações de um item (model_sku + estoque por variação)."""
        ts = int(time.time())
        path = "/api/v2/product/get_model_list"
        sign = _shopee_sign(path, ts, access_token, shop_id_int)
        try:
            r = requests.get(
                f"{_SHOPEE_BASE}{path}",
                params={"partner_id": SHOPEE_PARTNER_ID, "timestamp": ts,
                        "access_token": access_token, "shop_id": shop_id_int,
                        "sign": sign, "item_id": item_id},
                timeout=20
            )
            d = r.json()
            if r.status_code == 200 and not d.get("error"):
                return d.get("response", {}).get("model", []) or []
        except Exception:
            pass
        return []

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
                for it in d.get("response", {}).get("item_list", []):
                    # Item com variação: SKU e estoque ficam nos models → busca get_model_list
                    if it.get("has_model"):
                        it["_models"] = _shopee_get_models(access_token, shop_id_int, it.get("item_id"))
                    results.append(it)
        return results

    def _shopee_extract_sku(item):
        # Shopee usa item_sku no nivel do item; seller_sku como fallback
        sku = (item.get("item_sku") or item.get("seller_sku") or "").strip()
        if sku:
            return sku
        for m in (item.get("_models") or []):
            ms = (m.get("model_sku") or "").strip()
            if ms:
                return ms
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
        models = item.get("_models") or []
        if models:
            total = 0
            for m in models:
                ss = (m.get("stock_info_v2", {}) or {}).get("seller_stock", [{}])
                total += int(ss[0].get("stock", 0)) if ss else 0
            return total
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
                # on_conflict=shop_id,item_id: mesmo fix do ML — sem ele o upsert
                # bate na unique (shop_id,item_id) e dá 409, abortando o lote.
                r_up = requests.post(
                    f"{_WRX_SB_URL}/rest/v1/shopee_anuncios?on_conflict=shop_id,item_id",
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
        debug = request.args.get("debug") == "1"
        shop_ids = [str(shop_id_param)] if shop_id_param else list(tokens.keys())
        todas = []
        diag = []
        for sid in shop_ids:
            access_token, shop_id_int = _shopee_get_token(sid)
            if not access_token:
                continue
            now = int(time.time())
            # 1) Coleta os order_sn em janelas de <=15 dias (limite da Shopee), com paginacao
            sns = []
            restante = max(1, dias)
            win_to = now
            while restante > 0:
                win_dias = min(15, restante)
                win_from = win_to - win_dias * 86400
                cursor = ""
                for _ in range(20):  # ate 20 paginas por janela
                    ts = int(time.time())
                    path = "/api/v2/order/get_order_list"
                    sign = _shopee_sign(path, ts, access_token, shop_id_int)
                    r = requests.get(
                        f"{_SHOPEE_BASE}{path}",
                        params={"partner_id": SHOPEE_PARTNER_ID, "timestamp": ts,
                                "access_token": access_token, "shop_id": shop_id_int, "sign": sign,
                                "time_range_field": "create_time", "time_from": win_from,
                                "time_to": win_to, "page_size": 100, "cursor": cursor},
                        timeout=20
                    )
                    try:
                        d = r.json()
                    except Exception:
                        d = {}
                    if r.status_code != 200 or d.get("error"):
                        diag.append({"shop": sid, "fase": "list", "error": d.get("error", ""), "message": d.get("message", "")})
                        break
                    resp = d.get("response", {}) or {}
                    for o in resp.get("order_list", []):
                        if o.get("order_sn"):
                            sns.append(o["order_sn"])
                    if resp.get("more") and resp.get("next_cursor"):
                        cursor = resp["next_cursor"]
                    else:
                        break
                win_to = win_from
                restante -= win_dias
            # 2) Busca os detalhes (status/total/itens) em lotes de 50
            for i in range(0, len(sns), 50):
                lote = sns[i:i + 50]
                ts = int(time.time())
                path = "/api/v2/order/get_order_detail"
                sign = _shopee_sign(path, ts, access_token, shop_id_int)
                r = requests.get(
                    f"{_SHOPEE_BASE}{path}",
                    params={"partner_id": SHOPEE_PARTNER_ID, "timestamp": ts,
                            "access_token": access_token, "shop_id": shop_id_int, "sign": sign,
                            "order_sn_list": ",".join(lote),
                            "response_optional_fields": "order_status,total_amount,create_time,buyer_username,item_list"},
                    timeout=20
                )
                try:
                    d = r.json()
                except Exception:
                    d = {}
                if r.status_code != 200 or d.get("error"):
                    diag.append({"shop": sid, "fase": "detail", "error": d.get("error", ""), "message": d.get("message", "")})
                    continue
                for o in d.get("response", {}).get("order_list", []):
                    todas.append({
                        "marketplace": "shopee",
                        "shop_id": sid,
                        "order_sn": o.get("order_sn", ""),
                        "status": o.get("order_status", ""),
                        "criar_em": o.get("create_time", 0),
                        "total": o.get("total_amount", 0),
                        "comprador": o.get("buyer_username", ""),
                        "itens": o.get("item_list", []),
                    })
        todas.sort(key=lambda x: x.get("criar_em", 0), reverse=True)
        out = {"ok": True, "vendas": todas, "total": len(todas)}
        if debug:
            out["diag"] = diag
        return jsonify(out)

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
        debug = request.args.get("debug") == "1"
        todas = []
        diag = []
        for sid in shop_ids:
            access_token, shop_id_int = _shopee_get_token(sid)
            if not access_token:
                diag.append({"shop": sid, "erro": "sem access_token"})
                continue
            ts = int(time.time())
            path = "/api/v2/sellerchat/get_conversation_list"
            sign = _shopee_sign(path, ts, access_token, shop_id_int)
            r = requests.get(
                f"{_SHOPEE_BASE}{path}",
                params={"partner_id": SHOPEE_PARTNER_ID, "timestamp": ts,
                        "access_token": access_token, "shop_id": shop_id_int,
                        "sign": sign, "direction": "latest", "type": "all", "page_size": 25},
                timeout=20
            )
            try:
                d = r.json()
            except Exception:
                d = {}
            convs = (d.get("response", {}) or {}).get("conversations", []) or []
            diag.append({"shop": sid, "http": r.status_code, "error": d.get("error", ""),
                         "message": d.get("message", ""), "n_conversas": len(convs)})
            if r.status_code == 200 and not d.get("error"):
                for c in convs:
                    lm = c.get("latest_message_content", {}) or c.get("last_message", {}) or {}
                    txt = lm.get("text", "") if isinstance(lm, dict) else ""
                    todas.append({
                        "marketplace": "shopee",
                        "shop_id": sid,
                        "conversation_id": c.get("conversation_id", ""),
                        "comprador": c.get("to_name", "") or c.get("to_id", ""),
                        "ultima_msg": txt,
                        "nao_lidas": c.get("unread_count", 0),
                        "timestamp": c.get("last_message_timestamp", 0) or c.get("latest_message_time", 0),
                    })
        todas.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
        resp = {"ok": True, "mensagens": todas, "total": len(todas)}
        if debug:
            resp["diag"] = diag
        return jsonify(resp)

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
                # Normaliza SKU: variacoes vem sufixadas (-SH1..-SH4); o estoque usa o SKU base
                sku = re.sub(r'-(SH|DML|GML)\d+$', '', (item.get("model_sku") or "").strip(), flags=re.I)
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
        # Normaliza SKU: anuncios multi-variacao vem sufixados (-SH1..-SH4 / -DML#/-GML#); o estoque usa o SKU base
        sku = re.sub(r'-(SH|DML|GML)\d+$', '', (data.get("sku") or "").strip(), flags=re.I)
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

    # ── BLOCO B: sincronização multi-canal (módulo separado sync_multicanal.py) ───
    # Vendeu num canal -> pausa o MESMO SKU em todos os outros (ML + Shopee).
    # A lógica vive em sync_multicanal.py; aqui só injetamos os tokens e registramos.
    try:
        import sync_multicanal as _syncmc
        _syncmc.init_sync(
            ml_token_provider=_ml_get_user_token,
            shopee_token_provider=_shopee_get_token,
            shopee_partner_id=SHOPEE_PARTNER_ID,
            shopee_partner_key=SHOPEE_PARTNER_KEY,
            shopee_base=_SHOPEE_BASE,
        )
        app.register_blueprint(_syncmc.get_blueprint())
        print("[STARTUP] sync_multicanal registrado (/integracoes/sincronizar-venda)")
    except Exception as _e_sync:
        print(f"[STARTUP] sync_multicanal NAO registrado: {_e_sync}")

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

    def _cron_whatsapp_loop():
        """Thread: a cada 2 min chama checar-novidades (avisa pergunta/venda/reclamação no WhatsApp)."""
        import threading
        def _loop():
            time.sleep(40)  # espera o servidor subir
            while True:
                try:
                    requests.post(f"http://127.0.0.1:{PORT}/integracoes/whatsapp/checar-novidades", timeout=90)
                except Exception as _e:
                    print(f"[CRON-WHATSAPP] erro: {_e}")
                try:
                    requests.post(f"http://127.0.0.1:{PORT}/integracoes/whatsapp/pedidos-manha", timeout=60)
                except Exception as _e:
                    print(f"[CRON-MANHA] erro: {_e}")
                time.sleep(120)  # 2 minutos
        t = threading.Thread(target=_loop, daemon=True)
        t.start()
        print("[STARTUP] cron WhatsApp ativo (checa novidades a cada 2 min)")

    def _cron_shopee_etiquetas_loop():
        """Thread: a cada 15 min pré-gera etiquetas Shopee (ship_order + create_shipping_document),
        pra a etiqueta já estar pronta na hora da conferência (a Shopee demora pra liberar)."""
        import threading
        def _loop():
            time.sleep(60)  # espera o servidor subir
            while True:
                try:
                    requests.get(f"http://127.0.0.1:{PORT}/integracoes/shopee-pregerar-etiquetas", timeout=300)
                except Exception as _e:
                    print(f"[CRON-SHOPEE-ETQ] erro: {_e}")
                time.sleep(900)  # 15 minutos
        t = threading.Thread(target=_loop, daemon=True)
        t.start()
        print("[STARTUP] cron Shopee etiquetas ativo (pre-gera a cada 15 min)")

    def main():
        sys.stdout.reconfigure(encoding="utf-8", errors="replace") if hasattr(sys.stdout, "reconfigure") else None
        print(f"WRX-Search API Server - porta {PORT}")
        print(f"  http://localhost:{PORT}/buscar?q=CODIGO")
        print(f"  http://localhost:{PORT}/carros?q=fiat+uno")
        print(f"  http://localhost:{PORT}/ping")
        host = "0.0.0.0" if _IS_RAILWAY else "127.0.0.1"
        _cron_whatsapp_loop()
        _cron_shopee_etiquetas_loop()
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
