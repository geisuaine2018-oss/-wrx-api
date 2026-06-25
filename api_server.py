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

def _ml_achatar_branco(raw):
    """Achata a transparência sobre branco MANTENDO o tamanho (sem padding —
    o ML apara bordas brancas, então padding seria inútil). JPEG bytes ou None."""
    try:
        from PIL import Image
        import io as _io
        im = Image.open(_io.BytesIO(raw)).convert("RGBA")
        fundo = Image.new("RGBA", im.size, (255, 255, 255, 255))
        fundo.paste(im, (0, 0), im)
        buf = _io.BytesIO()
        fundo.convert("RGB").save(buf, format="JPEG", quality=92)
        return buf.getvalue()
    except Exception:
        return None

def _ml_foto_para_pic(token, conta_nome, foto):
    """URL http -> {'source': url} (o ML busca direto; mesmo resultado).
    Foto editada (base64) -> achata sobre branco e sobe como arquivo (não tem URL).
    Cacheia o upload da base64 por (conta, foto)."""
    import hashlib as _hl
    try:
        if foto.startswith("http"):
            return {"source": foto}
        if not foto.startswith("data:image"):
            return None
        import base64 as _b64
        raw = _b64.b64decode(foto.split(",", 1)[1])
        ckey = (conta_nome, "b64:" + _hl.md5(raw).hexdigest())
        if ckey in _ML_PIC_CACHE:
            return {"id": _ML_PIC_CACHE[ckey]}
        flat = _ml_achatar_branco(raw) or raw
        pid = _ml_upload_bytes(token, flat, "image/jpeg", "jpg")
        if pid:
            _ML_PIC_CACHE[ckey] = pid
            return {"id": pid}
        return None
    except Exception:
        return None

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

def _parse_itens_ml(html):
    """Extrai POR ANÚNCIO: {titulo, preco}. Diferente de _parse_html_ml (que separa em
    listas), aqui mantém título+preço JUNTOS para dar pra filtrar por título
    (concessionária, paralela, relevância) na raspagem da revisão de preços.
    NÃO separa por condição — desmonte costuma anunciar usado na categoria 'novo'."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    cards = (soup.select("div.poly-card") or
             soup.select("li.ui-search-layout__item") or
             soup.select("div.ui-search-result"))
    itens = []
    for c in cards:
        t = (c.select_one(".poly-component__title") or
             c.select_one(".ui-search-item__title") or
             c.find(class_=re.compile(r"poly-component__title|item__title")))
        frac = (c.select_one(".andes-money-amount__fraction") or
                c.find(class_="price-tag-fraction"))
        if not (t and frac):
            continue
        titulo = t.get_text(strip=True)
        try:
            cent = c.select_one(".andes-money-amount__cents")
            inteiro = float(frac.get_text(strip=True).replace(".", "").replace(",", ""))
            decimal = float(cent.get_text(strip=True).replace(",", ".")) / 100 if cent else 0
            preco = round(inteiro + decimal, 2)
        except Exception:
            continue
        if titulo and 5 < preco < 100000:
            itens.append({"titulo": titulo, "preco": preco})
    return itens

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

# ─── Coletor de preços LIMPO para a Revisão de Preços ──────────────────────────
# Filtra a concorrência pra comparar com o PRODUTO CERTO (não acessório/relacionado),
# tira vendedor com muitos anúncios iguais (atacado/revenda) e produto paralelo/danificado.
# NÃO mexe no executar_busca (motor do cadastro) — é uma coleta separada.
_REV_ACESSORIOS = [
    "lampada", "lâmpada", "led", "xenon", "soquete", "lente", "capa", "moldura",
    "friso", "sensor", "reparo", "conector", "chicote", "parafuso", "presilha",
    "palheta", "guarnicao", "guarnição", "vigia", "defletor", "aplique", "adesivo",
    "emblema", "protetor", "pelicula", "película", "cobertura",
]
_REV_RUINS = [
    "paralel", "similar", "recondicion", "remanufatur", "retific", "danificad",
    "avariad", "batid", "quebrad", "trincad", "p/ retirada", "para retirada",
    "para conserto", "no estado", "sem garantia de funcionamento", "com defeito",
    "concessionaria", "concessionária", "genuino", "genuíno",
]

def _revisao_tokens(txt):
    import re as _re
    txt = (txt or "").lower()
    txt = _re.sub(r"[^a-z0-9çãõáéíóúâêôà ]", " ", txt)
    stop = {"do", "da", "de", "para", "com", "sem", "e", "o", "a", "os", "as",
            "p", "par", "kit", "novo", "nova", "usado", "usada", "original", "peca", "peça"}
    return [w for w in txt.split() if len(w) >= 3 and w not in stop]

def _revisao_coletar_precos(consulta, eh_oem=False):
    """Coleta preços da concorrência no Mercado Livre.
    A API do ML está BLOQUEADA (403), então RASPA pelo navegador local (scraper.js) —
    funciona no PC. Varre VÁRIAS páginas (mais quantidade = mais assertivo) e aplica os
    filtros: relevância, acessório, PARALELA/CONCESSIONÁRIA (via _REV_RUINS) e VENDEDOR
    com +2 anúncios (atacado — detectado por anúncio repetido, já que a lista não traz o
    vendedor). NÃO filtra por condição (novo/usado): desmonte anuncia usado como 'novo'."""
    import re as _re
    slug = _re.sub(r"\s+", "-", (consulta or "").strip().lower())
    slug = _re.sub(r"[^a-z0-9\-]", "", slug).strip("-")
    if not slug:
        return []
    # ML pagina de ~48 em 48 (_Desde_49, _Desde_97...). Mais páginas = mais base de preço.
    urls = [f"https://lista.mercadolivre.com.br/{slug}" +
            ("" if d == 1 else f"_Desde_{d}") for d in (1, 49, 97, 145, 193)]
    try:
        htmls = _buscar_navegador(urls)        # navegador local (Edge) — funciona no PC
    except Exception:
        htmls = []
    if not htmls:
        try:
            htmls = _buscar_requests_html(urls)
        except Exception:
            htmls = []
    toks = _revisao_tokens(consulta)
    principal = toks[0] if toks else ""
    qlow = (consulta or "").lower()
    repetidos = {}   # mesmo título+preço repetido = vendedor de atacado → limita a 2
    precos = []
    for h in (htmls or []):
        for it in _parse_itens_ml(h):
            titulo = it.get("titulo") or ""
            tl = titulo.lower()
            preco = float(it.get("preco") or 0)
            if preco <= 5:
                continue
            # 1) RELEVÂNCIA: título precisa ter a palavra principal do produto.
            if not eh_oem and principal and principal not in tl:
                continue
            # 2) tira ACESSÓRIO/peça relacionada (só se não faz parte da consulta)
            if not eh_oem and any(a in tl and a not in qlow for a in _REV_ACESSORIOS):
                continue
            # 3) tira PARALELA / recondicionado / danificado / CONCESSIONÁRIA
            if any(b in tl for b in _REV_RUINS):
                continue
            # 4) VENDEDOR com +2 anúncios (atacado): a lista não traz o vendedor, então
            #    usamos o anúncio repetido (mesmo título+preço) como proxy — limita a 2.
            chave = (tl[:45], round(preco))
            repetidos[chave] = repetidos.get(chave, 0) + 1
            if repetidos[chave] > 2:
                continue
            precos.append(round(preco, 2))
    return precos

def _revisao_filtrar_pares(pares, consulta, eh_oem=False):
    """Aplica os MESMOS filtros (relevância, acessório, paralela, vendedor>2) sobre
    pares (titulo, preco, vendedor) vindos de FORA — ex: raspados pelo navegador.
    Retorna a lista de preços limpos (ainda sem aparar outliers)."""
    toks = _revisao_tokens(consulta)
    principal = toks[0] if toks else ""
    qlow = (consulta or "").lower()
    por_vendedor = {}
    precos = []
    for par in (pares or []):
        titulo = (par.get("titulo") or "")
        tl = titulo.lower()
        try:
            preco = float(par.get("preco") or 0)
        except Exception:
            continue
        if preco <= 5:
            continue
        vend = (par.get("vendedor") or "").strip().lower()
        if titulo:
            # RELEVÂNCIA pela PALAVRA-CABEÇA: o anúncio tem que COMEÇAR com a mesma
            # palavra do produto. Ex: "porta" não casa com "Maçaneta Porta...",
            # "farol" não casa com "Lâmpada Do Farol...". Bem mais preciso que "contém".
            res_head = (_revisao_tokens(titulo) or [""])[0]
            if not eh_oem and principal and res_head != principal:
                continue
            if not eh_oem and any(a in tl and a not in qlow for a in _REV_ACESSORIOS):
                continue
            if any(b in tl for b in _REV_RUINS):
                continue
        if vend:
            por_vendedor[vend] = por_vendedor.get(vend, 0) + 1
            if por_vendedor[vend] > 2:
                continue
        precos.append(round(preco, 2))
    return precos

def _revisao_aparar(precos):
    """Corta outliers ANTES de calcular menor/media/sugestao.
    Ex: miolo 450/500/600 -> nao deixa entrar o 250 (muito barato) nem o 1000 (muito caro).
    Usa a MEDIANA (robusta) como referencia: mantem so 0.6x ate 1.7x dela; depois apara 10% das pontas."""
    s = sorted(float(p) for p in precos if p and float(p) > 0)
    if len(s) < 4:
        return s  # poucos dados — nao da pra julgar outlier
    mediana = s[len(s) // 2]
    faixa = [p for p in s if mediana * 0.6 <= p <= mediana * 1.7]
    if len(faixa) < 3:
        faixa = s  # cortou demais — devolve tudo
    n = len(faixa)
    corte = int(n * 0.10)
    if corte:
        faixa = faixa[corte: n - corte] or faixa
    return faixa

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

    @app.route("/estoque/excluir-produto", methods=["POST", "OPTIONS"])
    def estoque_excluir_produto():
        if request.method == "OPTIONS":
            return _options_resp()
        data = request.get_json(force=True) or {}
        sku = str(data.get("sku") or "").strip()
        if not sku:
            return jsonify({"ok": False, "erro": "sku obrigatorio"}), 400
        headers = _wrx_headers()
        try:
            antes = requests.get(
                f"{_WRX_SB_URL}/rest/v1/pecas_estoque",
                params={"select": "sku,qtd", "sku": f"eq.{sku}", "limit": 1},
                headers=headers,
                timeout=15,
            )
            if antes.status_code != 200:
                return jsonify({
                    "ok": False,
                    "erro": f"Falha ao localizar produto ({antes.status_code}): {antes.text[:200]}",
                }), 502
            if not antes.json():
                return jsonify({"ok": False, "erro": f"SKU {sku} nao encontrado no estoque"}), 404
            r = requests.patch(
                f"{_WRX_SB_URL}/rest/v1/pecas_estoque",
                params={"sku": f"eq.{sku}"},
                headers={**headers, "Prefer": "return=representation"},
                json={"qtd": 0, "atualizado": _datetime.utcnow().isoformat() + "Z"},
                timeout=15,
            )
            if r.status_code not in (200, 204):
                return jsonify({
                    "ok": False,
                    "erro": f"Banco recusou a exclusao ({r.status_code}): {r.text[:200]}",
                }), 502
            removidos = r.json() if r.status_code == 200 and r.text.strip() else []
            depois = requests.get(
                f"{_WRX_SB_URL}/rest/v1/pecas_estoque",
                params={"select": "sku,qtd", "sku": f"eq.{sku}", "limit": 1},
                headers=headers,
                timeout=15,
            )
            linha_depois = depois.json()[0] if depois.status_code == 200 and depois.json() else None
            if not linha_depois or int(linha_depois.get("qtd") or 0) != 0:
                return jsonify({
                    "ok": False,
                    "erro": "O banco nao confirmou a exclusao do produto",
                }), 502
            return jsonify({
                "ok": True,
                "sku": sku,
                "removidos": 1,
                "modo": "desativado",
            })
        except Exception as e:
            return jsonify({"ok": False, "erro": str(e)}), 500

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

    @app.route("/medir-peca", methods=["POST", "OPTIONS"])
    def rota_medir_peca():
        # Mede a peca sobre o tapete ChArUco: escala pelos marcadores ArUco 5x5 + segmenta a peca (HSV) + bbox -> cm.
        if request.method == "OPTIONS":
            return Response(status=204, headers={"Access-Control-Allow-Origin":"*","Access-Control-Allow-Methods":"POST,OPTIONS","Access-Control-Allow-Headers":"Content-Type"})
        import base64
        try:
            import numpy as np, cv2
            if not hasattr(cv2, "aruco"):
                return jsonify({"ok": False, "erro": "Servidor sem o modulo ArUco (atualize o opencv-contrib)."}), 200
            data = request.get_json(force=True)
            img_b64 = data.get("imagem", "")
            marcador_cm = float(data.get("marcador_cm", 7.5))
            if not img_b64:
                return jsonify({"ok": False, "erro": "Campo 'imagem' obrigatorio"}), 400
            if img_b64.startswith("data:"):
                img_b64 = img_b64.split(",", 1)[1]
            arr = np.frombuffer(base64.b64decode(img_b64), np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is None:
                return jsonify({"ok": False, "erro": "imagem invalida"}), 400
            H, W = img.shape[:2]
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            # 1) detecta marcadores ArUco 5x5 do tapete
            dic = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_5X5_250)
            det = cv2.aruco.ArucoDetector(dic, cv2.aruco.DetectorParameters())
            corners, ids, _ = det.detectMarkers(gray)
            if ids is None or len(ids) == 0:
                return jsonify({"ok": False, "erro": "Nenhum marcador do tapete detectado. Tire a foto de cima, reta, com o tapete bem visivel e boa luz."}), 200
            square_cm = float(data.get("square_cm", 11.0))
            sq_x = int(data.get("squares_x", 10)); sq_y = int(data.get("squares_y", 20))
            # 1b) HOMOGRAFIA ChArUco: corrige a perspectiva (foto inclinada) e calibra a escala
            #     exatamente pelo marcador (7.5cm conhecido). Igual ao tapete do PartsHub.
            Hm = None; fator = 1.0; usou_homografia = False
            try:
                board = cv2.aruco.CharucoBoard((sq_x, sq_y), square_cm, marcador_cm, dic)
                ids_b = board.getIds().flatten()
                objpts = board.getObjPoints()
                obj_por_id = {int(m): objpts[ki][:, :2] for ki, m in enumerate(ids_b)}
                ip = []; op = []
                for c, mid in zip(corners, ids.flatten()):
                    if int(mid) in obj_por_id:
                        ip.extend(c[0]); op.extend(obj_por_id[int(mid)])
                if len(ip) >= 8:  # pelo menos 2 marcadores (8 cantos)
                    Hm, _msk = cv2.findHomography(np.array(ip, np.float32), np.array(op, np.float32), cv2.RANSAC, 3.0)
                    if Hm is not None:
                        lc = []
                        for c in corners:
                            pc = cv2.perspectiveTransform(c.reshape(-1, 1, 2).astype(np.float32), Hm).reshape(-1, 2)
                            for j in range(4):
                                lc.append(float(np.linalg.norm(pc[j] - pc[(j + 1) % 4])))
                        med = float(np.median(lc)) if lc else marcador_cm
                        fator = (marcador_cm / med) if med else 1.0
                        usou_homografia = True
            except Exception:
                Hm = None; usou_homografia = False
            # escala simples (fallback) — mediana e mais robusta que media
            lados = []
            for c in corners:
                p = c[0]
                for i in range(4):
                    lados.append(float(np.linalg.norm(p[i] - p[(i+1) % 4])))
            lado_px = float(np.median(lados))
            cm_por_px = marcador_cm / lado_px
            # 2) ISOLA a peca — primeiro pelo REMOVEDOR DE FUNDO (Pixian/rembg, mesmo do
            #    editor e do PartsHub): muito mais preciso que segmentar por cor. Fallback: HSV.
            peca = None
            try:
                _rf = requests.post("http://127.0.0.1:%s/remover-fundo" % PORT,
                                    json={"imagem": img_b64}, timeout=90)
                _png = _rf.json().get("png") if _rf.status_code == 200 else None
                if _png:
                    _rgba = cv2.imdecode(np.frombuffer(base64.b64decode(_png), np.uint8), cv2.IMREAD_UNCHANGED)
                    if _rgba is not None and _rgba.ndim == 3 and _rgba.shape[2] == 4:
                        _a = _rgba[:, :, 3]
                        if _a.shape[:2] != (H, W):
                            _a = cv2.resize(_a, (W, H))
                        peca = (_a > 128).astype(np.uint8) * 255
            except Exception:
                peca = None
            if peca is None or int((peca > 0).sum()) < (W * H * 0.002):
                hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
                verde  = cv2.inRange(hsv, (30, 40, 40), (90, 255, 255))
                branco = cv2.inRange(hsv, (0, 0, 175), (180, 55, 255))
                preto  = cv2.inRange(hsv, (0, 0, 0), (180, 255, 65))
                tapete = cv2.bitwise_or(cv2.bitwise_or(verde, branco), preto)
                peca = cv2.bitwise_not(tapete)
            k = np.ones((7, 7), np.uint8)
            peca = cv2.morphologyEx(peca, cv2.MORPH_OPEN, k, iterations=2)
            peca = cv2.morphologyEx(peca, cv2.MORPH_CLOSE, k, iterations=3)
            n, lab, stats, cent = cv2.connectedComponentsWithStats(peca, 8)
            best_i, best_score = -1, 0.0
            cx0, cy0 = W / 2.0, H / 2.0
            for i in range(1, n):
                area = stats[i, cv2.CC_STAT_AREA]
                if area < W * H * 0.005:
                    continue
                px, py = cent[i]
                dist = ((px - cx0) ** 2 + (py - cy0) ** 2) ** 0.5
                score = area / (1 + dist * 0.5)
                if score > best_score:
                    best_score, best_i = score, i
            if best_i < 0:
                return jsonify({"ok": False, "erro": "Nao consegui isolar a peca do tapete."}), 200
            # 4) mede na ORIENTACAO da peca (minAreaRect) — nao infla quando a peca esta girada.
            mask_p = (lab == best_i).astype(np.uint8)
            cnts_p, _hc = cv2.findContours(mask_p, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            cnt_p = max(cnts_p, key=cv2.contourArea)
            if usou_homografia and Hm is not None:
                cmp = cv2.perspectiveTransform(cnt_p.reshape(-1, 1, 2).astype(np.float32), Hm).reshape(-1, 2).astype(np.float32) * fator
                (_cc, (rw, rh), _ang) = cv2.minAreaRect(cmp)
                larg = round(float(max(rw, rh)), 1); alt = round(float(min(rw, rh)), 1)
            else:
                (_cc, (rw, rh), _ang) = cv2.minAreaRect(cnt_p.astype(np.float32))
                larg = round(float(max(rw, rh)) * cm_por_px, 1); alt = round(float(min(rw, rh)) * cm_por_px, 1)
            # largura sempre o maior lado (padrao de cadastro)
            L, A = (larg, alt) if larg >= alt else (alt, larg)
            return jsonify({"ok": True, "largura": L, "altura": A, "marcadores": int(len(ids)),
                            "metodo": ("perspectiva" if usou_homografia else "simples")})
        except Exception as e:
            return jsonify({"ok": False, "erro": str(e)[:200]}), 200

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
            # 0. Pixian.ai (API paga — melhor qualidade). Credenciais em env var no Railway
            #    (PIXIAN_API_ID / PIXIAN_API_SECRET). Se OK, devolve já; senão cai no fallback abaixo.
            try:
                def _px_env(_n):
                    # le a variavel tolerando espaco/tab invisivel no NOME (acontece ao colar no Railway)
                    _v = os.environ.get(_n)
                    if _v is None:
                        for _k, _vv in os.environ.items():
                            if _k.strip() == _n:
                                _v = _vv; break
                    return (_v or "").strip()
                _px_id = _px_env("PIXIAN_API_ID")
                _px_secret = _px_env("PIXIAN_API_SECRET")
                if _px_id and _px_secret:
                    import requests as _rq
                    _px_data = {"image.base64": img_b64, "output.format": "png"}
                    if _px_env("PIXIAN_TEST").lower() in ("1", "true", "sim"):
                        _px_data["test"] = "true"  # modo teste: nao gasta credito (qualidade reduzida)
                    _px_resp = _rq.post(
                        "https://api.pixian.ai/api/v2/remove-background",
                        auth=(_px_id, _px_secret), data=_px_data, timeout=60,
                    )
                    if _px_resp.status_code == 200 and _px_resp.content:
                        return jsonify({"png": base64.b64encode(_px_resp.content).decode()})
                    else:
                        print(f"[PIXIAN] HTTP {_px_resp.status_code}: {_px_resp.text[:300]}")
                else:
                    print(f"[PIXIAN] credenciais ausentes; chaves PIXIAN no env: {[repr(_k) for _k in os.environ if 'PIXIAN' in _k.upper()]}")
            except Exception as _e_px:
                print(f"[PIXIAN] excecao: {_e_px}")
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

    @app.route("/gerar-titulos", methods=["POST", "OPTIONS"])
    def rota_gerar_titulos():
        # Gera titulos com a GEMINI_API_KEY do servidor (frontend nao precisa de chave local)
        if request.method == "OPTIONS":
            return Response(status=204, headers={"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "POST,OPTIONS", "Access-Control-Allow-Headers": "Content-Type"})
        try:
            data = request.get_json(force=True)
            nome = (data.get("nome") or "").strip()
            if not nome:
                return jsonify({"erro": "nome obrigatorio"}), 400
            key = os.environ.get("GEMINI_API_KEY", "").strip()
            if not key:
                return jsonify({"erro": "GEMINI_API_KEY ausente no servidor"}), 500
            # Dados estruturados da peca (frontend manda; se nao mandar, usa so o nome)
            marca = (data.get("marca") or "").strip()
            modelo = (data.get("modelo") or "").strip()
            motor = (data.get("motor") or "").strip()
            eixo = (data.get("posicaoEixo") or data.get("eixo") or "").strip()
            lado = (data.get("posicaoLado") or data.get("lado") or "").strip()
            oem = (data.get("oem") or data.get("codigo") or "").strip()
            descricao = (data.get("descricao") or "").strip()
            carros = data.get("carros") or data.get("compat") or []
            # normaliza lista de carros -> "Veiculo Ano"
            linhas_carros = []
            try:
                for c in carros[:12]:
                    if isinstance(c, dict):
                        v = (c.get("veiculo") or c.get("nome") or "").strip()
                        a = (str(c.get("anos") or c.get("ano") or "")).strip()
                        if v:
                            linhas_carros.append((v + " " + a).strip())
                    elif isinstance(c, str) and c.strip():
                        linhas_carros.append(c.strip())
            except Exception:
                pass
            ctx = ["Peca: " + nome]
            if marca or modelo:
                ctx.append("Marca/Modelo: " + (marca + " " + modelo).strip())
            if motor:
                ctx.append("Motorizacao: " + motor)
            if eixo or lado:
                ctx.append("Posicao/Lado: " + (eixo + " " + lado).strip())
            if linhas_carros:
                ctx.append("Veiculos compativeis (USE TODOS estes - marca, modelo e ano; NAO invente outros): " + "; ".join(linhas_carros))
            if descricao:
                ctx.append("Descricao (use como apoio): " + descricao[:600])
            prompt = (
                "Voce e especialista em titulos de anuncio de autopecas (Mercado Livre e Shopee). "
                "Monte 5 titulos para a peca abaixo, do MAIS COMPLETO ao mais curto.\n\n"
                "DADOS:\n- " + "\n- ".join(ctx) + "\n\n"
                "REGRAS OBRIGATORIAS:\n"
                "1. O PRINCIPAL e MARCA + MODELO(S) + ANO (NAO o codigo). O titulo gira em torno disso.\n"
                "2. ORDEM: Produto (tipo da peca) + Marca + Modelo(s) + [Motor, se mecanica/eletrica | Lado, se lataria/farol/lanterna/acabamento] + ANOS. Comece pelo tipo da peca.\n"
                "3. Liste TODAS as MARCAS e TODOS os MODELOS compativeis (ex: Fiat Argo Cronos Pulse). Se ha mais de uma marca, inclua todas. NAO invente, NAO omita.\n"
                "4. SEMPRE inclua os ANOS dos compativeis (faixa, ex '2016 a 2022' ou '2016 2022'). Use SOMENTE anos reais dos veiculos compativeis. NUNCA invente. Se faltam anos e sobra espaco, ADICIONE os anos.\n"
                "5. NAO coloque codigo, OEM nem SKU no titulo (nem numeros tipo 109053). O codigo NAO e o principal. Se a peca nao tem OEM real, pode usar a palavra 'Original'.\n"
                "6. APROVEITE O ESPACO: titulo o MAIS COMPLETO possivel dentro do limite. Se sobrar espaco, inclua mais modelos, os anos por extenso, a palavra 'Original'. Prefira SEMPRE completo a curto. NAO repita nem invente.\n"
                "7. Cada titulo com ATE 60 caracteres (limite Mercado Livre) e use o MAXIMO desse limite. O 1o titulo entre 52 e 60 caracteres. Sem aspas, sem numeracao no inicio.\n"
                'Responda SOMENTE em JSON: {"titulos":["t1","t2","t3","t4","t5"]}'
            )
            data_ia = _gemini(key, prompt)  # funcao testada (usa thinkingBudget=0)
            titulos = (data_ia or {}).get("titulos") or []
            if not titulos:
                _diag = {"erro": "IA nao retornou titulos"}
                try:
                    import requests as _rq
                    _u = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=" + key
                    _r = _rq.post(_u, json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"maxOutputTokens": 1800, "temperature": 0.3, "thinkingConfig": {"thinkingBudget": 0}}}, timeout=40)
                    _diag["gemini_status"] = _r.status_code
                    _diag["gemini_body"] = _r.text[:400]
                except Exception as _e:
                    _diag["gemini_excecao"] = str(_e)
                return jsonify(_diag), 502
            return jsonify({"titulos": [str(t)[:90] for t in titulos[:5]]})
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
    _MAGALU_TOKENS_FILE = os.path.join(_INTEG_DIR, "wrx_magalu_tokens.json")
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
    OLX_CEP = os.environ.get("OLX_CEP", "22795065")
    SHOPEE_PARTNER_ID = int(os.environ.get("SHOPEE_PARTNER_ID", "2035574"))
    SHOPEE_PARTNER_KEY = os.environ.get("SHOPEE_PARTNER_KEY", "shpk4458415353465759486e516147454957414d4c444761414a577570795655")

    # ── Magalu (Magazine Luiza) — OAuth2 ID Magalu + API de marketplace ─────────
    # ⚠️ Este repo é PÚBLICO no GitHub — NUNCA hardcode o client_secret aqui.
    # As credenciais ficam SÓ nas env vars do Railway (MAGALU_CLIENT_ID /
    # MAGALU_CLIENT_SECRET). Audience = https://api.magalu.com (public).
    MAGALU_CLIENT_ID = os.environ.get("MAGALU_CLIENT_ID", "")
    MAGALU_CLIENT_SECRET = os.environ.get("MAGALU_CLIENT_SECRET", "")
    # ⚠️ Usar dominio-das-pecas.pages.dev (Cloudflare Pages — registrado no client E no ar).
    # O apex dominiodaspecas.com.br também está registrado, mas o DNS do apex está QUEBRADO
    # (ver memory project_crm_partshub). O redirect_uri precisa bater EXATO com o registrado.
    MAGALU_REDIRECT_URI = os.environ.get("MAGALU_REDIRECT_URI", "https://dominio-das-pecas.pages.dev/magalu-callback.html")
    MAGALU_API_BASE = os.environ.get("MAGALU_API_BASE", "https://api.magalu.com")
    MAGALU_ID_BASE = "https://id.magalu.com"
    # Escopos pedidos no client (todos saíram AVAILABLE, nenhum PENDING).
    MAGALU_SCOPES = (
        "open:portfolio-categories-seller:read "
        "open:portfolio-skus-seller:read open:portfolio-skus-seller:write "
        "open:portfolio-prices-seller:read open:portfolio-prices-seller:write "
        "open:portfolio-stocks-seller:read open:portfolio-stocks-seller:write "
        "open:portfolio-vehicles-seller:read open:portfolio-vehicles-compatibility-seller:write "
        "open:order-order-seller:read open:order-invoice-seller:read "
        "open:order-delivery-seller:read open:order-delivery-seller:write"
    )
    _magalu_token_mem = {}

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

    # Config generica no user_metadata do Supabase (merge — nao apaga wrx_ml_tokens)
    def _ph_save_meta(key, value):
        jwt = _ph_get_jwt()
        if not jwt:
            return False
        try:
            r = requests.put(
                f"https://{_PH_HOST}/auth/v1/user",
                json={"data": {key: value}},
                headers={"apikey": _PH_ANON, "Authorization": f"Bearer {jwt}", "Content-Type": "application/json"},
                timeout=15
            )
            return r.status_code == 200
        except Exception:
            return False

    def _ph_load_meta(key, default=None):
        jwt = _ph_get_jwt()
        if not jwt:
            return default
        try:
            _r = requests.get(
                f"https://{_PH_HOST}/auth/v1/user",
                headers={"apikey": _PH_ANON, "Authorization": f"Bearer {jwt}"},
                timeout=10
            )
            if _r.status_code == 200:
                return _r.json().get("user_metadata", {}).get(key, default)
        except Exception:
            pass
        return default

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

    # Baixa o pacote (zip XML+PDF) de NF-e do periodo para UMA conta. Retorna dict:
    #   sucesso -> {"ok": True, "conteudo": <bytes>, "content_type": str, "user_id": str}
    #   falha   -> {"ok": False, "erro": str, "http": int, "detalhe": ...}
    def _ml_baixar_nfe_zip(conta, start, end):
        token = _ml_get_user_token(conta)
        if not token:
            return {"ok": False, "conta": conta, "erro": f"conta '{conta}' nao autorizada no ML", "http": 401}
        tokens = _ml_load_tokens()
        user_id = str(tokens.get(conta, {}).get("user_id", "") or "")
        if not user_id:
            try:
                me = requests.get("https://api.mercadolibre.com/users/me",
                                  headers={"Authorization": f"Bearer {token}"}, timeout=10).json()
                user_id = str(me.get("id", ""))
            except Exception as e:
                return {"ok": False, "conta": conta, "erro": f"falha ao obter user_id: {e}", "http": 502}
        if not user_id:
            return {"ok": False, "conta": conta, "erro": "user_id da conta indisponivel", "http": 502}
        url = (
            f"https://api.mercadolibre.com/users/{user_id}/invoices/sites/MLB/batch_request/period/stream"
            f"?start={start}&end={end}&sale=all&return=all&full=all&others=all"
            f"&file_types=xml,pdf&simple_folder=false"
        )
        try:
            r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=120)
        except Exception as e:
            return {"ok": False, "conta": conta, "erro": f"falha na chamada ao ML: {e}", "http": 502}
        ct = r.headers.get("Content-Type", "")
        if r.status_code != 200 or "application/json" in ct:
            try:
                detalhe = r.json()
            except Exception:
                detalhe = r.text[:500]
            return {"ok": False, "conta": conta, "http": r.status_code,
                    "erro": "ML nao retornou pacote de notas (Faturador inativo ou sem notas no periodo)",
                    "detalhe": detalhe}
        return {"ok": True, "conta": conta, "conteudo": r.content, "content_type": ct, "user_id": user_id}

    # NF-e: valida/baixa os XMLs que o ML emitiu num periodo (fluxo mensal pro contador).
    #   GET .../nfe?conta=default&start=AAAAMMDD&end=AAAAMMDD             -> valida/relata (JSON)
    #   GET .../nfe?conta=default&start=...&end=...&download=1           -> baixa o .zip (XML+PDF)
    @app.route("/integracoes/mercadolivre/nfe", methods=["GET", "OPTIONS"])
    def ml_nfe():
        if request.method == "OPTIONS":
            return _options_resp()
        import re as _re_nfe
        conta = request.args.get("conta", "default")
        start = request.args.get("start", "")
        end = request.args.get("end", "")
        if not _re_nfe.fullmatch(r"\d{8}", start) or not _re_nfe.fullmatch(r"\d{8}", end):
            return jsonify({"erro": "Informe start e end no formato AAAAMMDD. Ex.: ?start=20260501&end=20260531"}), 400
        baixar = request.args.get("download") == "1"

        res = _ml_baixar_nfe_zip(conta, start, end)
        if not res["ok"]:
            if res.get("http") == 401:
                return jsonify({"erro": res["erro"]}), 401
            return jsonify({"ok": False, "ml_status": res.get("http"), "aviso": res["erro"],
                            "detalhe": res.get("detalhe")})

        conteudo = res["conteudo"]
        if baixar:
            return Response(conteudo, headers={
                "Content-Type": res.get("content_type") or "application/zip",
                "Content-Disposition": f'attachment; filename="notas-ml-{conta}-{start}-{end}.zip"',
                "Access-Control-Allow-Origin": "*",
            })
        return jsonify({
            "ok": True,
            "conta": conta,
            "periodo": {"start": start, "end": end},
            "user_id": res.get("user_id"),
            "content_type": res.get("content_type"),
            "bytes": len(conteudo),
            "dica": (f"Pacote recebido ({len(conteudo)} bytes). Acrescente &download=1 na URL para baixar o .zip."
                     if conteudo else "Resposta vazia — provavelmente nao ha notas emitidas pelo ML nesse periodo."),
        })

    # E-mail do contador (cadastro editavel pela tela). GET le, POST salva.
    @app.route("/integracoes/contador/email", methods=["GET", "POST", "OPTIONS"])
    def contador_email():
        if request.method == "OPTIONS":
            return _options_resp()
        if request.method == "GET":
            return jsonify({"email": _ph_load_meta("wrx_contador_email", "") or ""})
        import re as _re_em
        body = request.get_json(silent=True) or {}
        email = (body.get("email") or "").strip()
        if email and not _re_em.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            return jsonify({"erro": "e-mail invalido"}), 400
        ok = _ph_save_meta("wrx_contador_email", email)
        return jsonify({"ok": ok, "email": email})

    # Envia os XMLs do periodo pro contador por e-mail (Gmail SMTP). Disparo manual.
    #   POST .../nfe/enviar  body: {start, end, email?, contas?}
    @app.route("/integracoes/mercadolivre/nfe/enviar", methods=["POST", "OPTIONS"])
    def ml_nfe_enviar():
        if request.method == "OPTIONS":
            return _options_resp()
        import re as _re_env
        body = request.get_json(silent=True) or {}
        start = str(body.get("start", ""))
        end = str(body.get("end", ""))
        if not _re_env.fullmatch(r"\d{8}", start) or not _re_env.fullmatch(r"\d{8}", end):
            return jsonify({"erro": "Informe start e end no formato AAAAMMDD"}), 400
        contas = body.get("contas") or ["default", "geisa"]
        email = (body.get("email") or _ph_load_meta("wrx_contador_email", "") or "").strip()
        if not email:
            return jsonify({"erro": "e-mail do contador nao informado nem cadastrado"}), 400

        gmail_user = os.environ.get("GMAIL_USER", "").strip()
        gmail_pass = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
        if not gmail_user or not gmail_pass:
            return jsonify({"erro": "GMAIL_USER / GMAIL_APP_PASSWORD nao configurados no servidor"}), 500

        anexos, resumo = [], []
        for conta in contas:
            res = _ml_baixar_nfe_zip(conta, start, end)
            if res["ok"] and res.get("conteudo"):
                anexos.append((f"notas-ml-{conta}-{start}-{end}.zip", res["conteudo"]))
                resumo.append({"conta": conta, "bytes": len(res["conteudo"]), "anexado": True})
            else:
                resumo.append({"conta": conta, "anexado": False, "motivo": res.get("erro", "vazio")})
        if not anexos:
            return jsonify({"ok": False, "erro": "nenhuma nota encontrada para anexar no periodo", "resumo": resumo})

        import smtplib
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        from email.mime.application import MIMEApplication
        _per = f"{start[6:8]}/{start[4:6]}/{start[0:4]} a {end[6:8]}/{end[4:6]}/{end[0:4]}"
        msg = MIMEMultipart()
        msg["From"] = gmail_user
        msg["To"] = email
        msg["Subject"] = f"Notas fiscais (XML) — periodo {_per}"
        corpo = ("Ola,\n\nSeguem em anexo os XMLs e DANFEs das notas fiscais emitidas no Mercado Livre no periodo "
                 f"{_per}.\n\n" + "\n".join(f"- {n} ({len(c)//1024} KB)" for n, c in anexos)
                 + "\n\nEnviado pelo sistema WRX.")
        msg.attach(MIMEText(corpo, "plain", "utf-8"))
        for nome, conteudo in anexos:
            part = MIMEApplication(conteudo, _subtype="zip")
            part.add_header("Content-Disposition", "attachment", filename=nome)
            msg.attach(part)
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as s:
                s.login(gmail_user, gmail_pass)
                s.sendmail(gmail_user, [email], msg.as_string())
        except Exception as e:
            return jsonify({"ok": False, "erro": f"falha ao enviar e-mail: {e}", "resumo": resumo}), 502

        if body.get("email"):
            _ph_save_meta("wrx_contador_email", email)
        return jsonify({"ok": True, "enviado_para": email, "periodo": _per, "resumo": resumo})

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

    @app.route("/integracoes/mercadolivre/catalogo", methods=["GET", "OPTIONS"])
    def ml_catalogo():
        """Puxa a ficha de um PRODUTO DE CATÁLOGO do ML — igual ao PartsHub.
        Aceita CÓDIGO (ex: MLB12345678, com texto/URL junto) OU NOME (busca no catálogo).
        Por código: devolve a ficha direta. Por nome: busca e devolve a 1ª ficha + opções."""
        if request.method == "OPTIONS":
            return _options_resp()
        bruto = (request.args.get("id") or request.args.get("q") or "").strip()
        if not bruto:
            return jsonify({"erro": "informe o codigo ou nome"}), 400
        token = _get_ml_token()
        hdrs = {"Accept": "application/json"}
        if token:
            hdrs["Authorization"] = f"Bearer {token}"

        def _ficha(pid):
            try:
                r = requests.get(f"https://api.mercadolibre.com/products/{pid}", headers=hdrs, timeout=12)
                if r.status_code != 200:
                    return None
                j = r.json() or {}
                attrs = j.get("attributes") or []
                pics = j.get("pictures") or []
                _at = lambda aid: next((x.get("value_name") for x in attrs if x.get("id") == aid and x.get("value_name")), "")
                # Categoria: o produto de catálogo não traz category_id direto.
                # Descobre pelo predizer (que prefere categoria de carro).
                cat = j.get("category_id") or ""
                cat_nome = ""
                if not cat and j.get("name"):
                    try:
                        ds = requests.get("https://api.mercadolibre.com/sites/MLB/domain_discovery/search",
                                          params={"limit": 6, "q": j.get("name")}, headers=hdrs, timeout=10)
                        if ds.status_code == 200:
                            cands = ds.json() or []
                            esc = _ml_preferir_categoria_carro(cands) or (cands[0] if cands else {})
                            cat = (esc or {}).get("category_id", "") or ""
                            cat_nome = (esc or {}).get("category_name", "") or ""
                    except Exception:
                        pass
                nome = j.get("name") or ""
                marca = _at("BRAND") or _at("VEHICLE_BRAND") or _at("MANUFACTURER")
                modelo = _at("MODEL") or _at("VEHICLE_MODEL")
                if not modelo and marca and nome:
                    mm = re.search(re.escape(marca) + r'\s+([A-Za-zÀ-ÿ0-9\-]{2,})', nome, re.I)
                    if mm:
                        modelo = mm.group(1)
                # Posição (lado / eixo) a partir do atributo do ML + do nome + da categoria
                _low = ((_at("VEHICLE_PARTS_POSITION") or "") + " " + nome + " " + (cat_nome or "")).lower()
                pos_lado = "Direita" if "direit" in _low else ("Esquerda" if "esquerd" in _low else "")
                pos_eixo = "Traseira" if ("trasei" in _low or "vigia" in _low) else ("Dianteira" if "diantei" in _low else "")
                return {
                    "id": j.get("id") or pid,
                    "nome": nome,
                    "category_id": cat,
                    "categoria_nome": cat_nome,
                    "domain_id": j.get("domain_id") or "",
                    "marca": marca,
                    "modelo": modelo,
                    "part_number": _at("PART_NUMBER"),
                    "oem": _at("OEM") or _at("PART_NUMBER"),
                    "posicao_lado": pos_lado,
                    "posicao_eixo": pos_eixo,
                    "atributos": [{"id": a.get("id"), "nome": a.get("name"), "valor": a.get("value_name")}
                                  for a in attrs if a.get("value_name")],
                    "fotos": [(p.get("secure_url") or p.get("url")) for p in pics if (p.get("secure_url") or p.get("url"))],
                }
            except Exception:
                return None

        m = re.search(r'MLB\d{4,}', bruto, re.I)
        if m:
            f = _ficha(m.group(0).upper())
            if not f:
                return jsonify({"erro": "catalogo nao encontrado", "id": m.group(0).upper()}), 404
            return jsonify({"ok": True, "produto": f, "resultados": [{"id": f["id"], "nome": f["nome"]}]})

        # Busca por NOME no catálogo
        try:
            rs = requests.get("https://api.mercadolibre.com/products/search",
                              params={"site_id": "MLB", "status": "active", "q": bruto},
                              headers=hdrs, timeout=12)
            resultados = []
            if rs.status_code == 200:
                for p in (rs.json().get("results") or [])[:8]:
                    resultados.append({"id": p.get("id"), "nome": p.get("name")})
            produto = _ficha(resultados[0]["id"]) if resultados else None
            return jsonify({"ok": True, "resultados": resultados, "produto": produto})
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
        def _put_status(st):
            return requests.put(
                f"https://api.mercadolibre.com/items/{ml_id}",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"status": st}, timeout=15)
        try:
            if novo == "closed":
                # ML EXIGE pausar antes de encerrar (active->closed dá "status is not modifiable").
                # Zera o estoque tambem (ajuda itens que travam pra fechar).
                try:
                    requests.put(f"https://api.mercadolibre.com/items/{ml_id}",
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                                 json={"status": "paused", "available_quantity": 0}, timeout=15)
                except Exception:
                    pass
                r = _put_status("closed")
            else:
                r = _put_status(novo)
            if r.status_code == 200:
                return jsonify({"ok": True, "ml_id": ml_id, "status": novo})
            # Mensagem mais clara pro usuário
            _txt = r.text[:300]
            _amig = ""
            if "not_modifiable" in _txt or "not modifiable" in _txt:
                _amig = "O Mercado Livre não deixa encerrar este anúncio agora (pode estar em análise/revisão ou recém-criado). Tente pausar e encerrar mais tarde, ou encerre pelo painel do ML."
            return jsonify({"ok": False, "erro": _amig or f"ML {r.status_code}: {_txt}", "ml_raw": _txt}), 502
        except Exception as e:
            return jsonify({"ok": False, "erro": str(e)}), 500

    # ════════════════════════════════════════════════════════════════════════
    # REVISÃO DE PREÇOS — revisa anúncios ativos vs concorrência (ML) p/ aprovação
    # Fase 1: lote pequeno sob demanda. NUNCA altera preço sozinho — só com aprovação.
    # ════════════════════════════════════════════════════════════════════════
    _REVISAO_STATUS = {"rodando": False, "feitos": 0, "total": 0, "msg": "", "erro": ""}

    def _ml_atualizar_preco_item(ml_id, conta, preco):
        token = _ml_get_user_token(conta or "default")
        if not token:
            return False, f"conta '{conta}' sem token ML"
        try:
            r = requests.put(
                f"https://api.mercadolibre.com/items/{ml_id}",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"price": float(preco)}, timeout=20)
            if r.status_code == 200:
                return True, ""
            return False, f"ML {r.status_code}: {r.text[:200]}"
        except Exception as e:
            return False, str(e)

    def _revisao_atualizar_preco_ml(ml_id, conta, preco):
        return _ml_atualizar_preco_item(ml_id, conta, preco)

    def _revisao_processar(limite):
        # roda em thread separada — busca concorrência é lenta (scraping ~10-30s/item)
        try:
            _REVISAO_STATUS.update({"rodando": True, "feitos": 0, "total": 0, "msg": "selecionando anúncios ativos", "erro": ""})
            # 1) Carrega anúncios ML ATIVOS (paginado — PostgREST corta em 1000)
            anuncios = []
            off = 0
            while True:
                r = requests.get(
                    f"{_WRX_SB_URL}/rest/v1/ml_anuncios?select=*&status=eq.active&order=sku.asc&limit=1000&offset={off}",
                    headers=_wrx_headers(), timeout=20)
                if r.status_code != 200:
                    break
                rows = r.json()
                if not rows:
                    break
                anuncios.extend(rows)
                if len(rows) < 1000:
                    break
                off += 1000
                if off > 50000:
                    break
            # estoque disponível + sku válido
            anuncios = [a for a in anuncios if (a.get("estoque") or 0) > 0 and (a.get("sku") or "").strip()]
            # já tem revisão pendente? evita reprocessar
            ja = set()
            try:
                rr = requests.get(f"{_WRX_SB_URL}/rest/v1/revisao_precos?select=sku&status=eq.pendente&limit=5000",
                                  headers=_wrx_headers(), timeout=15)
                if rr.status_code == 200:
                    ja = {(x.get("sku") or "").upper() for x in rr.json()}
            except Exception:
                pass
            # prioridade: mais antigos primeiro (SKU sequencial). dedup por SKU.
            vistos = set()
            fila = []
            for a in sorted(anuncios, key=lambda x: str(x.get("sku") or "")):
                sku = (a.get("sku") or "").upper()
                if sku in vistos or sku in ja:
                    continue
                vistos.add(sku)
                fila.append(a)
            fila = fila[:max(1, int(limite or 10))]
            _REVISAO_STATUS["total"] = len(fila)
            for a in fila:
                sku = (a.get("sku") or "").strip()
                meu = float(a.get("preco") or 0)
                # OEM (e título) do cadastro
                oem = ""
                titulo_est = ""
                try:
                    pr = requests.get(
                        f"{_WRX_SB_URL}/rest/v1/pecas_estoque?select=oem,titulo&sku=eq.{sku}&limit=1",
                        headers=_wrx_headers(), timeout=15)
                    if pr.status_code == 200 and pr.json():
                        oem = (pr.json()[0].get("oem") or "").strip()
                        titulo_est = (pr.json()[0].get("titulo") or "").strip()
                except Exception:
                    pass
                eh_oem = bool(oem)
                consulta = oem or titulo_est or (a.get("titulo") or "")
                menor = media = sug = 0.0
                qtd = 0
                if consulta:
                    try:
                        precos_brutos = _revisao_coletar_precos(consulta, eh_oem=eh_oem)
                        precos = _revisao_aparar(precos_brutos)  # tira muito barato / muito caro
                        if precos:
                            menor = round(min(precos), 2)
                            media = round(sum(precos) / len(precos), 2)
                            qtd = len(precos)
                            # sugestão = mediana aparada (corta outliers) com 3% abaixo (regra da usuária)
                            sug = round(calcular_preco_sugerido(precos) * 0.97, 2)
                    except Exception as e:
                        print(f"[REVISAO] erro busca sku={sku}: {e}")
                # diferença: meu preço vs referência (sugestão; senão menor do mercado)
                ref = sug or menor
                dif = 0.0
                if meu > 0 and ref > 0:
                    dif = round((meu - ref) / meu * 100, 1)  # + = estou mais caro
                absd = abs(dif)
                prio = "manter" if absd <= 10 else ("revisar" if absd <= 20 else "alta")
                row = {
                    "sku": sku, "ml_id": a.get("ml_id", ""), "conta": a.get("conta", "default"),
                    "titulo": a.get("titulo", "") or titulo_est, "thumbnail": a.get("thumbnail", ""), "oem": oem,
                    "meu_preco": meu, "menor_mercado": menor, "media_mercado": media, "sugestao": sug,
                    "diferenca_pct": dif, "prioridade": prio, "fonte_qtd": qtd, "status": "pendente",
                }
                try:
                    requests.post(
                        f"{_WRX_SB_URL}/rest/v1/revisao_precos?on_conflict=sku,conta",
                        headers={**_wrx_headers(), "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates"},
                        json=row, timeout=15)
                except Exception as e:
                    print(f"[REVISAO] erro gravar sku={sku}: {e}")
                _REVISAO_STATUS["feitos"] += 1
            _REVISAO_STATUS["msg"] = "concluído"
        except Exception as e:
            _REVISAO_STATUS["erro"] = str(e)
            print(f"[REVISAO] erro geral: {e}")
        finally:
            _REVISAO_STATUS["rodando"] = False

    def _max_sku_numerico():
        # Maior SKU numérico do pecas_estoque (pra gerar o próximo na sequência).
        mx = 0
        off = 0
        while True:
            try:
                r = requests.get(
                    f"{_WRX_SB_URL}/rest/v1/pecas_estoque?select=sku&limit=1000&offset={off}",
                    headers=_wrx_headers(), timeout=20)
                if r.status_code != 200:
                    break
                rows = r.json()
            except Exception:
                break
            if not rows:
                break
            for x in rows:
                s = str(x.get("sku") or "")
                if s.isdigit():
                    n = int(s)
                    if n > mx:
                        mx = n
            if len(rows) < 1000:
                break
            off += 1000
            if off > 300000:
                break
        return mx

    @app.route("/locais", methods=["GET", "OPTIONS"])
    def locais_estoque():
        # Lista os locais de estoque que já existem (distintos) — pro picker do cadastro/localizar.
        if request.method == "OPTIONS":
            return _options_resp()
        locs = set()
        off = 0
        while True:
            try:
                r = requests.get(
                    f"{_WRX_SB_URL}/rest/v1/pecas_estoque?select=loc&loc=not.is.null&limit=1000&offset={off}",
                    headers=_wrx_headers(), timeout=20)
                if r.status_code != 200:
                    break
                rows = r.json()
            except Exception:
                break
            if not rows:
                break
            for x in rows:
                v = (x.get("loc") or "").strip()
                if v:
                    locs.add(v)
            if len(rows) < 1000:
                break
            off += 1000
            if off > 300000:
                break
        # ALÉM dos locais já em uso, inclui os locais CADASTRADOS na tela "Gestão de Locais"
        # (salvos em dx_config chave 'locais_estoque_v1'), pra aparecerem no picker mesmo
        # sem nenhuma peça ainda — senão vira ovo-e-galinha (não aparece pra poder usar).
        try:
            rc = requests.get(
                f"{_WRX_SB_URL}/rest/v1/dx_config?chave=eq.locais_estoque_v1&select=valor",
                headers=_wrx_headers(), timeout=15)
            if rc.status_code == 200 and rc.json():
                valor = rc.json()[0].get("valor") or {}
                for l in (valor.get("locais") or []):
                    if l.get("active") is False:
                        continue
                    partes = [str(l.get(k) or "").strip()
                              for k in ("yard", "corridor", "section", "shelf", "position", "slot", "box")]
                    partes = [p for p in partes if p]
                    if partes:
                        locs.add(" → ".join(partes))
        except Exception:
            pass
        return jsonify({"ok": True, "locais": sorted(locs)})

    @app.route("/peca-info", methods=["GET", "OPTIONS"])
    def peca_info():
        # Carrega uma peça por SKU (pra tela de localizar): titulo, loc atual, foto.
        if request.method == "OPTIONS":
            return _options_resp()
        sku = (request.args.get("sku") or "").strip()
        if not sku:
            return jsonify({"ok": False, "erro": "sku obrigatório"}), 400
        try:
            g = requests.get(
                f"{_WRX_SB_URL}/rest/v1/pecas_estoque?select=sku,titulo,loc,fotos,oem,marca,modelo&sku=eq.{sku}&limit=1",
                headers=_wrx_headers(), timeout=15)
            rows = g.json() if g.status_code == 200 else []
        except Exception:
            rows = []
        if not rows:
            return jsonify({"ok": False, "erro": "peça não encontrada"}), 404
        return jsonify({"ok": True, "peca": rows[0]})

    @app.route("/peca-localizar", methods=["POST", "OPTIONS"])
    def peca_localizar():
        # Salva SÓ a localização de uma peça (tela rápida do funcionário).
        if request.method == "OPTIONS":
            return _options_resp()
        d = request.get_json(force=True, silent=True) or {}
        sku = (d.get("sku") or "").strip()
        loc = (d.get("loc") or "").strip()
        if not sku:
            return jsonify({"ok": False, "erro": "sku obrigatório"}), 400
        try:
            g = requests.get(f"{_WRX_SB_URL}/rest/v1/pecas_estoque?select=sku,titulo&sku=eq.{sku}&limit=1",
                             headers=_wrx_headers(), timeout=15)
            rows = g.json() if g.status_code == 200 else []
        except Exception:
            rows = []
        if not rows:
            return jsonify({"ok": False, "erro": "peça não encontrada (confere o código)"}), 404
        try:
            r = requests.patch(
                f"{_WRX_SB_URL}/rest/v1/pecas_estoque?sku=eq.{sku}",
                headers={**_wrx_headers(), "Content-Type": "application/json", "Prefer": "return=minimal"},
                json={"loc": loc}, timeout=15)
            if r.status_code in (200, 204):
                return jsonify({"ok": True, "sku": sku, "titulo": rows[0].get("titulo"), "loc": loc})
            return jsonify({"ok": False, "erro": f"Supabase {r.status_code}: {r.text[:150]}"}), 502
        except Exception as e:
            return jsonify({"ok": False, "erro": str(e)}), 500

    @app.route("/proximo-sku", methods=["GET", "OPTIONS"])
    def proximo_sku():
        if request.method == "OPTIONS":
            return _options_resp()
        mx = _max_sku_numerico()
        return jsonify({"ok": True, "ultimo": mx, "proximo": mx + 1})

    def _subir_fotos_storage_cadastro(sku, fotos):
        """Sobe fotos base64 (data:image) pro Supabase Storage (bucket fotos-pecas) e devolve
        URLs públicas — igual o Criar Anúncio faz. URL http já pronta é mantida. Falha numa
        foto não derruba as outras. Sem isso, o base64 era gravado cru na coluna (some no
        cache do estoque e incha o banco)."""
        import base64 as _b64, time as _time
        from urllib.parse import quote as _quote
        out = []
        sku_safe = _quote(str(sku), safe="")
        for i, f in enumerate((fotos or [])[:8]):
            if not isinstance(f, str) or not f:
                continue
            if f.startswith("http"):
                out.append(f); continue
            if not f.startswith("data:image"):
                continue
            try:
                raw = _b64.b64decode(f.split(",", 1)[1])
                path = f"{sku_safe}/{int(_time.time()*1000)}_{i}.jpg"
                up = requests.post(
                    f"{_WRX_SB_URL}/storage/v1/object/fotos-pecas/{path}",
                    headers={"apikey": _WRX_SB_KEY, "Authorization": f"Bearer {_WRX_SB_KEY}", "Content-Type": "image/jpeg"},
                    data=raw, timeout=25)
                if up.status_code in (200, 201):
                    out.append(f"{_WRX_SB_URL}/storage/v1/object/public/fotos-pecas/{path}")
            except Exception:
                pass
        return out

    @app.route("/cadastro-rapido", methods=["POST", "OPTIONS"])
    def cadastro_rapido():
        # Cadastro rápido (mobile) -> grava em pecas_estoque com origem='manual'
        # (o sync do PartsHub NÃO mexe em origem!=null, então não some).
        if request.method == "OPTIONS":
            return _options_resp()
        d = request.get_json(force=True, silent=True) or {}
        nome = (d.get("nome") or d.get("titulo") or "").strip()
        if not nome:
            return jsonify({"ok": False, "erro": "nome obrigatório"}), 400
        sku = (d.get("sku") or "").strip()
        if not sku:
            # próximo SKU na SEQUÊNCIA (maior numérico + 1), igual ao PartsHub
            sku = str(_max_sku_numerico() + 1)
        def _num(v):
            try:
                return float(str(v).replace(",", ".")) if v not in (None, "") else None
            except Exception:
                return None
        fotos = d.get("fotos") or []
        if isinstance(fotos, str):
            fotos = [fotos]
        # Sobe as fotos pro Storage e guarda URL (não base64): aparece no card e sobrevive ao cache.
        fotos = _subir_fotos_storage_cadastro(sku, fotos)
        compat = d.get("compatibilidade") or []
        if isinstance(compat, str):
            compat = [c.strip() for c in compat.split(",") if c.strip()]
        row = {
            "sku": sku,
            "titulo": (d.get("titulo") or nome).strip(),
            "cadastrado_em": _datetime.utcnow().isoformat() + "Z",  # sem isso a peça não entra nas "recentes" do estoque
            "cadastrado_por": (d.get("cadastrado_por") or d.get("cadastradoPor") or d.get("usuario") or "").strip(),
            "oem": (d.get("oem") or "").strip(),
            "marca": (d.get("marca") or "").strip(),
            "modelo": (d.get("modelo") or "").strip(),
            "ano": (str(d.get("ano") or "").strip()),
            "preco": _num(d.get("preco")) or 0,
            "qtd": int(d.get("qtd") or 1),
            "cond": (d.get("cond") or "Usada").strip(),
            "loc": (d.get("loc") or "").strip(),
            "categoria": (d.get("categoria") or "").strip(),
            "peso": _num(d.get("peso")),
            "altura": _num(d.get("altura")),
            "largura": _num(d.get("largura")),
            "comprimento": _num(d.get("comprimento")),
            "fotos": fotos[:8],
            "compatibilidade": compat,
            "origem": "manual",
        }
        # remove chaves None (colunas de medida podem não existir ainda)
        row = {k: v for k, v in row.items() if v is not None}
        try:
            r = requests.post(
                f"{_WRX_SB_URL}/rest/v1/pecas_estoque?on_conflict=sku",
                headers={**_wrx_headers(), "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates,return=minimal"},
                json=row, timeout=20)
            if r.status_code in (200, 201, 204):
                return jsonify({"ok": True, "sku": sku})
            # se falhou por coluna inexistente (medidas/origem), tenta sem os extras
            base = {k: row[k] for k in ("sku", "titulo", "oem", "marca", "modelo", "ano", "preco", "qtd", "cond", "loc", "categoria", "fotos", "compatibilidade", "cadastrado_em", "cadastrado_por") if k in row}
            base["origem"] = "manual"
            r2 = requests.post(
                f"{_WRX_SB_URL}/rest/v1/pecas_estoque?on_conflict=sku",
                headers={**_wrx_headers(), "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates,return=minimal"},
                json=base, timeout=20)
            if r2.status_code in (200, 201, 204):
                return jsonify({"ok": True, "sku": sku, "aviso": "salvo sem medidas (colunas podem faltar)"})
            return jsonify({"ok": False, "erro": f"Supabase {r.status_code}: {r.text[:200]}"}), 502
        except Exception as e:
            return jsonify({"ok": False, "erro": str(e)}), 500

    @app.route("/revisao-precos/setup-tabela", methods=["GET", "POST", "OPTIONS"])
    def revisao_setup_tabela():
        if request.method == "OPTIONS":
            return _options_resp()
        sql = """
CREATE TABLE IF NOT EXISTS revisao_precos (
  id BIGSERIAL PRIMARY KEY,
  sku TEXT,
  ml_id TEXT,
  conta TEXT DEFAULT 'default',
  titulo TEXT,
  thumbnail TEXT,
  oem TEXT,
  meu_preco FLOAT DEFAULT 0,
  menor_mercado FLOAT DEFAULT 0,
  media_mercado FLOAT DEFAULT 0,
  sugestao FLOAT DEFAULT 0,
  diferenca_pct FLOAT DEFAULT 0,
  prioridade TEXT DEFAULT 'manter',
  fonte_qtd INTEGER DEFAULT 0,
  status TEXT DEFAULT 'pendente',
  preco_aplicado FLOAT,
  criado_em TIMESTAMPTZ DEFAULT NOW(),
  revisado_em TIMESTAMPTZ,
  UNIQUE(sku, conta)
);
CREATE INDEX IF NOT EXISTS idx_revisao_status ON revisao_precos(status);
CREATE INDEX IF NOT EXISTS idx_revisao_prioridade ON revisao_precos(prioridade);
"""
        service_key = os.environ.get("SUPABASE_SERVICE_KEY", "")
        if service_key:
            try:
                r = requests.post(
                    f"{_WRX_SB_URL}/rest/v1/rpc/exec",
                    headers={"apikey": service_key, "Authorization": f"Bearer {service_key}", "Content-Type": "application/json"},
                    json={"sql": sql}, timeout=15)
                if r.status_code in (200, 201, 204):
                    return jsonify({"ok": True, "msg": "Tabela revisao_precos criada"})
            except Exception:
                pass
        return jsonify({"ok": False, "msg": "Execute o SQL manualmente no Supabase", "sql": sql})

    @app.route("/revisao-precos/rodar", methods=["POST", "OPTIONS"])
    def revisao_rodar():
        if request.method == "OPTIONS":
            return _options_resp()
        if _REVISAO_STATUS.get("rodando"):
            return jsonify({"ok": False, "erro": "Já está rodando uma revisão", "status": _REVISAO_STATUS}), 409
        data = request.get_json(force=True, silent=True) or {}
        limite = int(data.get("limite") or 10)
        limite = max(1, min(limite, 100))  # teto de segurança
        import threading
        t = threading.Thread(target=_revisao_processar, args=(limite,), daemon=True)
        t.start()
        return jsonify({"ok": True, "iniciado": True, "limite": limite})

    @app.route("/revisao-precos/salvar-item", methods=["POST", "OPTIONS"])
    def revisao_salvar_item():
        # Recebe os preços JÁ RASPADOS (pelo navegador) e grava a linha calculada.
        # Ponte enquanto o Railway não tem navegador headless p/ raspar sozinho.
        if request.method == "OPTIONS":
            return _options_resp()
        d = request.get_json(force=True, silent=True) or {}
        sku = (d.get("sku") or "").strip()
        if not sku:
            return jsonify({"ok": False, "erro": "sku obrigatório"}), 400
        # PORTEIRO: usa o estoque FRESCO do PartsHub (pecas_estoque.qtd). Se a peça já
        # foi vendida / está zerada, NÃO entra na revisão (o ml_anuncios fica defasado).
        try:
            er = requests.get(
                f"{_WRX_SB_URL}/rest/v1/pecas_estoque?select=qtd&sku=eq.{sku}&limit=1",
                headers=_wrx_headers(), timeout=12)
            if er.status_code == 200 and er.json():
                qtd_fresca = er.json()[0].get("qtd")
                if qtd_fresca is not None and float(qtd_fresca) <= 0:
                    return jsonify({"ok": True, "pulado": True, "motivo": "sem estoque (vendido)"})
        except Exception:
            pass
        # NÃO re-raspar peça que você JÁ decidiu (aprovado/ignorado). Senão o upsert
        # abaixo regravaria status='pendente' e o selo "Preço revisado" sumiria do card.
        conta_chk = (d.get("conta") or "default").strip()
        try:
            ex = requests.get(
                f"{_WRX_SB_URL}/rest/v1/revisao_precos?select=status&sku=eq.{sku}&conta=eq.{conta_chk}&limit=1",
                headers=_wrx_headers(), timeout=12)
            if ex.status_code == 200 and ex.json():
                st_atual = (ex.json()[0].get("status") or "").lower()
                if st_atual in ("aprovado", "ignorado"):
                    return jsonify({"ok": True, "pulado": True, "motivo": "já " + st_atual})
        except Exception:
            pass
        oem = (d.get("oem") or "").strip()
        # OEM "real" = parece código (tem dígito e não é palavra tipo 'original')
        eh_oem = bool(re.search(r"\d", oem)) and oem.lower() not in ("original", "000", "0")
        consulta = oem if eh_oem else (d.get("titulo") or "")
        pares = d.get("pares") or []
        precos = _revisao_filtrar_pares(pares, consulta, eh_oem=eh_oem)
        precos = _revisao_aparar(precos)
        meu = float(d.get("meu_preco") or 0)
        menor = round(min(precos), 2) if precos else 0.0
        media = round(sum(precos) / len(precos), 2) if precos else 0.0
        sug = round(calcular_preco_sugerido(precos) * 0.97, 2) if precos else 0.0
        ref = sug or menor
        dif = round((meu - ref) / meu * 100, 1) if (meu > 0 and ref > 0) else 0.0
        absd = abs(dif)
        prio = "manter" if absd <= 10 else ("revisar" if absd <= 20 else "alta")
        row = {
            "sku": sku, "ml_id": d.get("ml_id", ""), "conta": d.get("conta", "default"),
            "titulo": d.get("titulo", ""), "thumbnail": d.get("thumbnail", ""), "oem": oem,
            "meu_preco": meu, "menor_mercado": menor, "media_mercado": media, "sugestao": sug,
            "diferenca_pct": dif, "prioridade": prio, "fonte_qtd": len(precos), "status": "pendente",
        }
        try:
            r = requests.post(
                f"{_WRX_SB_URL}/rest/v1/revisao_precos?on_conflict=sku,conta",
                headers={**_wrx_headers(), "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates"},
                json=row, timeout=15)
            ok = r.status_code in (200, 201, 204)
            return jsonify({"ok": ok, "linha": row, "supabase": r.status_code, "sb_msg": (None if ok else r.text[:200])})
        except Exception as e:
            return jsonify({"ok": False, "erro": str(e)}), 500

    @app.route("/revisao-precos/limpar", methods=["POST", "OPTIONS"])
    def revisao_limpar():
        # Apaga as linhas (por status) — pra zerar a lista e começar limpo.
        if request.method == "OPTIONS":
            return _options_resp()
        d = request.get_json(force=True, silent=True) or {}
        status = (d.get("status") or "pendente").strip()
        try:
            url = f"{_WRX_SB_URL}/rest/v1/revisao_precos?"
            url += "id=gt.0" if status == "todos" else f"status=eq.{status}"
            r = requests.delete(url, headers={**_wrx_headers(), "Prefer": "return=minimal"}, timeout=20)
            return jsonify({"ok": r.status_code in (200, 204), "supabase": r.status_code})
        except Exception as e:
            return jsonify({"ok": False, "erro": str(e)}), 500

    @app.route("/revisao-precos/fila", methods=["POST", "OPTIONS"])
    def revisao_fila():
        # Recebe SKUs SELECIONADOS no Estoque → grava como status='fila' (aguardando busca).
        # O script local pega a fila, raspa e converte em 'pendente' com os preços.
        if request.method == "OPTIONS":
            return _options_resp()
        d = request.get_json(force=True, silent=True) or {}
        skus = d.get("skus") or []
        if not skus:
            return jsonify({"ok": False, "erro": "skus obrigatório"}), 400
        add = 0
        pulados = 0
        for raw in skus[:500]:
            sku = str(raw).strip()
            if not sku:
                continue
            # estoque fresco (PartsHub) — pula vendido/sem estoque
            est = {}
            try:
                er = requests.get(
                    f"{_WRX_SB_URL}/rest/v1/pecas_estoque?select=titulo,oem,preco,qtd&sku=eq.{sku}&limit=1",
                    headers=_wrx_headers(), timeout=12)
                if er.status_code == 200 and er.json():
                    est = er.json()[0]
            except Exception:
                pass
            if est.get("qtd") is not None and float(est.get("qtd") or 0) <= 0:
                pulados += 1
                continue
            # peça JÁ APROVADA não volta pra fila — senão o selo "Preço revisado" sumiria.
            try:
                ax = requests.get(
                    f"{_WRX_SB_URL}/rest/v1/revisao_precos?select=status&sku=eq.{sku}&status=eq.aprovado&limit=1",
                    headers=_wrx_headers(), timeout=12)
                if ax.status_code == 200 and ax.json():
                    pulados += 1
                    continue
            except Exception:
                pass
            # dados do anúncio ML (ml_id + conta + preço p/ aprovar depois)
            an = {}
            try:
                ar = requests.get(
                    f"{_WRX_SB_URL}/rest/v1/ml_anuncios?select=ml_id,conta,titulo,preco,thumbnail&sku=eq.{sku}&limit=1",
                    headers=_wrx_headers(), timeout=12)
                if ar.status_code == 200 and ar.json():
                    an = ar.json()[0]
            except Exception:
                pass
            row = {
                "sku": sku, "ml_id": an.get("ml_id", ""), "conta": an.get("conta", "default"),
                "titulo": an.get("titulo") or est.get("titulo") or "", "thumbnail": an.get("thumbnail", ""),
                "oem": est.get("oem", "") or "",
                "meu_preco": float(an.get("preco") or est.get("preco") or 0),
                "menor_mercado": 0, "media_mercado": 0, "sugestao": 0, "diferenca_pct": 0,
                "prioridade": "manter", "fonte_qtd": 0, "status": "fila",
            }
            try:
                requests.post(
                    f"{_WRX_SB_URL}/rest/v1/revisao_precos?on_conflict=sku,conta",
                    headers={**_wrx_headers(), "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates"},
                    json=row, timeout=12)
                add += 1
            except Exception:
                pass
        return jsonify({"ok": True, "adicionados": add, "pulados_sem_estoque": pulados})

    @app.route("/revisao-precos/status", methods=["GET", "OPTIONS"])
    def revisao_status():
        if request.method == "OPTIONS":
            return _options_resp()
        return jsonify({"ok": True, **_REVISAO_STATUS})

    @app.route("/revisao-precos/testar", methods=["GET", "OPTIONS"])
    def revisao_testar():
        """Diagnóstico: coleta os preços (já filtrados/usados) de uma consulta e mostra
        quantos achou e a faixa, p/ conferir profundidade e assertividade da raspagem."""
        if request.method == "OPTIONS":
            return _options_resp()
        import statistics as _st
        q = request.args.get("q", "").strip()
        eh_oem = request.args.get("oem", "") in ("1", "true", "sim")
        if not q:
            return jsonify({"erro": "parametro ?q= obrigatorio"}), 400
        brutos = _revisao_coletar_precos(q, eh_oem=eh_oem)
        aparados = _revisao_aparar(brutos)
        out = {"consulta": q, "eh_oem": eh_oem,
               "qtd_coletado": len(brutos), "qtd_apos_outliers": len(aparados)}
        if aparados:
            out["menor"] = min(aparados)
            out["maior"] = max(aparados)
            out["media"] = round(sum(aparados) / len(aparados), 2)
            out["mediana"] = round(_st.median(aparados), 2)
            out["amostra"] = sorted(aparados)[:20]
        return jsonify(out)

    @app.route("/revisao-precos/listar", methods=["GET", "OPTIONS"])
    def revisao_listar():
        if request.method == "OPTIONS":
            return _options_resp()
        status = request.args.get("status", "pendente").strip()
        try:
            url = f"{_WRX_SB_URL}/rest/v1/revisao_precos?select=*&limit=2000"
            if status and status != "todos":
                url += f"&status=eq.{status}"
            r = requests.get(url, headers=_wrx_headers(), timeout=20)
            if r.status_code != 200:
                return jsonify({"ok": False, "erro": f"Supabase {r.status_code}: {r.text[:200]}"}), 502
            rows = r.json()
            # ordena: alta > revisar > manter; depois maior diferença
            ordem = {"alta": 0, "revisar": 1, "manter": 2}
            rows.sort(key=lambda x: (ordem.get(x.get("prioridade"), 3), -abs(float(x.get("diferenca_pct") or 0))))
            return jsonify({"ok": True, "itens": rows, "total": len(rows)})
        except Exception as e:
            return jsonify({"ok": False, "erro": str(e)}), 500

    def _revisao_patch(rev_id, campos):
        try:
            r = requests.patch(
                f"{_WRX_SB_URL}/rest/v1/revisao_precos?id=eq.{rev_id}",
                headers={**_wrx_headers(), "Content-Type": "application/json", "Prefer": "return=minimal"},
                json=campos, timeout=15)
            return r.status_code in (200, 204)
        except Exception:
            return False

    @app.route("/revisao-precos/aprovar", methods=["POST", "OPTIONS"])
    def revisao_aprovar():
        if request.method == "OPTIONS":
            return _options_resp()
        data = request.get_json(force=True, silent=True) or {}
        rev_id = data.get("id")
        novo_preco = data.get("preco")  # opcional — se vier, é "Editar Preço"
        if not rev_id:
            return jsonify({"ok": False, "erro": "id obrigatório"}), 400
        # busca a linha
        try:
            r = requests.get(f"{_WRX_SB_URL}/rest/v1/revisao_precos?id=eq.{rev_id}&limit=1",
                             headers=_wrx_headers(), timeout=15)
            rows = r.json() if r.status_code == 200 else []
            if not rows:
                return jsonify({"ok": False, "erro": "revisão não encontrada"}), 404
            rev = rows[0]
        except Exception as e:
            return jsonify({"ok": False, "erro": str(e)}), 500
        preco = float(novo_preco) if novo_preco else float(rev.get("sugestao") or 0)
        if preco <= 0:
            return jsonify({"ok": False, "erro": "sem preço válido para aplicar (sugestão vazia). Use Editar Preço."}), 400
        sku = (rev.get("sku") or "").strip()
        ml_id = rev.get("ml_id")
        avisos = []
        # 1) SEMPRE grava o preço no PRODUTO (pra publicar já com o preço revisado)
        if sku:
            try:
                requests.patch(f"{_WRX_SB_URL}/rest/v1/pecas_estoque?sku=eq.{sku}",
                               headers={**_wrx_headers(), "Content-Type": "application/json", "Prefer": "return=minimal"},
                               json={"preco": preco}, timeout=15)
            except Exception:
                avisos.append("não consegui gravar no produto")
        # 2) Se JÁ tem anúncio no ML, atualiza o preço lá também
        if ml_id:
            ok, err = _revisao_atualizar_preco_ml(ml_id, rev.get("conta"), preco)
            if ok:
                try:
                    requests.patch(f"{_WRX_SB_URL}/rest/v1/ml_anuncios?ml_id=eq.{ml_id}",
                                   headers={**_wrx_headers(), "Content-Type": "application/json", "Prefer": "return=minimal"},
                                   json={"preco": preco}, timeout=15)
                except Exception:
                    pass
            else:
                avisos.append(f"ML não atualizado: {err}")
        _revisao_patch(rev_id, {"status": "aprovado", "preco_aplicado": preco, "revisado_em": "now"})
        return jsonify({"ok": True, "ml_id": ml_id or "", "sku": sku, "preco": preco,
                        "aplicado": ("produto + anúncio" if ml_id else "produto"), "avisos": avisos})

    @app.route("/revisao-precos/ignorar", methods=["POST", "OPTIONS"])
    def revisao_ignorar():
        if request.method == "OPTIONS":
            return _options_resp()
        data = request.get_json(force=True, silent=True) or {}
        rev_id = data.get("id")
        if not rev_id:
            return jsonify({"ok": False, "erro": "id obrigatório"}), 400
        ok = _revisao_patch(rev_id, {"status": "ignorado", "revisado_em": "now"})
        return jsonify({"ok": ok})

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
        preco = round(float(data.get("preco", 0) or 0), 2)  # ML exige no máx. 2 casas decimais (price.invalid)
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
        # GARANTIA do vendedor (regra Domínio das Peças): 30 dias. Campo oficial do anúncio no ML.
        ml_payload["sale_terms"] = [
            {"id": "WARRANTY_TYPE", "value_name": "Garantia do vendedor"},
            {"id": "WARRANTY_TIME", "value_name": "30 dias"},
        ]
        attrs = list(data.get("attributes") or [])
        attr_ids = {a.get("id") for a in attrs}
        # Atributos obrigatórios: PART_NUMBER, BRAND, MODEL, dimensões de embalagem
        if "PART_NUMBER" not in attr_ids and sku:
            attrs.append({"id": "PART_NUMBER", "value_name": sku})
        # SELLER_SKU: é o "Código de identificação (SKU)" que aparece no ML novo.
        # Sem ele o campo SKU fica VAZIO no anúncio (seller_custom_field sozinho não preenche).
        if "SELLER_SKU" not in attr_ids and sku:
            attrs.append({"id": "SELLER_SKU", "value_name": sku})
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
        # ── BLOCO ISOLADO: atributos OBRIGATÓRIOS da categoria que ainda faltam ──
        # Aditivo: só PREENCHE o que falta (não altera o resto). Evita "Validation error"
        # por atributo obrigatório ausente. Ex: Rodas (MLB4860) exigem RIM_DIAMETER (aro).
        try:
            import re as _re  # garante _re disponível (acima só importa no ramo sem marca)
            _cat_all = _ml_categoria_attrs(ml_payload.get("category_id"))
            _ja = set(attr_ids) | {a.get("id") for a in attrs}
            for _aid, _a in (_cat_all or {}).items():
                _tg = _a.get("tags") or {}
                if not _tg.get("required") or _aid in _ja or _tg.get("fixed"):
                    continue
                # Diâmetro do aro: extrai "Aro 18" / "aro18" do título
                if "DIAMETER" in _aid or "RIM" in _aid:
                    _md = _re.search(r'aro\s*0?(\d{2})', _titulo, _re.I)
                    if _md:
                        _un = _a.get("default_unit") or '"'
                        attrs.append({"id": _aid, "value_name": _md.group(1) + _un})
                        continue
                # Outros obrigatórios de LISTA: usa o 1º valor permitido (melhor que faltar)
                _vals = _a.get("values") or []
                if _vals and _vals[0].get("id"):
                    attrs.append({"id": _aid, "value_id": _vals[0]["id"], "value_name": _vals[0].get("name")})
        except Exception:
            pass
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
                # Garante que o anúncio entre ATIVO (usuária quer publicado automático, não pausado).
                _status_final = item.get("status", "active")
                if _status_final != "active":
                    try:
                        _ra = requests.put(
                            f"https://api.mercadolibre.com/items/{item_id}",
                            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                            json={"status": "active"}, timeout=10
                        )
                        if _ra.status_code in (200, 201):
                            _status_final = (_ra.json() or {}).get("status", _status_final)
                            print(f"[ML] item {item_id} ativado (status={_status_final})")
                        else:
                            print(f"[ML] nao ativou item {item_id}: {_ra.status_code} {_ra.text[:200]}")
                    except Exception as _ea:
                        print(f"[ML] erro ao ativar item {item_id}: {_ea}")
                # ── COMPATIBILIDADE veicular: evita o ML PAUSAR o anúncio por falta de ficha ──
                # Lê a compat rica do banco (pecas_estoque.compatibilidade — migrada do PartsHub com
                # mlBrandId/mlModelId/mlYearId), resolve os produtos de catálogo de veículo do ML e
                # associa ao user_product do anúncio. DEFENSIVO: o anúncio já está publicado; se isto
                # falhar, não derruba a publicação. (Decifrado/testado 23/06/2026 — ver memória.)
                try:
                    _up = item.get("user_product_id")
                    if not _up and item_id:
                        try:
                            _di = requests.get(f"https://api.mercadolibre.com/items/{item_id}",
                                               headers={"Authorization": f"Bearer {token}"}, timeout=12).json()
                            _up = _di.get("user_product_id")
                        except Exception:
                            _up = None
                    if not _up:
                        print(f"[ML-COMPAT] item {item_id} sem user_product_id — pulando compatibilidade")
                    else:
                        _jatem = False
                        try:
                            _gc = requests.get(f"https://api.mercadolibre.com/items/{item_id}/compatibilities",
                                               headers={"Authorization": f"Bearer {token}"}, timeout=12).json()
                            _jatem = bool(_gc.get("products"))
                        except Exception:
                            pass
                        if _jatem:
                            print(f"[ML-COMPAT] item {item_id} já tem compatibilidade — não duplica")
                        else:
                            _cv = []
                            try:
                                _rc = requests.get(f"{_WRX_SB_URL}/rest/v1/pecas_estoque?sku=eq.{sku}&select=compatibilidade",
                                                   headers={"apikey": _WRX_SB_KEY, "Authorization": f"Bearer {_WRX_SB_KEY}"}, timeout=15)
                                if _rc.status_code == 200 and _rc.json():
                                    _cv = _rc.json()[0].get("compatibilidade") or []
                                    if isinstance(_cv, str):
                                        _cv = json.loads(_cv)
                            except Exception:
                                _cv = []
                            _combos = set()
                            for _c in (_cv or []):
                                if not isinstance(_c, dict):
                                    continue
                                _b = str(_c.get("mlBrandId") or "").strip()
                                _m = str(_c.get("mlModelId") or "").strip()
                                _y = str(_c.get("mlYearId") or "").strip()
                                if _b and _m and _y:
                                    _combos.add((_b, _m, _y))
                            _cpids = []
                            for (_b, _m, _y) in _combos:
                                try:
                                    _rs = requests.post("https://api.mercadolibre.com/catalog_compatibilities/products_search/chunks",
                                                        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                                                        json={"domain_id": "MLB-CARS_AND_VANS", "site_id": "MLB",
                                                              "known_attributes": [{"id": "BRAND", "value_ids": [_b]},
                                                                                   {"id": "MODEL", "value_ids": [_m]},
                                                                                   {"id": "VEHICLE_YEAR", "value_ids": [_y]}]}, timeout=20)
                                    if _rs.status_code == 200:
                                        for _p in (_rs.json().get("results") or []):
                                            _cid = _p.get("id") or _p.get("catalog_product_id")
                                            if _cid:
                                                _cpids.append(_cid)
                                except Exception:
                                    pass
                            _cpids = list(dict.fromkeys(_cpids))[:200]
                            if not _cpids:
                                print(f"[ML-COMPAT] item {item_id} sku {sku}: sem catalog_products (compat sem ml*Id no banco?)")
                            else:
                                try:
                                    _rcomp = requests.post(f"https://api.mercadolibre.com/user-products/{_up}/compatibilities",
                                                           headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                                                           json={"domain_id": "MLB-CARS_AND_VANS",
                                                                 "products": [{"id": _cid, "creation_source": "SELLER"} for _cid in _cpids]}, timeout=25)
                                    print(f"[ML-COMPAT] item {item_id}: {len(_cpids)} veículos -> {_rcomp.status_code} {_rcomp.text[:160]}")
                                except Exception as _ecp:
                                    print(f"[ML-COMPAT] POST compat erro: {_ecp}")
                except Exception as _ecomp:
                    print(f"[ML-COMPAT] erro não crítico: {_ecomp}")
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
                            "status": _status_final,
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
        # Tenta Supabase primeiro — PAGINADO (PostgREST corta em 1000 por requisicao;
        # com limit=5000 vinha so 1000 e a maioria dos SKUs aparecia como "Sem anuncio").
        try:
            dados = []
            _off = 0
            while True:
                r = requests.get(
                    # ORDER por chave UNICA (ml_id,conta) -> paginacao por offset ESTAVEL.
                    # Antes era order=sync_at.desc: o sync grava milhares de linhas com o MESMO
                    # sync_at; sem desempate unico, o offset pula/omite linhas entre paginas
                    # (anuncios recem-publicados sumiam do anuncios-db -> card "Sem anuncio").
                    f"{_WRX_SB_URL}/rest/v1/ml_anuncios?select=*&order=ml_id.asc,conta.asc&limit=1000&offset={_off}",
                    headers=_wrx_headers(), timeout=15
                )
                if r.status_code != 200:
                    break
                _rows = r.json()
                if not _rows:
                    break
                dados.extend(_rows)
                if len(_rows) < 1000:
                    break
                _off += 1000
                if _off > 50000:
                    break
            if dados:
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

    def _ml_carregar_skus_sistema():
        # Carrega TODOS os SKUs de pecas_estoque com PAGINACAO. O PostgREST corta em
        # 1000 linhas por requisicao (mesmo com limit=20000) — por isso so vinha 1000,
        # e a maioria dos anuncios caia como "nao vinculado" falsamente.
        skus = set()
        off = 0
        try:
            while True:
                rp = requests.get(
                    f"{_WRX_SB_URL}/rest/v1/pecas_estoque?select=sku&limit=1000&offset={off}",
                    headers=_wrx_headers(), timeout=20)
                if rp.status_code != 200:
                    break
                rows = rp.json()
                if not rows:
                    break
                for p in rows:
                    if p.get("sku"):
                        skus.add(str(p["sku"]).strip().upper())
                if len(rows) < 1000:
                    break
                off += 1000
                if off > 60000:
                    break
        except Exception:
            pass
        return skus

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
        skus_sistema = _ml_carregar_skus_sistema()

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

    @app.route("/integracoes/mercadolivre/relatorio-vinculo", methods=["GET", "OPTIONS"])
    def ml_relatorio_vinculo():
        # SOMENTE LEITURA: nao escreve nada no ML. Conta ativos vinculados vs nao
        # vinculados (sem SKU no titulo / SKU nao existe no estoque) + amostra.
        if request.method == "OPTIONS":
            return _options_resp()
        tokens_data = _ml_load_tokens()
        if not tokens_data:
            return jsonify({"ok": False, "erro": "ML nao autorizado"}), 401
        skus_sistema = _ml_carregar_skus_sistema()
        resumo = {"skus_no_sistema": len(skus_sistema), "contas": [],
                  "total_ativos": 0, "vinculados": 0, "sem_sku_no_titulo": 0, "sku_nao_existe": 0}
        amostra = []
        for conta_nome in list(tokens_data.keys()):
            token = _ml_get_user_token(conta_nome)
            if not token:
                continue
            user_id = tokens_data.get(conta_nome, {}).get("user_id", "") or ""
            if not user_id:
                try:
                    _r = requests.get("https://api.mercadolibre.com/users/me",
                                      headers={"Authorization": f"Bearer {token}"}, timeout=10)
                    if _r.status_code == 200:
                        user_id = str(_r.json().get("id", ""))
                except Exception:
                    pass
            if not user_id:
                continue
            ids = _ml_buscar_todos_ids(token, user_id, "active")
            itens = _ml_buscar_detalhes_lote(token, ids)
            c_total = c_vinc = c_sem = c_nao = 0
            for item in itens:
                sku = _ml_extrair_sku(item).upper()
                c_total += 1
                if not sku:
                    c_sem += 1
                    if len(amostra) < 40:
                        amostra.append({"conta": conta_nome, "id": item.get("id"),
                                        "titulo": (item.get("title") or "")[:60],
                                        "motivo": "sem SKU no titulo", "sku": ""})
                elif sku in skus_sistema:
                    c_vinc += 1
                else:
                    c_nao += 1
                    if len(amostra) < 40:
                        amostra.append({"conta": conta_nome, "id": item.get("id"),
                                        "titulo": (item.get("title") or "")[:60],
                                        "motivo": "SKU nao existe no estoque", "sku": sku})
            resumo["contas"].append({"conta": conta_nome, "ativos": c_total,
                                     "vinculados": c_vinc, "sem_sku": c_sem, "sku_nao_existe": c_nao})
            resumo["total_ativos"] += c_total
            resumo["vinculados"] += c_vinc
            resumo["sem_sku_no_titulo"] += c_sem
            resumo["sku_nao_existe"] += c_nao
        resumo["amostra_nao_vinculados"] = amostra
        resumo["ok"] = True
        return jsonify(resumo)

    _estoque_sem_ml_cache = {"ts": 0, "itens": []}

    @app.route("/integracoes/mercadolivre/estoque-sem-anuncio", methods=["GET", "OPTIONS"])
    def ml_estoque_sem_anuncio():
        # SOMENTE LEITURA: lista peças do estoque (qtd>=1) que NÃO têm anúncio no ML.
        # Sinal: SKU base não está em ml_anuncios (qualquer status) E sem ml_url.
        if request.method == "OPTIONS":
            return _options_resp()
        if request.args.get("forcar") not in ("1", "true", "sim") and \
           (time.time() - _estoque_sem_ml_cache["ts"]) < 300 and _estoque_sem_ml_cache["itens"]:
            return jsonify({"ok": True, "total": len(_estoque_sem_ml_cache["itens"]),
                            "itens": _estoque_sem_ml_cache["itens"], "cache": True})

        def _base(s):
            return str(s or "").strip().upper().split("-")[0]

        # SKUs que JÁ têm anúncio no ML (qualquer status/conta)
        ml_skus = set()
        try:
            for r in _sb_get_all("ml_anuncios?select=sku"):
                b = _base(r.get("sku"))
                if b:
                    ml_skus.add(b)
        except Exception as _e:
            return jsonify({"ok": False, "erro": f"ml_anuncios: {_e}"}), 500

        itens = []
        try:
            est = _sb_get_all(
                "pecas_estoque?select=sku,titulo,fotos,preco,marca,modelo,ano,categoria,qtd,ml_url"
                "&qtd=gte.1&order=cadastrado_em.desc")
            for p in est:
                sku = str(p.get("sku") or "").strip()
                if not sku:
                    continue
                if _base(sku) in ml_skus:
                    continue
                mlu = str(p.get("ml_url") or "").strip()
                if mlu.startswith("http"):
                    continue  # já tem link do ML conhecido
                fotos = p.get("fotos") or []
                foto = ""
                if isinstance(fotos, list) and fotos:
                    foto = str(fotos[0] or "")
                itens.append({
                    "sku": sku,
                    "titulo": p.get("titulo") or "",
                    "foto": foto.replace("http://", "https://"),
                    "preco": p.get("preco"),
                    "marca": p.get("marca") or "",
                    "modelo": p.get("modelo") or "",
                    "ano": p.get("ano") or "",
                    "categoria": p.get("categoria") or "",
                    "qtd": p.get("qtd") or 0,
                })
        except Exception as _e:
            return jsonify({"ok": False, "erro": f"pecas_estoque: {_e}"}), 500

        _estoque_sem_ml_cache["ts"] = time.time()
        _estoque_sem_ml_cache["itens"] = itens
        return jsonify({"ok": True, "total": len(itens), "itens": itens, "cache": False})

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

    # Mapa: sub_status do ML -> causa legível + o que fazer pra reativar
    _ML_SUBSTATUS_INFO = {
        "out_of_stock":             ("Sem estoque", "Reponha a quantidade (maior que 0) e reative o anúncio."),
        "under_review":             ("Em revisão pelo Mercado Livre", "Aguarde a revisão do ML ou ajuste o que foi solicitado."),
        "waiting_for_patch":        ("Aguardando correção", "O ML pediu correções no anúncio; ajuste os dados e reenvie."),
        "suspended":                ("Suspenso por infração", "Revise título, descrição, fotos e as políticas do ML, depois recorra/reative."),
        "deleted":                  ("Excluído", "Anúncio excluído no ML; precisa ser recriado."),
        "expired":                  ("Expirado", "Anúncio vencido; republique para voltar ao ar."),
        "freezed":                  ("Congelado pelo ML", "Verifique pendências da conta/reputação no Mercado Livre."),
        "picture_download_pending": ("Processando imagens", "Aguarde o ML terminar de processar as fotos."),
        "forbidden":                ("Produto não permitido", "Produto/categoria não permitido; revise a categoria ou o item."),
        "inactive":                 ("Inativo", "Anúncio inativo; revise os dados e reative."),
    }

    @app.route("/integracoes/mercadolivre/situacao", methods=["GET", "OPTIONS"])
    def ml_situacao():
        """Painel de SITUAÇÃO dos anúncios ML (todas as contas):
        - Pausados (com a CAUSA = sub_status + o que fazer pra reativar)
        - Sem estoque (sub_status out_of_stock OU quantidade 0)
        - Vendidos e Cancelados (orders dos últimos ?dias=)
        Defensivo: cada conta isolada em try/except, nunca derruba a resposta."""
        if request.method == "OPTIONS":
            return _options_resp()
        try:
            dias = int(request.args.get("dias", "30"))
        except Exception:
            dias = 30
        tokens = _ml_load_tokens()
        if not tokens:
            return jsonify({"ok": False, "erro": "Mercado Livre nao autorizado"}), 401
        from datetime import timedelta
        date_from = (_datetime.utcnow() - timedelta(days=dias)).strftime("%Y-%m-%dT00:00:00.000-00:00")
        pausados, sem_estoque, vendidos, cancelados = [], [], [], []
        contas_info, erros = [], []
        for conta in list(tokens.keys()):
            token = _ml_get_user_token(conta)
            if not token:
                erros.append(f"{conta}: sem token")
                continue
            user_id = _ml_conta_user_id(conta, token)
            if not user_id:
                erros.append(f"{conta}: sem user_id")
                continue
            contas_info.append(conta)
            # ── 1) PAUSADOS (paginado; separa sem-estoque) ──
            try:
                ids_paused = []
                _off = 0
                while _off < 1000:
                    rs = requests.get(
                        f"https://api.mercadolibre.com/users/{user_id}/items/search",
                        params={"status": "paused", "limit": 50, "offset": _off},
                        headers={"Authorization": f"Bearer {token}"}, timeout=15)
                    if rs.status_code != 200:
                        erros.append(f"{conta}: pausados HTTP {rs.status_code}")
                        break
                    dd = rs.json()
                    res = dd.get("results", [])
                    ids_paused.extend(res)
                    if len(res) < 50:
                        break
                    _off += 50
                for it in _ml_buscar_detalhes_lote(token, ids_paused):
                    subs = it.get("sub_status") or []
                    sub = subs[0] if subs else ""
                    qty = it.get("available_quantity", 0)
                    causa, acao = _ML_SUBSTATUS_INFO.get(
                        sub, ("Pausado manualmente", "Reative quando quiser (pausa feita por você ou sem motivo informado pelo ML)."))
                    item = {
                        "conta": conta, "mlId": it.get("id", ""), "titulo": it.get("title", ""),
                        "preco": it.get("price", 0), "estoque": qty, "sku": _ml_extrair_sku(it),
                        "thumbnail": it.get("thumbnail", ""), "permalink": it.get("permalink", ""),
                        "subStatus": sub, "causa": causa, "acao": acao,
                    }
                    if sub == "out_of_stock" or qty == 0:
                        item["causa"], item["acao"] = _ML_SUBSTATUS_INFO["out_of_stock"]
                        sem_estoque.append(item)
                    else:
                        pausados.append(item)
            except Exception as _e:
                erros.append(f"{conta}: pausados {_e}")
            # ── 2) ORDERS (vendidos + cancelados) ──
            try:
                _off = 0
                while _off < 400:
                    ro = requests.get(
                        "https://api.mercadolibre.com/orders/search",
                        params={"seller": user_id, "sort": "date_desc", "limit": 50,
                                "offset": _off, "order.date_created.from": date_from},
                        headers={"Authorization": f"Bearer {token}"}, timeout=15)
                    if ro.status_code != 200:
                        erros.append(f"{conta}: orders HTTP {ro.status_code}")
                        break
                    do = ro.json()
                    results = do.get("results", [])
                    for o in results:
                        itens = [{"titulo": i.get("item", {}).get("title", ""),
                                   "sku": i.get("item", {}).get("seller_sku", ""),
                                   "qty": i.get("quantity", 1),
                                   "preco": i.get("unit_price", 0)} for i in o.get("order_items", [])]
                        reg = {
                            "conta": conta, "order_id": o.get("id"),
                            "status": o.get("status", ""), "data": o.get("date_created", ""),
                            "total": o.get("total_amount", 0),
                            "comprador": o.get("buyer", {}).get("nickname", ""),
                            "itens": itens,
                        }
                        if o.get("status") == "cancelled":
                            cancelados.append(reg)
                        elif o.get("status") in ("paid", "confirmed", "invoiced", "shipped", "delivered"):
                            vendidos.append(reg)
                    if len(results) < 50:
                        break
                    _off += 50
            except Exception as _e:
                erros.append(f"{conta}: orders {_e}")
        for lst in (vendidos, cancelados):
            lst.sort(key=lambda x: x.get("data", ""), reverse=True)
        return jsonify({
            "ok": True, "contas": contas_info, "dias": dias,
            "pausados": pausados, "semEstoque": sem_estoque,
            "vendidos": vendidos, "cancelados": cancelados,
            "totais": {"pausados": len(pausados), "semEstoque": len(sem_estoque),
                       "vendidos": len(vendidos), "cancelados": len(cancelados)},
            "erros": erros,
        })

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
        _sku = data.get("_sku") or data.get("sku", "")
        fotos = [f for f in (data.get("fotos") or data.get("images") or []) if f and f.startswith("http")]
        # A OLX só aceita fotos por URL (http). As fotos editadas no canvas chegam como base64
        # (data:image/...) e seriam filtradas aqui -> anúncio SEM FOTO (bug confirmado 23/06: o
        # parachoque 109437 entrou sem foto). Fallback: se sobrou 0 foto http, usa as fotos do
        # estoque (pecas_estoque.fotos — já subidas como http pelo passo de salvar antes de publicar).
        if not fotos and _sku:
            try:
                _rf = requests.get(f"{_WRX_SB_URL}/rest/v1/pecas_estoque?sku=eq.{_sku}&select=fotos",
                                   headers={"apikey": _WRX_SB_KEY, "Authorization": f"Bearer {_WRX_SB_KEY}"}, timeout=12)
                if _rf.ok and _rf.json():
                    _bf = _rf.json()[0].get("fotos") or []
                    fotos = [f for f in _bf if isinstance(f, str) and f.startswith("http")][:10]
                    print(f"[OLX] payload sem foto http; usei {len(fotos)} fotos do estoque (sku {_sku})")
            except Exception as _ef:
                print(f"[OLX] fallback fotos estoque falhou: {_ef}")
        preco = float(data.get("preco") or data.get("price") or 0)
        if preco < 180:
            return jsonify({"ok": False, "erro": f"OLX exige preco minimo de R$ 180. Valor enviado: R$ {preco:.2f}"}), 400
        # CATEGORIA: peças e acessórios de CARRO no autoupload da OLX = 2101 (número).
        # O valor antigo {"id":"8020"} estava errado (8020 não existe) e a OLX recusava com
        # statusCode -6 "Without permission" — o plano do cliente é de PEÇAS, então o anúncio
        # precisa ir na categoria de peças. 'condition' é OBRIGATÓRIO em params (1=novo, 2=usado).
        _cond_raw = str(data.get("condicao") or data.get("cond") or data.get("condition") or data.get("estado") or "").strip().lower()
        _condition = "1" if (("nov" in _cond_raw) or _cond_raw in ("new", "1")) else "2"  # default: usado (desmonte)
        # FORMATO do autoupload (doc OLX /anuncio/api/import.html), campos OBRIGATÓRIOS no
        # nível do anúncio: id, operation, type, category, subject, body, price, phone, zipcode, images.
        # phone = INTEIRO (DDD+numero, só dígitos); zipcode = string numérica (campo direto, NÃO em locations).
        _tel = "".join(c for c in str(data.get("telefone") or OLX_TELEFONE) if c.isdigit())[:11]
        _cep = "".join(c for c in str(data.get("cep") or OLX_CEP) if c.isdigit())
        _id = "".join(c for c in str(_sku) if (c.isalnum() or c in "_{}-")) or "0"
        payload = {
            "id": _id[:19],                      # único, regex [A-Za-z0-9_{}-]{1,19}
            "operation": "insert",               # insert = inserir/editar
            "type": "s",                         # s = venda
            "category": 2101,                    # Peças e acessórios > Carros/vans/utilitários
            "subject": (data.get("subject") or data.get("titulo") or data.get("nomeInterno", "Peca Automotiva"))[:90],
            "body": (data.get("body") or data.get("descricao") or data.get("titulo", ""))[:6000],
            "price": int(preco),                 # sem centavos
            "phone": int(_tel) if len(_tel) >= 10 else 0,
            "phone_hidden": False,
            "zipcode": _cep,
            "images": fotos[:20],
            # params da categoria 2101: condition é OBRIGATÓRIO (1=novo, 2=usado).
            # parts_name_cars=4 (Peças automotivas) e exchange=2 (não aceita troca) são opcionais.
            "params": {"condition": _condition, "parts_name_cars": "4", "exchange": "2"},
        }
        try:
            # A OLX exige o access_token DENTRO do corpo JSON, no mesmo nível do ad_list
            # (não basta o header Authorization). Sem isso a API responde -6 "Without
            # permission" com errors=[] — confirmado pelo suporte OLX (11/06/2026).
            _r = requests.put("https://apps.olx.com.br/autoupload/import",
                              headers={"Authorization": f"Bearer {_olx_token_mem['access_token']}", "Content-Type": "application/json"},
                              json={"access_token": _olx_token_mem["access_token"], "ad_list": [payload]}, timeout=20)
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

    # ─── Magalu (Magazine Luiza) ────────────────────────────────────────────────
    #   OAuth2 Authorization Code via ID Magalu (id.magalu.com). O refresh_token é
    #   longo e crítico, então o token é guardado de forma DURÁVEL no dx_config
    #   (chave magalu_token) — diferente do OLX/ML que usam arquivo em /tmp, que o
    #   Railway apaga a cada deploy/restart. Arquivo local serve só de cache rápido.
    def _magalu_token_save(tok):
        global _magalu_token_mem
        _magalu_token_mem = tok
        try:
            with open(_MAGALU_TOKENS_FILE, "w") as _f:
                json.dump(tok, _f)
        except Exception:
            pass
        try:
            requests.post(
                f"{_WRX_SB_URL}/rest/v1/dx_config",
                headers={**_wrx_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
                json={"chave": "magalu_token", "valor": tok}, timeout=12)
        except Exception as _e:
            print(f"[MAGALU] falha ao gravar token no dx_config: {_e}")

    def _magalu_token_load():
        global _magalu_token_mem
        if _magalu_token_mem.get("access_token"):
            return _magalu_token_mem
        # tenta arquivo local (cache rápido) e depois dx_config (durável)
        try:
            with open(_MAGALU_TOKENS_FILE) as _f:
                _magalu_token_mem = json.load(_f)
        except Exception:
            pass
        if not _magalu_token_mem.get("access_token"):
            try:
                _r = requests.get(f"{_WRX_SB_URL}/rest/v1/dx_config",
                                  params={"chave": "eq.magalu_token", "select": "valor"},
                                  headers=_wrx_headers(), timeout=12)
                if _r.status_code == 200 and _r.json():
                    _v = _r.json()[0].get("valor")
                    if isinstance(_v, dict) and _v.get("access_token"):
                        _magalu_token_mem = _v
            except Exception:
                pass
        return _magalu_token_mem

    def _magalu_trocar_code(code, redir=None):
        """Troca authorization_code por access/refresh token. Retorna (ok, dict_ou_erro)."""
        _redir = (redir or MAGALU_REDIRECT_URI).strip()
        try:
            _r = requests.post(f"{MAGALU_ID_BASE}/oauth/token",
                               headers={"Content-Type": "application/json"},
                               json={
                                   "grant_type": "authorization_code",
                                   "client_id": MAGALU_CLIENT_ID,
                                   "client_secret": MAGALU_CLIENT_SECRET,
                                   "redirect_uri": _redir,
                                   "code": code,
                               }, timeout=15)
            if _r.status_code != 200:
                return False, f"Magalu {_r.status_code}: {_r.text[:300]}"
            _d = _r.json()
            tok = {
                "access_token": _d.get("access_token", ""),
                "refresh_token": _d.get("refresh_token", ""),
                "expires_at": time.time() + _d.get("expires_in", 3600),
                "scope": _d.get("scope", ""),
            }
            _magalu_token_save(tok)
            return True, tok
        except Exception as _e:
            return False, str(_e)

    def _magalu_access_token():
        """Retorna um access_token válido, renovando via refresh_token se necessário.
        Retorna "" se não autorizado."""
        tok = _magalu_token_load()
        if not tok.get("access_token"):
            return ""
        # renova se faltam < 2 min e há refresh_token
        if tok.get("expires_at", 0) - time.time() < 120 and tok.get("refresh_token"):
            try:
                _r = requests.post(f"{MAGALU_ID_BASE}/oauth/token",
                                   headers={"Content-Type": "application/x-www-form-urlencoded"},
                                   data={
                                       "grant_type": "refresh_token",
                                       "client_id": MAGALU_CLIENT_ID,
                                       "client_secret": MAGALU_CLIENT_SECRET,
                                       "refresh_token": tok.get("refresh_token", ""),
                                   }, timeout=15)
                if _r.status_code == 200:
                    _d = _r.json()
                    tok = {
                        "access_token": _d.get("access_token", ""),
                        "refresh_token": _d.get("refresh_token", tok.get("refresh_token", "")),
                        "expires_at": time.time() + _d.get("expires_in", 3600),
                        "scope": _d.get("scope", tok.get("scope", "")),
                    }
                    _magalu_token_save(tok)
                    print("[MAGALU] token renovado via refresh_token")
                else:
                    print(f"[MAGALU] refresh falhou: {_r.status_code} {_r.text[:150]}")
            except Exception as _e:
                print(f"[MAGALU] erro refresh: {_e}")
        return tok.get("access_token", "")

    def _magalu_auth_url(audience=None):
        url = (
            f"{MAGALU_ID_BASE}/login"
            f"?client_id={MAGALU_CLIENT_ID}"
            f"&redirect_uri={_urlparse.quote(MAGALU_REDIRECT_URI, safe='')}"
            f"&scope={_urlparse.quote(MAGALU_SCOPES, safe='')}"
            f"&response_type=code&choose_tenants=true"
        )
        if audience:
            url += f"&audience={_urlparse.quote(audience, safe='')}"
        return url

    @app.route("/integracoes/magalu/config", methods=["GET", "OPTIONS"])
    def magalu_config():
        if request.method == "OPTIONS":
            return _options_resp()
        configured = bool(MAGALU_CLIENT_ID and MAGALU_CLIENT_SECRET)
        tok = _magalu_token_load()
        token_saved = bool(tok.get("access_token"))
        forcar = request.args.get("forcar") in ("1", "true", "sim")
        # ?audience=... testa emitir o token p/ a audience da API (resolve 401?)
        audience = (request.args.get("audience") or "").strip() or None
        auth_url = _magalu_auth_url(audience) if (configured and (not token_saved or forcar or audience)) else None
        return jsonify({
            "configured": configured,
            "tokenSaved": token_saved,
            "authUrl": auth_url,
            "scope": tok.get("scope", ""),
            "redirectUri": MAGALU_REDIRECT_URI,
        })

    @app.route("/integracoes/magalu/oauth/callback")
    @app.route("/callback/magalu")
    def magalu_oauth_callback():
        code = request.args.get("code", "")
        erro = request.args.get("error", "")
        if erro:
            return jsonify({"erro": f"Magalu recusou: {erro}"}), 400
        if not code or not MAGALU_CLIENT_ID:
            return jsonify({"erro": "Magalu nao configurada ou codigo ausente"}), 400
        ok, res = _magalu_trocar_code(code)
        if not ok:
            return jsonify({"erro": res}), 400
        return ("<html><body style='font-family:sans-serif;text-align:center;padding:40px;"
                "background:#0f172a;color:#fff'><h2 style='color:#22c55e'>&#10003; Magalu conectada!</h2>"
                "<p style='color:#9ca3af'>Token salvo. Pode fechar esta janela.</p></body></html>")

    @app.route("/integracoes/magalu/oauth/trocar-codigo")
    def magalu_trocar_codigo():
        code = request.args.get("code", "")
        redir = request.args.get("redirect_uri", "").strip() or None
        if not code or not MAGALU_CLIENT_ID:
            return jsonify({"erro": "code ausente ou Magalu nao configurada"}), 400
        ok, res = _magalu_trocar_code(code, redir)
        if not ok:
            return jsonify({"ok": False, "erro": res}), 400
        return jsonify({"ok": True, "msg": "Magalu conectada", "scope": res.get("scope", "")})

    @app.route("/integracoes/magalu/status", methods=["GET", "OPTIONS"])
    def magalu_status():
        if request.method == "OPTIONS":
            return _options_resp()
        tok = _magalu_token_load()
        if not tok.get("access_token"):
            return jsonify({"ok": False, "conectada": False, "erro": "Magalu nao autorizada"}), 200
        valido = _magalu_access_token()
        return jsonify({
            "ok": bool(valido),
            "conectada": bool(valido),
            "scope": tok.get("scope", ""),
            "expiraEm": int(max(0, tok.get("expires_at", 0) - time.time())),
        })

    @app.route("/integracoes/magalu/api", methods=["GET", "OPTIONS"])
    def magalu_api_proxy():
        """Explorador read-only: repassa GET pra API da Magalu com o Bearer token.
        Ex: /integracoes/magalu/api?path=seller/v1/portfolios/categories&_QS=...
        'path' é o caminho relativo à MAGALU_API_BASE (com ou sem barra inicial).
        Qualquer outro query param (exceto path) é repassado como querystring."""
        if request.method == "OPTIONS":
            return _options_resp()
        token = _magalu_access_token()
        if not token:
            return jsonify({"ok": False, "erro": "Magalu nao autorizada"}), 401
        path = (request.args.get("path") or "").strip().lstrip("/")
        if not path:
            return jsonify({"ok": False, "erro": "informe ?path="}), 400
        # repassa os demais params como querystring (exceto path e _tenant)
        extra = {k: v for k, v in request.args.items() if k not in ("path", "_tenant")}
        _hdrs = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        _tenant = (request.args.get("_tenant") or "").strip()
        if _tenant:
            _hdrs["x-tenant-id"] = _tenant
        # headers arbitrários p/ debug: ?_h=Nome:Valor (pode repetir). Ex: _h=X-Api-Key:abc
        for _hv in request.args.getlist("_h"):
            if ":" in _hv:
                _hn, _, _hval = _hv.partition(":")
                _hdrs[_hn.strip()] = _hval.strip()
        extra = {k: v for k, v in extra.items() if k != "_h"}
        url = f"{MAGALU_API_BASE}/{path}"
        try:
            _r = requests.get(url, headers=_hdrs, params=extra, timeout=20)
            _ct = _r.headers.get("content-type", "")
            body = _r.json() if "json" in _ct else _r.text[:2000]
            return jsonify({"ok": _r.ok, "status": _r.status_code, "url": _r.url, "body": body}), (200 if _r.ok else _r.status_code)
        except Exception as _e:
            return jsonify({"ok": False, "erro": str(_e)}), 500

    def _magalu_montar_sku_payload(p):
        """Monta o corpo do POST /seller/v1/portfolios/skus a partir de uma peça
        (dict com campos do pecas_estoque OU do JSON enviado pelo front).
        ⚠️ Nomes de campo baseados na doc Magalu; confirmar/ajustar no 1º teste real
        (a API valida e devolve os campos que faltam). GTIN vazio = peça de desmonte:
        manda o motivo da isenção na ficha técnica."""
        def _num(*ks):
            for k in ks:
                v = p.get(k)
                if v not in (None, "", 0, "0"):
                    try:
                        return float(v)
                    except Exception:
                        pass
            return None
        ean = (p.get("ean") or p.get("gtin") or "").strip()
        fotos = [f for f in (p.get("imagens") or p.get("fotos") or []) if isinstance(f, str) and f.startswith("http")]
        # ficha técnica / atributos (datasheet). Sem EAN → motivo de GTIN vazio.
        datasheet = []
        if not ean:
            # peça usada de desmonte não tem código de barras
            datasheet.append({"name": "gtin_isento", "value": "Produto sem GTIN/EAN"})
        payload = {
            "code": str(p.get("sku") or p.get("id") or "").strip(),     # SKU do vendedor
            "name": (p.get("titulo") or p.get("nome") or "")[:120],
            "description": (p.get("descricao") or p.get("titulo") or "")[:8000],
            "brand": (p.get("marca") or "").strip(),                     # precisa casar c/ catálogo Magalu
            "category": str(p.get("categoria_magalu") or p.get("categoria") or "").strip(),  # ID da categoria
            "ncm": "".join(c for c in str(p.get("ncm") or "") if c.isdigit()),
            "dimensions": {
                "weight": _num("peso", "peso_kg"),        # kg
                "height": _num("altura"),                 # cm
                "width": _num("largura"),
                "length": _num("comprimento"),
            },
            "images": [{"url": u} for u in fotos[:20]],
            "datasheet": datasheet,
        }
        if ean:
            payload["gtin"] = ean
        return payload

    @app.route("/integracoes/magalu/publicar", methods=["POST", "OPTIONS"])
    def magalu_publicar():
        """Estrutura de envio do anúncio: 3 chamadas encadeadas (SKU → preço → estoque).
        Aceita {sku:"..."} (busca a peça no pecas_estoque) OU o objeto da peça inteiro.
        ⚠️ Enquanto a API devolver 401 (app não autorizado / audience), nada é criado —
        mas a estrutura fica pronta. Cada passo retorna o status real da Magalu."""
        if request.method == "OPTIONS":
            return _options_resp()
        token = _magalu_access_token()
        if not token:
            return jsonify({"ok": False, "erro": "Magalu nao autorizada — clique em Conectar"}), 401
        data = request.get_json(force=True) or {}
        peca = data
        # se veio só o sku, busca a peça no estoque (reusa os dados já cadastrados)
        if data.get("sku") and not data.get("titulo"):
            try:
                _r = requests.get(f"{_WRX_SB_URL}/rest/v1/pecas_estoque",
                                  params={"sku": f"eq.{data['sku']}", "select": "*"},
                                  headers=_wrx_headers(), timeout=12)
                if _r.ok and _r.json():
                    peca = {**_r.json()[0], **data}  # data sobrescreve o estoque
            except Exception as _e:
                print(f"[MAGALU] falha ao buscar peça {data.get('sku')}: {_e}")
        _hdrs = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "x-tenant-id": data.get("_tenant") or "GENPUB.b114d77c-0181-4571-8949-be95ae5a02e3",
        }
        sku_code = str(peca.get("sku") or peca.get("id") or "").strip()
        resultado = {"sku": sku_code, "passos": {}}

        def _chamar(nome, metodo, path, body):
            url = f"{MAGALU_API_BASE}/{path}"
            try:
                _r = requests.request(metodo, url, headers=_hdrs, json=body, timeout=25)
                _ct = _r.headers.get("content-type", "")
                _b = _r.json() if "json" in _ct else _r.text[:600]
                resultado["passos"][nome] = {"status": _r.status_code, "ok": _r.ok, "resp": _b}
                return _r.ok
            except Exception as _e:
                resultado["passos"][nome] = {"status": 0, "ok": False, "erro": str(_e)}
                return False

        # 1) cria/atualiza o SKU
        sku_payload = _magalu_montar_sku_payload(peca)
        resultado["sku_payload"] = sku_payload
        ok_sku = _chamar("sku", "POST", "seller/v1/portfolios/skus", sku_payload)

        # 2) preço — serviço separado. ⚠️ path/forma a confirmar no 1º teste.
        preco = None
        try:
            preco = float(peca.get("preco") or peca.get("price") or 0)
        except Exception:
            preco = 0
        if preco and preco > 0:
            _chamar("preco", "POST", "seller/v1/portfolios/prices", {
                "sku": sku_code, "price": preco, "list_price": preco,
            })

        # 3) estoque — serviço separado. ⚠️ path/forma a confirmar.
        try:
            qtd = int(float(peca.get("estoque") or peca.get("qtd") or 0))
        except Exception:
            qtd = 0
        _chamar("estoque", "POST", "seller/v1/portfolios/stocks", {
            "sku": sku_code, "quantity": qtd,
        })

        # 4) compatibilidade veicular (opcional) — só se a peça tiver e o passo SKU ok.
        compat = peca.get("compatibilidade") or peca.get("compat") or []
        if compat and ok_sku:
            _chamar("compatibilidade", "POST",
                    f"seller/v1/portfolios/skus/{sku_code}/vehicles-compatibility",
                    {"vehicles": compat})

        resultado["ok"] = all(v.get("ok") for v in resultado["passos"].values())
        return jsonify(resultado), (200 if resultado["ok"] else 207)

    @app.route("/integracoes/magalu/retoken", methods=["GET", "OPTIONS"])
    def magalu_retoken():
        """DEBUG: refaz o token via refresh_token pedindo uma audience específica
        (?audience=https://api.magalu.com). Salva e retorna o aud do novo token.
        Testa a hipótese de que a API exige aud != 'public'. ?save=0 não persiste."""
        if request.method == "OPTIONS":
            return _options_resp()
        tok = _magalu_token_load()
        if not tok.get("refresh_token"):
            return jsonify({"ok": False, "erro": "sem refresh_token"}), 401
        audience = (request.args.get("audience") or "").strip()
        data = {
            "grant_type": "refresh_token",
            "client_id": MAGALU_CLIENT_ID,
            "client_secret": MAGALU_CLIENT_SECRET,
            "refresh_token": tok.get("refresh_token", ""),
        }
        if audience:
            data["audience"] = audience
        try:
            _r = requests.post(f"{MAGALU_ID_BASE}/oauth/token",
                               headers={"Content-Type": "application/x-www-form-urlencoded"},
                               data=data, timeout=15)
            if _r.status_code != 200:
                return jsonify({"ok": False, "status": _r.status_code, "body": _r.text[:400]}), 400
            _d = _r.json()
            new_tok = {
                "access_token": _d.get("access_token", ""),
                "refresh_token": _d.get("refresh_token", tok.get("refresh_token", "")),
                "expires_at": time.time() + _d.get("expires_in", 3600),
                "scope": _d.get("scope", tok.get("scope", "")),
            }
            # decodifica aud do novo token
            aud = None
            try:
                _p = new_tok["access_token"].split(".")
                _pad = _p[1] + "=" * (-len(_p[1]) % 4)
                aud = json.loads(_base64.urlsafe_b64decode(_pad).decode("utf-8", "ignore")).get("aud")
            except Exception:
                pass
            if request.args.get("save") != "0":
                _magalu_token_save(new_tok)
            return jsonify({"ok": True, "audiencePedida": audience, "audNoToken": aud, "salvo": request.args.get("save") != "0"})
        except Exception as _e:
            return jsonify({"ok": False, "erro": str(_e)}), 500

    @app.route("/integracoes/magalu/whoami", methods=["GET", "OPTIONS"])
    def magalu_whoami():
        """Decodifica as claims do JWT access_token (parte do meio, sem validar
        assinatura) pra achar o tenant id / dados do seller. Read-only debug."""
        if request.method == "OPTIONS":
            return _options_resp()
        token = _magalu_access_token()
        if not token:
            return jsonify({"ok": False, "erro": "Magalu nao autorizada"}), 401
        claims = {}
        try:
            _parts = token.split(".")
            if len(_parts) >= 2:
                _pad = _parts[1] + "=" * (-len(_parts[1]) % 4)
                claims = json.loads(_base64.urlsafe_b64decode(_pad).decode("utf-8", "ignore"))
        except Exception as _e:
            return jsonify({"ok": False, "erro": f"falha ao decodificar JWT: {_e}"}), 500
        # devolve só chaves que possam conter tenant/seller (evita despejar o token todo)
        return jsonify({"ok": True, "claims": claims})

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
            "config": {
                "noweb": {
                    "store": {
                        "enabled": True,
                        "full_sync": True,
                    },
                },
                "webhooks": [{
                    "url": WAHA_WEBHOOK_URL,
                    "events": ["message"],
                    "retries": {
                        "policy": "linear",
                        "delaySeconds": 2,
                        "attempts": 3,
                    },
                }],
            },
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
    _pedido_entrada_lock = _threading.Lock()
    _pedido_itens_lock = _threading.Lock()
    _respostas_func_lock = _threading.Lock()
    _pedidos_manha_lock = _threading.Lock()

    def _normalizar_fone_pedido(valor):
        numero = "".join(ch for ch in str(valor or "") if ch.isdigit())
        if len(numero) in (10, 11):
            numero = "55" + numero
        return numero

    def _identificar_funcionario_pedido(phone):
        numero = _normalizar_fone_pedido(phone)
        for nome, whatsapp in FUNCS_PEDIDO.items():
            if numero == _normalizar_fone_pedido(whatsapp):
                return {"nome": nome.title(), "whatsapp": numero, "origem": "config"}
        try:
            consulta = requests.get(
                f"{_WRX_SB_URL}/rest/v1/funcionarios",
                params={
                    "select": "id,nome,whatsapp",
                    "whatsapp": f"eq.{numero}",
                    "limit": "1",
                },
                headers=_wrx_headers(),
                timeout=12,
            )
            rows = consulta.json() if consulta.status_code == 200 else []
            if isinstance(rows, list) and rows:
                return {
                    "id": rows[0].get("id"),
                    "nome": rows[0].get("nome") or numero,
                    "whatsapp": numero,
                    "origem": "funcionarios",
                }
        except Exception:
            pass
        return None

    def _carregar_itens_pedido(pedido):
        pedido_id = str(pedido.get("id") or "")
        itens_file = os.path.join(_INTEG_DIR, "pedido_itens.json")
        try:
            with open(itens_file, encoding="utf-8") as arquivo:
                todos = json.load(arquivo)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            todos = {}
        itens = todos.get(pedido_id)
        if not isinstance(itens, list) or not itens:
            itens = [{
                "id": f"{pedido_id}-1",
                "pedido_id": pedido_id,
                "peca": pedido.get("peca") or "",
                "veiculo": pedido.get("veiculo") or "",
                "ano": pedido.get("ano") or "",
                "lado": pedido.get("lado") or "",
                "status": "aguardando_busca",
                "origem": "pedido_principal",
                "criado_em": pedido.get("criado_em"),
            }]
            todos[pedido_id] = itens
        return itens_file, todos, itens

    @app.route("/integracoes/marcelo/pedido-item", methods=["POST", "OPTIONS"])
    def marcelo_adicionar_item_pedido():
        """Adiciona outra peca ao mesmo pedido sem criar pedido duplicado."""
        if request.method == "OPTIONS":
            return _options_resp()

        dados = request.get_json(silent=True) or {}
        pedido_id = str(dados.get("pedido_id") or "").strip()
        peca = str(dados.get("peca") or "").strip()
        if not pedido_id or not peca:
            return jsonify({"ok": False, "erro": "pedido_id e peca sao obrigatorios"}), 400

        try:
            consulta = requests.get(
                f"{_WRX_SB_URL}/rest/v1/pedidos",
                params={
                    "select": "id,phone,nome,peca,veiculo,ano,lado,status,criado_em",
                    "id": f"eq.{pedido_id}",
                    "limit": "1",
                },
                headers=_wrx_headers(),
                timeout=15,
            )
            pedidos = consulta.json() if consulta.status_code == 200 else []
        except Exception as e:
            return jsonify({"ok": False, "erro": f"falha ao consultar pedido: {e}"}), 502
        if not isinstance(pedidos, list) or not pedidos:
            return jsonify({"ok": False, "erro": "pedido nao encontrado"}), 404

        pedido = pedidos[0]
        if pedido.get("status") in ("pago", "concluido", "cancelado"):
            return jsonify({"ok": False, "erro": "pedido ja encerrado"}), 409

        novo = {
            "peca": peca,
            "veiculo": str(dados.get("veiculo") or pedido.get("veiculo") or "").strip(),
            "ano": str(dados.get("ano") or pedido.get("ano") or "").strip(),
            "lado": str(dados.get("lado") or "").strip(),
        }
        chave_nova = "|".join(
            _texto_busca_estoque(novo[campo])
            for campo in ("peca", "veiculo", "ano", "lado")
        )

        with _pedido_itens_lock:
            itens_file, todos, itens = _carregar_itens_pedido(pedido)
            for item in itens:
                chave_item = "|".join(
                    _texto_busca_estoque(item.get(campo))
                    for campo in ("peca", "veiculo", "ano", "lado")
                )
                if chave_item == chave_nova:
                    return jsonify({
                        "ok": True,
                        "criado": False,
                        "duplicado": True,
                        "pedido": pedido,
                        "item": item,
                        "itens": itens,
                    })

            item = {
                "id": f"{pedido_id}-{len(itens) + 1}",
                "pedido_id": pedido_id,
                **novo,
                "status": "aguardando_busca",
                "origem": str(dados.get("origem") or "manual").strip(),
                "criado_em": _datetime.now().astimezone().isoformat(),
            }
            itens.append(item)
            todos[pedido_id] = itens
            try:
                _pedidos_estado_save(itens_file, todos)
            except Exception as e:
                return jsonify({"ok": False, "erro": f"falha ao salvar item: {e}"}), 500

        return jsonify({
            "ok": True,
            "criado": True,
            "duplicado": False,
            "pedido": pedido,
            "item": item,
            "itens": itens,
        }), 201

    @app.route("/integracoes/marcelo/pedidos-facebook", methods=["GET", "OPTIONS"])
    def marcelo_pedidos_facebook():
        # Detecta quais pedidos vieram do Facebook lendo o WhatsApp (WAHA): a 1ª
        # mensagem do cliente carrega o marcador [FACEBOOK] (vem do link wa.me da
        # página de divulgação). NÃO toca no bot n8n. Resultado (telefones) fica em
        # cache durável no dx_config (chave pedidos_facebook). Read-only no pedido.
        if request.method == "OPTIONS":
            return _options_resp()
        fb = set()
        try:
            _rc = requests.get(f"{_WRX_SB_URL}/rest/v1/dx_config",
                               params={"chave": "eq.pedidos_facebook", "select": "valor"},
                               headers=_wrx_headers(), timeout=12)
            if _rc.status_code == 200 and _rc.json():
                v = _rc.json()[0].get("valor")
                if isinstance(v, list):
                    fb = set(str(x) for x in v)
        except Exception:
            pass
        if request.args.get("so_cache") in ("1", "true"):
            return jsonify({"ok": True, "fones": sorted(fb), "checados": 0, "cache": True})
        # telefones de pedidos recentes ainda não classificados
        try:
            peds = requests.get(f"{_WRX_SB_URL}/rest/v1/pedidos",
                                params={"select": "phone,criado_em", "order": "criado_em.desc", "limit": "200"},
                                headers=_wrx_headers(), timeout=15).json()
        except Exception:
            peds = []
        fones = []
        for p in (peds if isinstance(peds, list) else []):
            f = _normalizar_fone_pedido(p.get("phone"))
            if f and f not in fb and f not in fones:
                fones.append(f)
        checados = 0
        for f in fones[:40]:   # limita p/ não travar (cache cobre o resto nas próximas cargas)
            checados += 1
            try:
                rr = requests.get(f"{WAHA_BASE}/api/{WAHA_SESSION}/chats/{f}@c.us/messages",
                                  params={"limit": "60"}, headers=_waha_h({"Accept": "application/json"}), timeout=15)
                msgs = rr.json() if rr.status_code == 200 else []
                if isinstance(msgs, dict):
                    msgs = msgs.get("data") or msgs.get("messages") or []
                for m in (msgs if isinstance(msgs, list) else []):
                    if not isinstance(m, dict) or m.get("fromMe") is True:
                        continue
                    txt = str(m.get("body") or m.get("text") or m.get("caption") or "").upper()
                    if "[FACEBOOK]" in txt or "VIM PELO FACEBOOK" in txt:
                        fb.add(f)
                        break
            except Exception:
                continue
        try:
            requests.post(f"{_WRX_SB_URL}/rest/v1/dx_config",
                          headers={**_wrx_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
                          json={"chave": "pedidos_facebook", "valor": sorted(fb)}, timeout=12)
        except Exception as _e:
            print(f"[FB-PEDIDOS] falha ao gravar cache: {_e}")
        return jsonify({"ok": True, "fones": sorted(fb), "checados": checados, "cache": False})

    @app.route("/integracoes/marcelo/respostas-grupo", methods=["GET", "OPTIONS"])
    def marcelo_respostas_grupo():
        # Identifica quem RESPONDEU um pedido nos grupos: lê o WhatsApp procurando
        # mensagens (de terceiros) que citam o #codigo do pedido (que sai no disparo
        # "PROCURO ... #512"). Retorna por pedido. Cache em dx_config:respostas_grupo.
        if request.method == "OPTIONS":
            return _options_resp()
        # mapa codigo(numerico) -> {pedido_id, fone_cliente}
        try:
            peds = requests.get(f"{_WRX_SB_URL}/rest/v1/pedidos",
                                params={"select": "id,phone,peca", "order": "criado_em.desc", "limit": "300"},
                                headers=_wrx_headers(), timeout=15).json()
        except Exception:
            peds = []
        cod_map = {}
        for p in (peds if isinstance(peds, list) else []):
            raw = str(p.get("id") or "")[-5:]
            if raw.isdigit():
                cod = str(int(raw))   # tira zeros à esquerda
                cod_map[cod] = {"id": p.get("id"), "cliente": _normalizar_fone_pedido(p.get("phone")), "peca": p.get("peca")}
        if not cod_map:
            return jsonify({"ok": True, "respostas": {}, "checados": 0})
        # chats recentes (grupos + privados)
        try:
            chats = requests.get(f"{WAHA_BASE}/api/{WAHA_SESSION}/chats",
                                 params={"limit": "60"}, headers=_waha_h({"Accept": "application/json"}), timeout=20).json()
        except Exception:
            chats = []
        if isinstance(chats, dict):
            chats = chats.get("data") or chats.get("chats") or []
        pat = re.compile(r"#\s*(\d{2,5})")
        respostas = {}   # pedido_id -> [ {de, nome, texto, hora, grupo} ]
        checados = 0
        for ch in (chats if isinstance(chats, list) else [])[:50]:
            chid = ch.get("id") if isinstance(ch, dict) else ch
            chid = chid.get("_serialized") if isinstance(chid, dict) else chid
            if not chid or chid == "status@broadcast":
                continue
            checados += 1
            ehgrupo = str(chid).endswith("@g.us")
            try:
                rr = requests.get(f"{WAHA_BASE}/api/{WAHA_SESSION}/chats/{chid}/messages",
                                  params={"limit": "40"}, headers=_waha_h({"Accept": "application/json"}), timeout=15)
                msgs = rr.json() if rr.status_code == 200 else []
            except Exception:
                continue
            if isinstance(msgs, dict):
                msgs = msgs.get("data") or msgs.get("messages") or []
            for m in (msgs if isinstance(msgs, list) else []):
                if not isinstance(m, dict) or m.get("fromMe") is True:
                    continue
                txt = str(m.get("body") or m.get("text") or m.get("caption") or "")
                if "#" not in txt:
                    continue
                for cod in pat.findall(txt):
                    cod = str(int(cod)) if cod.isdigit() else cod
                    alvo = cod_map.get(cod)
                    if not alvo:
                        continue
                    de = str(m.get("from") or m.get("author") or chid).split("@")[0]
                    # ignora a própria mensagem do cliente do pedido
                    if alvo.get("cliente") and de.endswith(alvo["cliente"][-8:]):
                        continue
                    pid = alvo["id"]
                    respostas.setdefault(pid, [])
                    if len(respostas[pid]) < 12 and not any(r["texto"] == txt[:300] for r in respostas[pid]):
                        respostas[pid].append({
                            "de": de,
                            "nome": str(m.get("notifyName") or m.get("pushName") or ""),
                            "texto": txt[:300],
                            "hora": m.get("timestamp"),
                            "grupo": ehgrupo,
                        })
        try:
            requests.post(f"{_WRX_SB_URL}/rest/v1/dx_config",
                          headers={**_wrx_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
                          json={"chave": "respostas_grupo", "valor": respostas}, timeout=12)
        except Exception:
            pass
        return jsonify({"ok": True, "respostas": respostas, "checados": checados,
                        "totalPedidosComResposta": len(respostas)})

    @app.route("/integracoes/marcelo/pedido-itens/<pedido_id>", methods=["GET", "OPTIONS"])
    def marcelo_listar_itens_pedido(pedido_id):
        if request.method == "OPTIONS":
            return _options_resp()
        try:
            consulta = requests.get(
                f"{_WRX_SB_URL}/rest/v1/pedidos",
                params={
                    "select": "id,peca,veiculo,ano,lado,status,criado_em",
                    "id": f"eq.{pedido_id}",
                    "limit": "1",
                },
                headers=_wrx_headers(),
                timeout=15,
            )
            pedidos = consulta.json() if consulta.status_code == 200 else []
        except Exception as e:
            return jsonify({"ok": False, "erro": f"falha ao consultar pedido: {e}"}), 502
        if not isinstance(pedidos, list) or not pedidos:
            return jsonify({"ok": False, "erro": "pedido nao encontrado"}), 404
        with _pedido_itens_lock:
            itens_file, todos, itens = _carregar_itens_pedido(pedidos[0])
            alterado = False
            for item in itens:
                if item.get("sku") or item.get("status") not in (
                    "aguardando_busca",
                    "produto_nao_cadastrado",
                    "aguardando_confirmacao_fisica",
                ):
                    continue
                busca = _buscar_estoque_dados(
                    item.get("peca"),
                    item.get("veiculo"),
                    item.get("ano"),
                    item.get("lado"),
                )
                item["candidatos"] = busca.get("candidatos") or []
                item["status"] = busca.get("status_sugerido") or "produto_nao_cadastrado"
                if item["candidatos"]:
                    item["sku_sugerido"] = item["candidatos"][0].get("sku")
                alterado = True
            if alterado:
                todos[str(pedido_id)] = itens
                _pedidos_estado_save(itens_file, todos)
        return jsonify({
            "ok": True,
            "pedido_id": str(pedido_id),
            "total": len(itens),
            "itens": itens,
        })

    def _texto_busca_estoque(valor):
        import unicodedata
        texto = unicodedata.normalize("NFKD", str(valor or "").lower())
        texto = "".join(ch for ch in texto if not unicodedata.combining(ch))
        return " ".join(re.findall(r"[a-z0-9]+", texto))

    def _termos_busca_estoque(valor):
        ignorar = {
            "a", "as", "com", "da", "das", "de", "do", "dos", "e", "em",
            "para", "por", "um", "uma", "peca", "preciso", "quero",
        }
        return [
            termo for termo in _texto_busca_estoque(valor).split()
            if len(termo) >= 2 and termo not in ignorar
        ]

    def _interpretar_resposta_funcionario(dados):
        texto = _texto_busca_estoque(
            dados.get("mensagem") or dados.get("resposta") or ""
        )
        acao_informada = _texto_busca_estoque(dados.get("acao") or "")
        if acao_informada in ("nao tenho", "naotenho"):
            acao = "nao_tenho"
        elif acao_informada == "tenho":
            acao = "tenho"
        elif re.search(r"\bnao\s+tenho\b", texto):
            acao = "nao_tenho"
        elif re.search(r"\btenho\b", texto):
            acao = "tenho"
        else:
            acao = ""

        pedido_id = str(dados.get("pedido_id") or "").strip()
        item_id = str(dados.get("item_id") or "").strip()
        if not pedido_id:
            origem = str(dados.get("mensagem") or dados.get("resposta") or "")
            encontrado = re.search(
                r"(?:#\s*|c[oó]d(?:igo)?\.?\s*:?\s*)?(\d+)-(\d+)\b",
                origem,
                flags=re.I,
            )
            if not encontrado:
                encontrado = re.search(
                    r"(?:#\s*|c[oó]d(?:igo)?\.?\s*:?\s*)"
                    r"(\d+)(?:-(\d+))?\b",
                    origem,
                    flags=re.I,
                )
            pedido_id = encontrado.group(1) if encontrado else ""
            if encontrado and encontrado.group(2):
                item_id = f"{pedido_id}-{encontrado.group(2)}"
        if pedido_id and not item_id:
            item_id = f"{pedido_id}-1"
        return acao, pedido_id, item_id

    @app.route("/integracoes/marcelo/resposta-funcionario", methods=["POST", "OPTIONS"])
    def marcelo_resposta_funcionario():
        """Registra Tenho/Nao tenho sem enviar qualquer mensagem ao cliente."""
        if request.method == "OPTIONS":
            return _options_resp()

        dados = request.get_json(silent=True) or {}
        funcionario = _identificar_funcionario_pedido(
            dados.get("phone") or dados.get("telefone")
        )
        if not funcionario:
            return jsonify({"ok": False, "erro": "funcionario nao identificado"}), 403

        acao, pedido_id, item_id = _interpretar_resposta_funcionario(dados)
        if acao not in ("tenho", "nao_tenho") or not pedido_id.isdigit():
            return jsonify({
                "ok": False,
                "erro": "use Tenho #PEDIDO ou Nao tenho #PEDIDO",
            }), 400

        try:
            consulta = requests.get(
                f"{_WRX_SB_URL}/rest/v1/pedidos",
                params={
                    "select": "id,phone,nome,peca,veiculo,ano,lado,status",
                    "id": f"eq.{pedido_id}",
                    "limit": "1",
                },
                headers=_wrx_headers(),
                timeout=15,
            )
            pedidos = consulta.json() if consulta.status_code == 200 else []
        except Exception as e:
            return jsonify({"ok": False, "erro": f"falha ao consultar pedido: {e}"}), 502
        if not isinstance(pedidos, list) or not pedidos:
            return jsonify({"ok": False, "erro": "pedido nao encontrado"}), 404

        pedido = pedidos[0]
        if pedido.get("status") in ("pago", "concluido", "cancelado"):
            return jsonify({"ok": False, "erro": "pedido ja encerrado"}), 409
        with _pedido_itens_lock:
            _, _, itens = _carregar_itens_pedido(pedido)
        item = next((linha for linha in itens if linha.get("id") == item_id), None)
        if not item:
            return jsonify({"ok": False, "erro": "item do pedido nao encontrado"}), 404

        respostas_file = os.path.join(_INTEG_DIR, "respostas_func.json")
        chave = f"{item_id}:{funcionario['whatsapp']}:{acao}"
        with _respostas_func_lock:
            try:
                with open(respostas_file, encoding="utf-8") as arquivo:
                    respostas = json.load(arquivo)
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                respostas = {}
            if chave in respostas:
                return jsonify({
                    "ok": True,
                    "duplicado": True,
                    "evento": respostas[chave],
                    "pedido": pedido,
                })

            novo_status = "atendimento" if acao == "tenho" else "verificando"
            try:
                alteracao = requests.patch(
                    f"{_WRX_SB_URL}/rest/v1/pedidos",
                    params={"id": f"eq.{pedido_id}"},
                    headers={**_wrx_headers(), "Prefer": "return=representation"},
                    json={"status": novo_status},
                    timeout=15,
                )
                if alteracao.status_code not in (200, 204):
                    return jsonify({
                        "ok": False,
                        "erro": "falha ao atualizar status do pedido",
                        "http": alteracao.status_code,
                    }), 502
            except Exception as e:
                return jsonify({"ok": False, "erro": f"falha ao atualizar pedido: {e}"}), 502

            evento = {
                "pedido_id": pedido_id,
                "item_id": item_id,
                "peca": item.get("peca"),
                "acao": acao,
                "status": novo_status,
                "funcionario": funcionario["nome"],
                "whatsapp": funcionario["whatsapp"],
                "recebido_em": _datetime.now().astimezone().isoformat(),
            }
            respostas[chave] = evento
            try:
                _pedidos_estado_save(respostas_file, respostas)
            except Exception as e:
                return jsonify({
                    "ok": False,
                    "erro": f"status atualizado, mas resposta nao foi salva: {e}",
                }), 500

        pedido["status"] = novo_status
        busca = None
        if acao == "tenho":
            try:
                busca = _buscar_estoque_dados(
                    item.get("peca"),
                    item.get("veiculo"),
                    item.get("ano"),
                    item.get("lado"),
                )
                with _pedido_itens_lock:
                    itens_file, todos, itens = _carregar_itens_pedido(pedido)
                    item_salvo = next(
                        (linha for linha in itens if linha.get("id") == item_id),
                        None,
                    )
                    if item_salvo:
                        item_salvo["candidatos"] = busca.get("candidatos") or []
                        item_salvo["status"] = (
                            busca.get("status_sugerido")
                            or "produto_nao_cadastrado"
                        )
                        item_salvo["responsavel"] = funcionario["nome"]
                        item_salvo["resposta_funcionario"] = {
                            "nome": funcionario["nome"],
                            "whatsapp": funcionario["whatsapp"],
                            "acao": acao,
                            "recebido_em": evento["recebido_em"],
                        }
                        if item_salvo["candidatos"]:
                            item_salvo["sku_sugerido"] = (
                                item_salvo["candidatos"][0].get("sku")
                            )
                        todos[pedido_id] = itens
                        _pedidos_estado_save(itens_file, todos)
                        item = item_salvo
            except Exception as e:
                print(f"[RESPOSTA-FUNC] falha na busca automatica item={item_id}: {e}")
        return jsonify({
            "ok": True,
            "duplicado": False,
            "evento": evento,
            "pedido": pedido,
            "item": item,
            "busca_estoque": busca,
            "envio_cliente": False,
        })

    def _evento_tenho_item(item_id, whatsapp):
        respostas_file = os.path.join(_INTEG_DIR, "respostas_func.json")
        try:
            with open(respostas_file, encoding="utf-8") as arquivo:
                respostas = json.load(arquivo)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            respostas = {}
        return respostas.get(
            f"{item_id}:{_normalizar_fone_pedido(whatsapp)}:tenho"
        )

    def _ultimo_evento_tenho_funcionario(whatsapp):
        respostas_file = os.path.join(_INTEG_DIR, "respostas_func.json")
        try:
            with open(respostas_file, encoding="utf-8") as arquivo:
                respostas = json.load(arquivo)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            respostas = {}
        numero = _normalizar_fone_pedido(whatsapp)
        eventos = [
            evento for evento in respostas.values()
            if evento.get("acao") == "tenho"
            and _normalizar_fone_pedido(evento.get("whatsapp")) == numero
        ]
        eventos.sort(key=lambda evento: str(evento.get("recebido_em") or ""), reverse=True)
        return eventos[0] if eventos else None

    @app.route("/integracoes/marcelo/confirmar-estoque-item", methods=["POST", "OPTIONS"])
    def marcelo_confirmar_estoque_item():
        """Confirma fisicamente um SKU e o vincula ao item do pedido."""
        if request.method == "OPTIONS":
            return _options_resp()
        dados = request.get_json(silent=True) or {}
        item_id = str(dados.get("item_id") or "").strip()
        sku = str(dados.get("sku") or "").strip()
        funcionario = _identificar_funcionario_pedido(
            dados.get("phone") or dados.get("telefone")
        )
        if not funcionario:
            return jsonify({"ok": False, "erro": "funcionario nao identificado"}), 403
        if not re.fullmatch(r"\d+-\d+", item_id) or not sku:
            return jsonify({"ok": False, "erro": "item_id e sku sao obrigatorios"}), 400
        pedido_id = item_id.split("-", 1)[0]

        try:
            pedido_req = requests.get(
                f"{_WRX_SB_URL}/rest/v1/pedidos",
                params={
                    "select": "id,peca,veiculo,ano,lado,status,criado_em",
                    "id": f"eq.{pedido_id}",
                    "limit": "1",
                },
                headers=_wrx_headers(),
                timeout=15,
            )
            produto_req = requests.get(
                f"{_WRX_SB_URL}/rest/v1/pecas_estoque",
                params={
                    "select": (
                        "sku,titulo,descricao,categoria,marca,modelo,ano,lado,"
                        "compatibilidade,preco,qtd,fotos,loc"
                    ),
                    "sku": f"eq.{sku}",
                    "limit": "1",
                },
                headers=_wrx_headers(),
                timeout=15,
            )
            pedidos = pedido_req.json() if pedido_req.status_code == 200 else []
            produtos = produto_req.json() if produto_req.status_code == 200 else []
        except Exception as e:
            return jsonify({"ok": False, "erro": f"falha na conferencia: {e}"}), 502
        if not isinstance(pedidos, list) or not pedidos:
            return jsonify({"ok": False, "erro": "pedido nao encontrado"}), 404
        if not isinstance(produtos, list) or not produtos:
            return jsonify({"ok": False, "erro": "sku nao encontrado"}), 404
        produto = produtos[0]
        if float(produto.get("qtd") or 0) <= 0:
            return jsonify({"ok": False, "erro": "produto sem estoque"}), 409

        with _pedido_itens_lock:
            itens_file, todos, itens = _carregar_itens_pedido(pedidos[0])
            item = next((linha for linha in itens if linha.get("id") == item_id), None)
            if not item:
                return jsonify({"ok": False, "erro": "item nao encontrado"}), 404
            pontos = _pontuar_produto_pedido(
                produto,
                item.get("peca"),
                item.get("veiculo"),
                item.get("ano"),
                item.get("lado"),
                exigir_ano=True,
            )
            if pontos < 55:
                busca = _buscar_estoque_dados(
                    item.get("peca"),
                    item.get("veiculo"),
                    item.get("ano"),
                    item.get("lado"),
                )
                sugestao = next(
                    (
                        candidato for candidato in busca.get("candidatos", [])
                        if candidato.get("ano_compativel") is not False
                    ),
                    None,
                )
                return jsonify({
                    "ok": False,
                    "erro": "sku nao corresponde ao item solicitado",
                    "pontuacao": pontos,
                    "sku_sugerido": sugestao.get("sku") if sugestao else None,
                    "titulo_sugerido": sugestao.get("titulo") if sugestao else None,
                }), 409
            item["sku"] = sku
            item["status"] = "estoque_confirmado"
            item["responsavel"] = funcionario["nome"]
            item["fotos"] = produto.get("fotos") or []
            item["preco"] = produto.get("preco")
            item["estoque_confirmado_em"] = _datetime.now().astimezone().isoformat()
            todos[pedido_id] = itens
            try:
                atualizacao = requests.patch(
                    f"{_WRX_SB_URL}/rest/v1/pecas_estoque",
                    params={"sku": f"eq.{sku}"},
                    headers={**_wrx_headers(), "Prefer": "return=minimal"},
                    json={"atualizado": _datetime.now().astimezone().isoformat()},
                    timeout=15,
                )
                if atualizacao.status_code not in (200, 204):
                    return jsonify({"ok": False, "erro": "falha ao atualizar confirmacao"}), 502
                _pedidos_estado_save(itens_file, todos)
            except Exception as e:
                return jsonify({"ok": False, "erro": f"falha ao salvar confirmacao: {e}"}), 502

        return jsonify({
            "ok": True,
            "item": item,
            "produto": produto,
            "envio_cliente": False,
        })

    @app.route("/integracoes/marcelo/pedido-item-foto", methods=["POST", "OPTIONS"])
    def marcelo_vincular_foto_item():
        """Vincula foto ao item somente depois de uma resposta Tenho."""
        if request.method == "OPTIONS":
            return _options_resp()

        dados = request.get_json(silent=True) or {}
        item_id = str(dados.get("item_id") or "").strip()
        foto = str(dados.get("foto") or dados.get("media_url") or "").strip()
        funcionario = _identificar_funcionario_pedido(
            dados.get("phone") or dados.get("telefone")
        )
        if not funcionario:
            return jsonify({"ok": False, "erro": "funcionario nao identificado"}), 403
        if not re.fullmatch(r"\d+-\d+", item_id) or not foto:
            return jsonify({"ok": False, "erro": "item_id e foto sao obrigatorios"}), 400
        if not foto.startswith(("http://", "https://", "data:image/")):
            return jsonify({"ok": False, "erro": "formato de foto invalido"}), 400

        pedido_id = item_id.split("-", 1)[0]
        if not _evento_tenho_item(item_id, funcionario["whatsapp"]):
            return jsonify({
                "ok": False,
                "erro": "o funcionario precisa responder Tenho antes de enviar a foto",
            }), 409

        try:
            consulta = requests.get(
                f"{_WRX_SB_URL}/rest/v1/pedidos",
                params={
                    "select": "id,peca,veiculo,ano,lado,status,criado_em",
                    "id": f"eq.{pedido_id}",
                    "limit": "1",
                },
                headers=_wrx_headers(),
                timeout=15,
            )
            pedidos = consulta.json() if consulta.status_code == 200 else []
        except Exception as e:
            return jsonify({"ok": False, "erro": f"falha ao consultar pedido: {e}"}), 502
        if not isinstance(pedidos, list) or not pedidos:
            return jsonify({"ok": False, "erro": "pedido nao encontrado"}), 404

        with _pedido_itens_lock:
            itens_file, todos, itens = _carregar_itens_pedido(pedidos[0])
            item = next((linha for linha in itens if linha.get("id") == item_id), None)
            if not item:
                return jsonify({"ok": False, "erro": "item nao encontrado"}), 404
            cadastro_automatico_permitido = (
                item.get("status") == "produto_nao_cadastrado"
                and not item.get("candidatos")
                and not item.get("sku")
            )
            fotos = (
                item.get("fotos_funcionario")
                if isinstance(item.get("fotos_funcionario"), list)
                else []
            )
            duplicado = foto in fotos
            if not duplicado:
                fotos.append(foto)
                item["fotos_funcionario"] = fotos[:8]
                # Mantido para compatibilidade com os fluxos antigos de cadastro.
                item["fotos"] = fotos[:8]
                if not item.get("candidatos"):
                    item["status"] = "foto_recebida"
                item["responsavel"] = funcionario["nome"]
                item["foto_recebida_em"] = _datetime.now().astimezone().isoformat()
                item["resposta_funcionario"] = {
                    "nome": funcionario["nome"],
                    "whatsapp": funcionario["whatsapp"],
                    "acao": "tenho",
                    "recebido_em": item["foto_recebida_em"],
                }
                todos[pedido_id] = itens
                try:
                    _pedidos_estado_save(itens_file, todos)
                except Exception as e:
                    return jsonify({"ok": False, "erro": f"falha ao salvar foto: {e}"}), 500

        cadastro_automatico = None
        if not duplicado and cadastro_automatico_permitido:
            cadastro_automatico, cadastro_http = _cadastrar_produto_encontrado_dados(
                item_id,
                funcionario,
                {"preco": 0, "cond": "Usada", "loc": ""},
            )
            if cadastro_http in (200, 201):
                item = cadastro_automatico.get("item") or item

        return jsonify({
            "ok": True,
            "duplicado": duplicado,
            "pedido_id": pedido_id,
            "item": item,
            "cadastro_automatico": cadastro_automatico,
            "envio_cliente": False,
        })

    def _cadastrar_produto_encontrado_dados(item_id, funcionario, dados):
        pedido_id = item_id.split("-", 1)[0]
        if not _evento_tenho_item(item_id, funcionario["whatsapp"]):
            return {"ok": False, "erro": "resposta Tenho nao encontrada"}, 409

        try:
            consulta = requests.get(
                f"{_WRX_SB_URL}/rest/v1/pedidos",
                params={
                    "select": "id,peca,veiculo,ano,lado,status,criado_em",
                    "id": f"eq.{pedido_id}",
                    "limit": "1",
                },
                headers=_wrx_headers(),
                timeout=15,
            )
            pedidos = consulta.json() if consulta.status_code == 200 else []
        except Exception as e:
            return {"ok": False, "erro": f"falha ao consultar pedido: {e}"}, 502
        if not isinstance(pedidos, list) or not pedidos:
            return {"ok": False, "erro": "pedido nao encontrado"}, 404

        with _pedido_itens_lock:
            itens_file, todos, itens = _carregar_itens_pedido(pedidos[0])
            item = next((linha for linha in itens if linha.get("id") == item_id), None)
            if not item:
                return {"ok": False, "erro": "item nao encontrado"}, 404
            if item.get("sku"):
                return {
                    "ok": True,
                    "criado": False,
                    "duplicado": True,
                    "sku": item["sku"],
                    "item": item,
                }, 200
            if item.get("candidatos"):
                return {
                    "ok": True,
                    "criado": False,
                    "produto_existente": True,
                    "sku_sugerido": item.get("sku_sugerido"),
                    "item": item,
                }, 200
            fotos = (
                item.get("fotos_funcionario")
                if isinstance(item.get("fotos_funcionario"), list)
                else item.get("fotos") if isinstance(item.get("fotos"), list)
                else []
            )
            if not fotos:
                return {"ok": False, "erro": "envie pelo menos uma foto"}, 409

            sku = str(_max_sku_numerico() + 1)
            titulo = " ".join(filter(None, [
                item.get("peca"),
                item.get("veiculo"),
                item.get("ano"),
                item.get("lado"),
            ])).strip()
            try:
                preco = float(str(dados.get("preco") or 0).replace(",", "."))
            except (TypeError, ValueError):
                preco = 0
            produto = {
                "sku": sku,
                "titulo": titulo,
                "modelo": item.get("veiculo") or "",
                "ano": str(item.get("ano") or ""),
                "lado": item.get("lado") or "",
                "preco": preco,
                "qtd": 1,
                "cond": str(dados.get("cond") or "Usada"),
                "loc": str(dados.get("loc") or "").strip(),
                "fotos": fotos[:8],
                "origem": f"pedido #{pedido_id}",
                "cadastrado_por": funcionario["nome"],
                "atualizado": _datetime.now().astimezone().isoformat(),
            }
            try:
                criacao = requests.post(
                    f"{_WRX_SB_URL}/rest/v1/pecas_estoque",
                    headers={**_wrx_headers(), "Prefer": "return=representation"},
                    json=produto,
                    timeout=20,
                )
                if criacao.status_code not in (200, 201):
                    return {
                        "ok": False,
                        "erro": "falha ao cadastrar produto no estoque",
                        "http": criacao.status_code,
                        "detalhe": criacao.text[:300],
                    }, 502
            except Exception as e:
                return {"ok": False, "erro": f"falha ao cadastrar produto: {e}"}, 502

            item["sku"] = sku
            item["status"] = "produto_cadastrado_automaticamente"
            item["responsavel"] = funcionario["nome"]
            item["produto_cadastrado_em"] = _datetime.now().astimezone().isoformat()
            todos[pedido_id] = itens
            try:
                _pedidos_estado_save(itens_file, todos)
            except Exception as e:
                return {
                    "ok": False,
                    "erro": f"produto criado, mas item nao foi atualizado: {e}",
                    "sku": sku,
                }, 500

        return {
            "ok": True,
            "criado": True,
            "sku": sku,
            "produto": produto,
            "item": item,
            "publicado": False,
            "envio_cliente": False,
        }, 201

    @app.route("/integracoes/marcelo/cadastrar-produto-encontrado", methods=["POST", "OPTIONS"])
    def marcelo_cadastrar_produto_encontrado():
        """Cadastra no estoque uma peca encontrada, sem publicar anuncios."""
        if request.method == "OPTIONS":
            return _options_resp()

        dados = request.get_json(silent=True) or {}
        item_id = str(dados.get("item_id") or "").strip()
        funcionario = _identificar_funcionario_pedido(
            dados.get("phone") or dados.get("telefone")
        )
        if not funcionario:
            return jsonify({"ok": False, "erro": "funcionario nao identificado"}), 403
        if not re.fullmatch(r"\d+-\d+", item_id):
            return jsonify({"ok": False, "erro": "item_id invalido"}), 400
        resultado, http = _cadastrar_produto_encontrado_dados(
            item_id, funcionario, dados
        )
        return jsonify(resultado), http

    def _waha_enviar_imagem(numero, url, legenda=""):
        num = "".join(ch for ch in str(numero) if ch.isdigit())
        if not num or not str(url).startswith(("http://", "https://")):
            return False, "numero ou imagem invalida"
        try:
            resposta = requests.post(
                f"{WAHA_BASE}/api/sendImage",
                headers=_waha_h({"Content-Type": "application/json"}),
                json={
                    "session": WAHA_SESSION,
                    "chatId": f"{num}@c.us",
                    "file": {"url": url},
                    "caption": legenda,
                },
                timeout=30,
            )
            return resposta.status_code in (200, 201), resposta.text[:300]
        except Exception as e:
            return False, str(e)

    def _conferencia_item_cliente(pedido, item, produto):
        fotos = produto.get("fotos") or item.get("fotos") or []
        if isinstance(fotos, str):
            try:
                fotos = json.loads(fotos)
            except Exception:
                fotos = [fotos] if fotos else []
        try:
            preco = float(produto.get("preco") or item.get("preco") or 0)
        except (TypeError, ValueError):
            preco = 0
        checks = {
            "produto_localizado": bool(item.get("sku")),
            "estoque_confirmado": item.get("status") in (
                "estoque_confirmado",
                "produto_cadastrado_automaticamente",
            ),
            "fotos_recebidas": bool(fotos),
            "preco_informado": preco > 0,
            "telefone_cliente": bool(_normalizar_fone_pedido(pedido.get("phone"))),
            "quantidade_positiva": float(produto.get("qtd") or 0) > 0,
        }
        return checks, fotos, preco

    @app.route("/integracoes/marcelo/conferencia-final", methods=["POST", "OPTIONS"])
    def marcelo_conferencia_final():
        """Somente confere; nao envia mensagem."""
        if request.method == "OPTIONS":
            return _options_resp()
        dados = request.get_json(silent=True) or {}
        item_id = str(dados.get("item_id") or "").strip()
        if not re.fullmatch(r"\d+-\d+", item_id):
            return jsonify({"ok": False, "erro": "item_id invalido"}), 400
        pedido_id = item_id.split("-", 1)[0]
        try:
            pedido_req = requests.get(
                f"{_WRX_SB_URL}/rest/v1/pedidos",
                params={
                    "select": "id,phone,nome,peca,veiculo,ano,lado,status",
                    "id": f"eq.{pedido_id}",
                    "limit": "1",
                },
                headers=_wrx_headers(),
                timeout=15,
            )
            pedidos = pedido_req.json() if pedido_req.status_code == 200 else []
        except Exception as e:
            return jsonify({"ok": False, "erro": f"falha ao consultar pedido: {e}"}), 502
        if not isinstance(pedidos, list) or not pedidos:
            return jsonify({"ok": False, "erro": "pedido nao encontrado"}), 404
        pedido = pedidos[0]
        with _pedido_itens_lock:
            _, _, itens = _carregar_itens_pedido(pedido)
        item = next((linha for linha in itens if linha.get("id") == item_id), None)
        if not item or not item.get("sku"):
            return jsonify({"ok": False, "erro": "item sem produto vinculado"}), 409
        try:
            produto_req = requests.get(
                f"{_WRX_SB_URL}/rest/v1/pecas_estoque",
                params={"select": "sku,titulo,preco,qtd,fotos", "sku": f"eq.{item['sku']}", "limit": "1"},
                headers=_wrx_headers(),
                timeout=15,
            )
            produtos = produto_req.json() if produto_req.status_code == 200 else []
        except Exception as e:
            return jsonify({"ok": False, "erro": f"falha ao consultar produto: {e}"}), 502
        if not isinstance(produtos, list) or not produtos:
            return jsonify({"ok": False, "erro": "produto nao encontrado"}), 404
        checks, fotos, preco = _conferencia_item_cliente(pedido, item, produtos[0])
        return jsonify({
            "ok": True,
            "pronto": all(checks.values()),
            "checks": checks,
            "pedido": pedido,
            "item": item,
            "produto": produtos[0],
            "fotos": fotos,
            "preco": preco,
            "envio_cliente": False,
        })

    @app.route("/integracoes/marcelo/enviar-oferta-cliente", methods=["POST", "OPTIONS"])
    def marcelo_enviar_oferta_cliente():
        """Envia uma unica oferta depois da confirmacao final explicita."""
        if request.method == "OPTIONS":
            return _options_resp()
        dados = request.get_json(silent=True) or {}
        item_id = str(dados.get("item_id") or "").strip()
        if dados.get("confirmar") is not True:
            return jsonify({"ok": False, "erro": "confirmacao explicita obrigatoria"}), 400
        enviados_file = os.path.join(_INTEG_DIR, "ofertas_cliente_enviadas.json")
        try:
            with open(enviados_file, encoding="utf-8") as arquivo:
                enviados = json.load(arquivo)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            enviados = {}
        if item_id in enviados:
            return jsonify({
                "ok": True,
                "duplicado": True,
                "envio": enviados[item_id],
            })

        with app.test_request_context(
            "/integracoes/marcelo/conferencia-final",
            method="POST",
            json={"item_id": item_id},
        ):
            conferencia_resp = marcelo_conferencia_final()
        if isinstance(conferencia_resp, tuple):
            return conferencia_resp
        conferencia = conferencia_resp.get_json()
        if not conferencia.get("pronto"):
            return jsonify({
                "ok": False,
                "erro": "conferencia final incompleta",
                "checks": conferencia.get("checks"),
            }), 409

        pedido = conferencia["pedido"]
        item = conferencia["item"]
        fotos = conferencia["fotos"]
        preco = conferencia["preco"]

        nome = str(pedido.get("nome") or "").split(" ")[0] or "cliente"
        formas = str(dados.get("formas_pagamento") or "PIX, dinheiro e cartao")
        mensagem = (
            f"Ola, {nome}! Encontramos sua peca.\n\n"
            f"Pedido #{pedido.get('id')}\n"
            f"Peca: {item.get('peca')}\n"
            f"Veiculo: {item.get('veiculo')} {item.get('ano')}\n"
            f"Valor: R$ {preco:.2f}".replace(".", ",")
            + f"\nPagamento: {formas}\n\nDeseja confirmar?"
        )
        telefone = _normalizar_fone_pedido(pedido.get("phone"))
        ok_texto, erro_texto = _waha_enviar(telefone, mensagem)
        if not ok_texto:
            return jsonify({"ok": False, "erro": f"falha ao enviar texto: {erro_texto}"}), 502
        fotos_enviadas = 0
        for foto in fotos[:5]:
            ok_foto, _ = _waha_enviar_imagem(telefone, foto)
            if ok_foto:
                fotos_enviadas += 1
        if fotos and fotos_enviadas == 0:
            return jsonify({
                "ok": False,
                "erro": "texto enviado, mas as fotos falharam",
            }), 502

        try:
            alteracao = requests.patch(
                f"{_WRX_SB_URL}/rest/v1/pedidos",
                params={"id": f"eq.{pedido.get('id')}"},
                headers={**_wrx_headers(), "Prefer": "return=minimal"},
                json={"status": "confirmado", "preco": preco},
                timeout=15,
            )
            if alteracao.status_code not in (200, 204):
                return jsonify({"ok": False, "erro": "oferta enviada, mas status nao foi atualizado"}), 502
        except Exception as e:
            return jsonify({"ok": False, "erro": f"oferta enviada, mas status falhou: {e}"}), 502

        envio = {
            "item_id": item_id,
            "pedido_id": str(pedido.get("id")),
            "telefone": telefone,
            "preco": preco,
            "fotos_enviadas": fotos_enviadas,
            "enviado_em": _datetime.now().astimezone().isoformat(),
        }
        enviados[item_id] = envio
        _pedidos_estado_save(enviados_file, enviados)
        return jsonify({"ok": True, "duplicado": False, "envio": envio})

    def _ano_produto_compativel(produto, ano):
        ano_num = re.search(r"(19|20)\d{2}", str(ano or ""))
        texto_produto = _texto_busca_estoque(" ".join([
            str(produto.get("ano") or ""),
            str(produto.get("titulo") or ""),
            str(produto.get("compatibilidade") or ""),
        ]))
        anos_produto = [
            int(valor)
            for valor in re.findall(r"\b(?:19|20)\d{2}\b", texto_produto)
        ]
        if not ano_num or not anos_produto:
            return None
        ano_pedido = int(ano_num.group(0))
        return min(anos_produto) <= ano_pedido <= max(anos_produto)

    def _pontuar_produto_pedido(
        produto, peca, veiculo="", ano="", lado="", exigir_ano=False
    ):
        from difflib import SequenceMatcher

        def termo_presente(termo, texto):
            if termo.isdigit():
                return termo in texto.split()
            if termo in texto:
                return True
            return any(
                SequenceMatcher(None, termo, token).ratio() >= 0.82
                for token in texto.split()
                if abs(len(token) - len(termo)) <= 2
            )

        titulo = _texto_busca_estoque(" ".join([
            str(produto.get("titulo") or ""),
            str(produto.get("descricao") or ""),
            str(produto.get("categoria") or ""),
            str(produto.get("lado") or ""),
        ]))
        carro = _texto_busca_estoque(" ".join([
            str(produto.get("marca") or ""),
            str(produto.get("modelo") or ""),
            str(produto.get("ano") or ""),
            str(produto.get("compatibilidade") or ""),
        ]))
        termos_peca = _termos_busca_estoque(peca)
        termos_veiculo = [
            termo for termo in _termos_busca_estoque(veiculo)
            if not termo.isdigit()
            and not re.fullmatch(r"(?:19|20)\d{2}", termo)
        ]
        modelos_numericos = re.findall(r"\b\d{3}\b", str(veiculo or ""))
        termos_lado = _termos_busca_estoque(lado)
        termos_principais = [
            termo for termo in termos_peca
            if termo not in {
                "direita", "direito", "esquerda", "esquerdo",
                "dianteira", "dianteiro", "traseira", "traseiro",
            } and not termo.isdigit()
        ]

        encontrados_peca = sum(
            1 for termo in termos_peca if termo_presente(termo, titulo)
        )
        encontrados_principais = sum(
            1 for termo in termos_principais if termo_presente(termo, titulo)
        )
        encontrados_veiculo = sum(
            1 for termo in termos_veiculo
            if termo_presente(termo, carro) or termo_presente(termo, titulo)
        )
        encontrados_lado = sum(
            1 for termo in termos_lado if termo_presente(termo, titulo)
        )
        ano_status = _ano_produto_compativel(produto, ano)
        ano_ok = ano_status is not False

        if not termos_peca or encontrados_peca == 0:
            return 0
        if termos_principais and encontrados_principais / len(termos_principais) < 0.6:
            return 0
        if termos_veiculo and encontrados_veiculo / len(termos_veiculo) < 0.6:
            return 0
        texto_modelo_produto = _texto_busca_estoque(" ".join([
            str(produto.get("modelo") or ""),
            str(produto.get("titulo") or ""),
            str(produto.get("compatibilidade") or ""),
        ]))
        if modelos_numericos and any(
            modelo not in texto_modelo_produto.split()
            for modelo in modelos_numericos
        ):
            return 0
        acessorios_porta = {
            "fechadura", "limitador", "maquina", "mecanismo", "trinco",
            "puxador", "macaneta", "dobradica", "borracha", "vidro",
            "forro", "moldura", "friso",
        }
        pedido_porta_completa = (
            "porta" in termos_principais
            and not any(termo in acessorios_porta for termo in termos_principais)
        )
        if pedido_porta_completa and any(
            termo in titulo.split() for termo in acessorios_porta
        ):
            return 0
        tokens_pedido = set(termos_peca + termos_lado)
        tokens_produto = set(titulo.split())
        pediu_direita = bool(tokens_pedido.intersection({"direita", "direito", "right"}))
        pediu_esquerda = bool(tokens_pedido.intersection({"esquerda", "esquerdo", "left"}))
        produto_direita = bool(tokens_produto.intersection({"direita", "direito", "right"}))
        produto_esquerda = bool(tokens_produto.intersection({"esquerda", "esquerdo", "left"}))
        if (pediu_direita and produto_esquerda) or (
            pediu_esquerda and produto_direita
        ):
            return 0
        if exigir_ano and not ano_ok:
            return 0
        cobertura_peca = encontrados_peca / len(termos_peca)
        cobertura_veiculo = (
            encontrados_veiculo / len(termos_veiculo) if termos_veiculo else 1
        )
        cobertura_lado = encontrados_lado / len(termos_lado) if termos_lado else 1
        return round(
            cobertura_peca * 65
            + cobertura_veiculo * 20
            + cobertura_lado * 10
            + (5 if ano_ok else 0)
        )

    def _buscar_estoque_dados(peca, veiculo="", ano="", lado=""):
        peca = str(peca or "").strip()
        veiculo = str(veiculo or "").strip()
        ano = str(ano or "").strip()
        lado = str(lado or "").strip()
        termos = _termos_busca_estoque(peca)
        if not termos:
            return {"ok": False, "erro": "peca obrigatoria", "candidatos": []}

        # Consulta ampla pelos termos da peca; a classificacao detalhada e feita
        # localmente para considerar modelo, ano e lado sem depender de SQL novo.
        filtros = ",".join(f"titulo.ilike.*{termo}*" for termo in termos[:4])
        try:
            consulta = requests.get(
                f"{_WRX_SB_URL}/rest/v1/pecas_estoque",
                params={
                    "select": (
                        "sku,titulo,descricao,categoria,marca,modelo,ano,lado,"
                        "compatibilidade,preco,qtd,fotos,loc,atualizado,cadastrado_em"
                    ),
                    "qtd": "gt.0",
                    "or": f"({filtros})",
                    "limit": "1000",
                },
                headers=_wrx_headers(),
                timeout=20,
            )
            if consulta.status_code != 200:
                return {
                    "ok": False,
                    "erro": "falha ao consultar estoque",
                    "http": consulta.status_code,
                    "candidatos": [],
                }
            produtos = consulta.json()
        except Exception as e:
            return {
                "ok": False,
                "erro": f"falha ao consultar estoque: {e}",
                "candidatos": [],
            }

        agora = _datetime.now().astimezone()
        candidatos = []
        for produto in produtos if isinstance(produtos, list) else []:
            pontos = _pontuar_produto_pedido(produto, peca, veiculo, ano, lado)
            if pontos < 55:
                continue
            ultima_confirmacao = produto.get("atualizado") or produto.get("cadastrado_em")
            dias_sem_confirmar = None
            if ultima_confirmacao:
                try:
                    data = _datetime.fromisoformat(str(ultima_confirmacao).replace("Z", "+00:00"))
                    if data.tzinfo is None:
                        data = data.replace(tzinfo=agora.tzinfo)
                    dias_sem_confirmar = max(0, (agora - data.astimezone(agora.tzinfo)).days)
                except (TypeError, ValueError):
                    pass
            item = {
                "sku": produto.get("sku"),
                "titulo": produto.get("titulo"),
                "marca": produto.get("marca"),
                "modelo": produto.get("modelo"),
                "ano": produto.get("ano"),
                "lado": produto.get("lado"),
                "preco": produto.get("preco"),
                "qtd": produto.get("qtd"),
                "fotos": produto.get("fotos") or [],
                "loc": produto.get("loc"),
                "compatibilidade": produto.get("compatibilidade") or [],
                "pontuacao": pontos,
                "ano_compativel": _ano_produto_compativel(produto, ano),
                "dias_sem_confirmar": dias_sem_confirmar,
                "confirmacao_vencida": (
                    dias_sem_confirmar is None or dias_sem_confirmar >= 90
                ),
            }
            candidatos.append(item)

        candidatos_ano_exato = [
            item for item in candidatos if item.get("ano_compativel") is True
        ]
        if candidatos_ano_exato:
            candidatos = candidatos_ano_exato

        candidatos.sort(
            key=lambda item: (
                item["pontuacao"],
                item.get("ano_compativel") is True,
                bool(item.get("fotos")),
                float(item.get("preco") or 0) > 0,
                float(item.get("qtd") or 0),
            ),
            reverse=True,
        )
        candidatos = candidatos[:5]
        return {
            "ok": True,
            "encontrado": bool(candidatos),
            "status_sugerido": (
                "aguardando_confirmacao_fisica"
                if candidatos else "produto_nao_cadastrado"
            ),
            "confirmacao_fisica_obrigatoria": bool(candidatos),
            "candidatos": candidatos,
        }

    @app.route("/integracoes/marcelo/buscar-estoque", methods=["POST", "OPTIONS"])
    def marcelo_buscar_estoque():
        """Busca isolada: nao altera pedido, estoque nem envia mensagens."""
        if request.method == "OPTIONS":
            return _options_resp()

        dados = request.get_json(silent=True) or {}
        resultado = _buscar_estoque_dados(
            dados.get("peca"),
            dados.get("veiculo"),
            dados.get("ano"),
            dados.get("lado"),
        )
        if not resultado.get("ok") and resultado.get("erro") == "peca obrigatoria":
            return jsonify(resultado), 400
        if not resultado.get("ok"):
            return jsonify(resultado), 502
        return jsonify(resultado)

    @app.route("/integracoes/marcelo/pedido-unico", methods=["POST", "OPTIONS"])
    def marcelo_pedido_unico():
        """Entrada idempotente do Marcelo: no maximo um pedido aberto por telefone."""
        if request.method == "OPTIONS":
            return _options_resp()

        dados = request.get_json(silent=True) or {}
        phone = _normalizar_fone_pedido(dados.get("phone") or dados.get("telefone"))
        peca = str(dados.get("peca") or "").strip()
        if len(phone) < 12 or not peca:
            return jsonify({
                "ok": False,
                "erro": "phone e peca sao obrigatorios",
            }), 400

        campos = {
            "phone": phone,
            "nome": str(dados.get("nome") or "").strip() or None,
            "peca": peca,
            "veiculo": str(dados.get("veiculo") or "").strip() or None,
            "ano": str(dados.get("ano") or "").strip() or None,
            "lado": str(dados.get("lado") or "").strip() or None,
            "cor": str(dados.get("cor") or "").strip() or None,
            "status": "aguardando",
        }

        # O servidor roda com um processo no Railway. O lock cobre mensagens
        # paralelas do n8n e impede duas consultas/criacoes ao mesmo tempo.
        with _pedido_entrada_lock:
            try:
                consulta = requests.get(
                    f"{_WRX_SB_URL}/rest/v1/pedidos",
                    params={
                        "select": "id,phone,nome,peca,veiculo,ano,lado,cor,status,criado_em",
                        "phone": f"eq.{phone}",
                        "status": "in.(aguardando,verificando,atendimento,confirmado)",
                        "order": "criado_em.asc",
                        "limit": "1",
                    },
                    headers=_wrx_headers(),
                    timeout=15,
                )
                if consulta.status_code != 200:
                    return jsonify({
                        "ok": False,
                        "erro": "falha ao consultar pedido existente",
                        "http": consulta.status_code,
                    }), 502
                existentes = consulta.json()
            except Exception as e:
                return jsonify({"ok": False, "erro": f"falha ao consultar pedido: {e}"}), 502

            if isinstance(existentes, list) and existentes:
                return jsonify({
                    "ok": True,
                    "criado": False,
                    "duplicado": True,
                    "pedido": existentes[0],
                })

            try:
                criacao = requests.post(
                    f"{_WRX_SB_URL}/rest/v1/pedidos",
                    headers={**_wrx_headers(), "Prefer": "return=representation"},
                    json={k: v for k, v in campos.items() if v is not None},
                    timeout=15,
                )
                if criacao.status_code not in (200, 201):
                    return jsonify({
                        "ok": False,
                        "erro": "falha ao criar pedido",
                        "http": criacao.status_code,
                        "detalhe": criacao.text[:300],
                    }), 502
                criado = criacao.json()
                pedido = criado[0] if isinstance(criado, list) and criado else criado
                return jsonify({
                    "ok": True,
                    "criado": True,
                    "duplicado": False,
                    "pedido": pedido,
                }), 201
            except Exception as e:
                return jsonify({"ok": False, "erro": f"falha ao criar pedido: {e}"}), 502

    def _pedidos_estado_save(state_file, estado):
        """Grava o controle de disparos atomicamente no volume do Railway."""
        tmp = state_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(estado, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, state_file)

    def _func_em_janela(emp, wd, h):
        # wd: Mon=0 .. Sun=6 ; h: hora 0-23 (America/Sao_Paulo)
        if 0 <= wd <= 4 and 9 <= h < 17:          # seg-sex 9-17: todos
            return True
        if wd == 5 and 9 <= h < 13:               # sabado 9-13: rafael e geisa
            return emp in ("rafael", "geisa")
        return False                              # domingo: ninguem

    def _mensagens_funcionarios_waha():
        """Contingencia: le o WAHA quando o webhook/n8n nao persistiu a mensagem."""
        mensagens = []
        numeros = {
            _normalizar_fone_pedido(numero)
            for numero in FUNCS_PEDIDO.values()
        }
        try:
            consulta = requests.get(
                f"{_WRX_SB_URL}/rest/v1/funcionarios",
                params={"select": "whatsapp", "limit": "200"},
                headers=_wrx_headers(),
                timeout=15,
            )
            funcionarios = (
                consulta.json() if consulta.status_code == 200 else []
            )
            if isinstance(funcionarios, list):
                numeros.update(
                    _normalizar_fone_pedido(linha.get("whatsapp"))
                    for linha in funcionarios
                    if _normalizar_fone_pedido(linha.get("whatsapp"))
                )
        except Exception as e:
            print(f"[RESPOSTAS-FUNC] falha ao listar funcionarios: {e}")
        for numero in numeros:
            try:
                resposta = requests.get(
                    (
                        f"{WAHA_BASE}/api/{WAHA_SESSION}/chats/"
                        f"{numero}@c.us/messages"
                    ),
                    params={"limit": "100", "downloadMedia": "true"},
                    headers=_waha_h({"Accept": "application/json"}),
                    timeout=25,
                )
                dados = resposta.json() if resposta.status_code == 200 else []
            except Exception as e:
                print(f"[RESPOSTAS-FUNC] falha ao ler WAHA {numero}: {e}")
                continue
            if isinstance(dados, dict):
                dados = dados.get("data") or dados.get("messages") or []
            if not isinstance(dados, list):
                continue
            for linha in dados:
                if not isinstance(linha, dict) or linha.get("fromMe") is True:
                    continue
                origem = linha.get("from") or linha.get("chatId") or ""
                media = (
                    linha.get("media")
                    if isinstance(linha.get("media"), dict)
                    else {}
                )
                media_url = str(
                    linha.get("mediaUrl") or media.get("url") or ""
                ).strip()
                if media_url:
                    media_url = re.sub(
                        r"^https?://(?:localhost|127\.0\.0\.1):3000",
                        WAHA_BASE.rstrip("/"),
                        media_url,
                        flags=re.I,
                    )
                mimetype = str(
                    media.get("mimetype") or linha.get("mimetype") or ""
                ).lower()
                tipo = (
                    "audio" if mimetype.startswith("audio/")
                    else "image" if mimetype.startswith("image/")
                    else "media" if media_url or linha.get("hasMedia")
                    else "text"
                )
                timestamp = linha.get("timestamp")
                try:
                    criado_em = _datetime.fromtimestamp(
                        float(timestamp),
                        tz=_datetime.now().astimezone().tzinfo,
                    ).isoformat()
                except (TypeError, ValueError, OSError):
                    criado_em = ""
                mensagens.append({
                    "id": f"waha:{linha.get('id')}",
                    "numero": numero,
                    "chat_id": origem,
                    "mensagem": linha.get("body") or "",
                    "de_mim": False,
                    "criado_em": criado_em,
                    "tipo": tipo,
                    "media_url": media_url,
                })
        return mensagens

    @app.route("/integracoes/whatsapp/processar-respostas-funcionarios", methods=["GET", "POST", "OPTIONS"])
    def whatsapp_processar_respostas_funcionarios():
        """Le respostas ja salvas pelo n8n, sem alterar o webhook existente."""
        if request.method == "OPTIONS":
            return _options_resp()

        estado_file = os.path.join(_INTEG_DIR, "mensagens_func_processadas.json")
        try:
            consulta = requests.get(
                f"{_WRX_SB_URL}/rest/v1/mensagens_whatsapp",
                params={
                    "select": (
                        "id,numero,chat_id,mensagem,de_mim,criado_em,"
                        "tipo,media_url"
                    ),
                    "de_mim": "eq.false",
                    "order": "criado_em.desc",
                    "limit": "200",
                },
                headers=_wrx_headers(),
                timeout=20,
            )
            mensagens = consulta.json() if consulta.status_code == 200 else []
        except Exception as e:
            return jsonify({"ok": False, "erro": f"falha ao consultar mensagens: {e}"}), 502
        if not isinstance(mensagens, list):
            mensagens = []

        mensagens_func = []
        for mensagem in mensagens:
            numero = _normalizar_fone_pedido(
                mensagem.get("numero") or mensagem.get("chat_id")
            )
            if not numero:
                continue
            if any(
                numero == _normalizar_fone_pedido(whatsapp)
                for whatsapp in FUNCS_PEDIDO.values()
            ):
                mensagens_func.append((mensagem, numero))
                continue
            # Funcionarios adicionados pelo painel tambem sao aceitos.
            if _identificar_funcionario_pedido(numero):
                mensagens_func.append((mensagem, numero))
        if not mensagens_func:
            for mensagem in _mensagens_funcionarios_waha():
                numero = _normalizar_fone_pedido(
                    mensagem.get("numero") or mensagem.get("chat_id")
                )
                if numero:
                    mensagens_func.append((mensagem, numero))

        try:
            with open(estado_file, encoding="utf-8") as arquivo:
                processadas = set(json.load(arquivo))
            primeira_execucao = False
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            processadas = set()
            primeira_execucao = True

        ids_atuais = {
            str(mensagem.get("id"))
            for mensagem, _ in mensagens_func
            if mensagem.get("id") is not None
        }

        def reconciliar_fotos():
            vinculadas = 0
            for mensagem, numero in mensagens_func:
                foto = str(mensagem.get("media_url") or "").strip()
                tipo = str(mensagem.get("tipo") or "").lower()
                if not foto or tipo == "audio":
                    continue
                evento = _ultimo_evento_tenho_funcionario(numero)
                if not evento or not evento.get("item_id"):
                    continue
                if str(mensagem.get("criado_em") or "") < str(
                    evento.get("recebido_em") or ""
                ):
                    continue
                try:
                    resposta = requests.post(
                        f"http://127.0.0.1:{PORT}/integracoes/marcelo/pedido-item-foto",
                        json={
                            "phone": numero,
                            "item_id": evento["item_id"],
                            "foto": foto,
                        },
                        timeout=30,
                    )
                    if resposta.status_code in (200, 201):
                        vinculadas += 1
                except Exception as e:
                    print(
                        f"[FOTO-FUNC] falha item={evento.get('item_id')} "
                        f"mensagem={mensagem.get('id')}: {e}"
                    )
            return vinculadas

        if primeira_execucao:
            _pedidos_estado_save(estado_file, sorted(ids_atuais))
            return jsonify({
                "ok": True,
                "seed": len(ids_atuais),
                "processadas": 0,
                "fotos_vinculadas": reconciliar_fotos(),
                "msg": "historico marcado; somente novas respostas serao processadas",
            })

        resultados = []
        novas = sorted(
            (
                (mensagem, numero)
                for mensagem, numero in mensagens_func
                if str(mensagem.get("id")) not in processadas
            ),
            key=lambda par: str(par[0].get("criado_em") or ""),
        )
        for mensagem, numero in novas:
            mid = str(mensagem.get("id"))
            texto = str(mensagem.get("mensagem") or "")
            acao, pedido_id, item_id = _interpretar_resposta_funcionario({
                "mensagem": texto,
            })
            if acao and pedido_id:
                try:
                    resposta = requests.post(
                        f"http://127.0.0.1:{PORT}/integracoes/marcelo/resposta-funcionario",
                        json={
                            "phone": numero,
                            "mensagem": texto,
                            "pedido_id": pedido_id,
                            "item_id": item_id,
                        },
                        timeout=30,
                    )
                    resultados.append({
                        "mensagem_id": mid,
                        "pedido_id": pedido_id,
                        "item_id": item_id,
                        "http": resposta.status_code,
                        "ok": resposta.status_code in (200, 201),
                    })
                    if resposta.status_code in (200, 201):
                        processadas.add(mid)
                    elif resposta.status_code == 404:
                        _waha_enviar(
                            numero,
                            (
                                f"Nao encontrei o item #{item_id}. "
                                "Confira o codigo mostrado no painel e responda "
                                "novamente, por exemplo: TENHO #258-1."
                            ),
                        )
                except Exception as e:
                    resultados.append({
                        "mensagem_id": mid,
                        "ok": False,
                        "erro": str(e),
                    })
                    continue
            elif not str(texto).strip() and mensagem.get("media_url"):
                processadas.add(mid)

        # Mantem somente IDs ainda presentes na janela consultada.
        processadas = processadas.intersection(ids_atuais)
        _pedidos_estado_save(estado_file, sorted(processadas))
        return jsonify({
            "ok": True,
            "novas": len(novas),
            "processadas": sum(1 for item in resultados if item.get("ok")),
            "fotos_vinculadas": reconciliar_fotos(),
            "resultados": resultados,
        })

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
        desde = (now - _dt.timedelta(hours=100)).astimezone(_dt.timezone.utc).isoformat()
        try:
            rows = requests.get(
                f"{_WRX_SB_URL}/rest/v1/pedidos"
                f"?select=id,phone,peca,veiculo,ano,lado,status,criado_em"
                f"&status=in.(aguardando,verificando)"
                f"&criado_em=gte.{_urlparse.quote(desde)}&order=criado_em.asc",
                headers={"apikey": _WRX_SB_KEY, "Authorization": f"Bearer {_WRX_SB_KEY}"},
                timeout=20
            ).json()
        except Exception as e:
            return jsonify({"ok": False, "erro": str(e)})
        if not isinstance(rows, list):
            rows = []
        state_file = os.path.join(_INTEG_DIR, "avisos_func.json")
        if not _pedidos_manha_lock.acquire(blocking=False):
            return jsonify({"ok": True, "skip": "disparo ja em andamento"})

        # O Marcelo pode receber a mesma mensagem varias vezes em paralelo.
        # Mantem somente UM pedido aberto por telefone e cancela os excedentes.
        def _fone_de(p):
            f = "".join(ch for ch in str(p.get("phone") or "") if ch.isdigit())
            return f or ("id:" + str(p.get("id") or ""))

        primeiro_por_fone = {}
        rows_unicas = []
        duplicados = []
        for p in rows:  # consulta ordenada do mais antigo para o mais novo
            fone = _fone_de(p)
            if fone not in primeiro_por_fone:
                primeiro_por_fone[fone] = str(p.get("id") or "")
                rows_unicas.append(p)
            else:
                duplicados.append(p)
        cancelados = 0
        for p in duplicados:
            pid_dup = str(p.get("id") or "")
            if not pid_dup:
                continue
            try:
                r_dup = requests.patch(
                    f"{_WRX_SB_URL}/rest/v1/pedidos",
                    params={"id": f"eq.{pid_dup}"},
                    headers={**_wrx_headers(), "Prefer": "return=minimal"},
                    json={"status": "cancelado"},
                    timeout=10,
                )
                if r_dup.status_code in (200, 204):
                    cancelados += 1
            except Exception as e:
                print(f"[PEDIDOS-MANHA] falha ao cancelar duplicado {pid_dup}: {e}")
        rows = rows_unicas

        if not abertos:
            _pedidos_manha_lock.release()
            return jsonify({
                "ok": True,
                "skip": "ninguem em janela",
                "wd": wd,
                "h": h,
                "duplicados_cancelados": cancelados,
            })
        primeira_vez = not os.path.exists(state_file)
        if primeira_vez:
            # 1º deploy: nao dispara o historico (evita blast); marca tudo como avisado.
            seed = {str(p.get("id")): list(FUNCS_PEDIDO.keys()) for p in rows if p.get("id")}
            try:
                _pedidos_estado_save(state_file, seed)
            except Exception:
                pass
            _pedidos_manha_lock.release()
            return jsonify({"ok": True, "seed": len(seed), "msg": "primeira execucao - historico marcado, novos pedidos a partir de agora"})
        try:
            if os.path.exists(state_file):
                with open(state_file, encoding="utf-8") as arquivo:
                    estado = _json.load(arquivo)
            else:
                estado = {}
        except Exception:
            estado = {}
        ids_atuais = {str(p.get("id")) for p in rows if p.get("id")}
        estado = {k: v for k, v in estado.items() if k in ids_atuais}   # prune antigos

        # ── Trava anti-repetição: 1 aviso por (NÚMERO + PEÇA) por DIA ───────────
        #   Cada (fone|peca) vira "dono" de UM pedido no dia. Pedido DUPLICADO (mesmo
        #   número e mesma peça, id diferente) NÃO dispara de novo — corrige o bot
        #   avisando o mesmo pedido 4x. Pedido de PEÇA DIFERENTE do mesmo cliente passa
        #   normal (não perde venda). Durável em dx_config (sobrevive a reinício do
        #   Railway). Fail-open: se o Supabase falhar, _disp_map fica vazio e não bloqueia.
        _disp_dia = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 3 * 3600))  # data BR
        _disp_chave = "pddisp:" + _disp_dia
        _disp_map = {}
        try:
            _rdsp = requests.get(
                f"{_WRX_SB_URL}/rest/v1/dx_config",
                params={"chave": f"eq.{_disp_chave}", "select": "valor"},
                headers=_wrx_headers(), timeout=12)
            if _rdsp.status_code == 200 and _rdsp.json():
                _vdsp = _rdsp.json()[0].get("valor")
                if isinstance(_vdsp, dict):
                    # valor por (fone|peca) = LISTA de funcionários que JÁ receberam o aviso hoje.
                    # registro antigo (valor string/pid) → trata como "todos já avisados" (não repete).
                    for _k, _v in _vdsp.items():
                        _disp_map[_k] = list(_v) if isinstance(_v, list) else list(FUNCS_PEDIDO.keys())
        except Exception:
            pass
        def _disp_key(pp):
            fone = "".join(ch for ch in str(pp.get("phone") or "") if ch.isdigit())
            peca = " ".join(str(pp.get("peca") or "").lower().split())
            return (fone or ("id:" + str(pp.get("id") or ""))) + "|" + peca
        def _disp_salvar():
            try:
                requests.post(
                    f"{_WRX_SB_URL}/rest/v1/dx_config",
                    headers={**_wrx_headers(), "Content-Type": "application/json",
                             "Prefer": "resolution=merge-duplicates,return=minimal"},
                    json={"chave": _disp_chave, "valor": _disp_map}, timeout=12)
            except Exception as _edsp:
                print(f"[PEDIDOS-MANHA] falha ao gravar trava disparo: {_edsp}")

        # ── 1 disparo por NÚMERO enquanto tiver pedido aberto ──────────────────
        #   rows vem ordenado por criado_em.asc. Para cada telefone só UM pedido
        #   pode disparar: o que já começou a ser avisado (está no estado) ou,
        #   se nenhum começou, o mais antigo aberto. Pedidos extras do mesmo
        #   número ficam de fora até o ativo ser resolvido (sair de
        #   aguardando/verificando), aí o próximo do cliente volta a disparar.
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
            # CONTROLE DURÁVEL (no banco) de quem JÁ recebeu este (número+peça) hoje — sobrevive a
            # restart do servidor (o estado local some no restart e fazia re-disparar = o bug dos 4x).
            # Soma com o estado local. Respeita as janelas: quem ainda não recebeu continua recebendo.
            _k = _disp_key(p)
            ja_dur = set(_disp_map.get(_k) or [])
            ja = set(estado.get(pid, [])) | ja_dur
            alvos = [e for e in abertos if e not in ja]
            if not alvos:
                continue
            veic = (p.get("veiculo") or "").strip()
            ano = (p.get("ano") or "").strip()
            lado = (p.get("lado") or "").strip()
            with _pedido_itens_lock:
                _, _, itens = _carregar_itens_pedido(p)
            linhas_itens = []
            for item in itens:
                detalhe = " ".join(filter(None, [
                    item.get("veiculo") or veic,
                    item.get("ano") or ano,
                    item.get("lado") or lado,
                ]))
                linhas_itens.append(
                    f"*#{item['id']}* {item.get('peca') or peca}"
                    + (f" - {detalhe}" if detalhe else "")
                )
            msg = (
                f"*Pedido #{pid}*\n\n"
                + "\n".join(linhas_itens)
                + "\n\nResponda usando o codigo da peca:\n"
                + f"*Tenho #{itens[0]['id']}*\n"
                + "ou\n"
                + f"*Nao tenho #{itens[0]['id']}*\n\n"
                + "Se tiver, envie tambem foto e valor."
            )
            enviados_pedido = 0
            for e in alvos:
                # ── TRAVA ATÔMICA anti-duplicação entre WORKERS (corrige os "4 disparos") ──
                # O cron roda em CADA worker do servidor; com vários workers, várias execuções liam
                # a trava ao MESMO tempo (antes de qualquer uma gravar) e TODAS enviavam. Aqui cada
                # (pedido+funcionário+dia) só pode ser INSERIDO 1x no banco (dx_config.chave é PK):
                # o 1º worker cria (201) e envia; os concorrentes recebem 409 e NÃO reenviam.
                _lock_chave = "pdsent:%s:%s:%s" % (_disp_dia, pid, e)
                _dup = False
                try:
                    _rl = requests.post(
                        f"{_WRX_SB_URL}/rest/v1/dx_config",
                        headers={**_wrx_headers(), "Content-Type": "application/json", "Prefer": "return=minimal"},
                        json={"chave": _lock_chave, "valor": {"t": int(time.time())}},
                        timeout=10)
                    if _rl.status_code == 409:   # chave já existe = outro worker já reservou
                        _dup = True
                except Exception:
                    pass
                if _dup:
                    # outro worker já disparou este aviso agora — marca como avisado e NÃO reenvia.
                    ja.add(e); ja_dur.add(e); estado[pid] = list(ja)
                    try:
                        _pedidos_estado_save(state_file, estado)
                    except Exception:
                        pass
                    continue
                # Reserva antes do envio: se o processo cair depois de o WAHA aceitar,
                # o próximo ciclo não dispara o mesmo pedido novamente.
                ja.add(e)
                estado[pid] = list(ja)
                try:
                    _pedidos_estado_save(state_file, estado)
                except Exception as save_err:
                    ja.discard(e)
                    estado[pid] = list(ja)
                    print(f"[PEDIDOS-MANHA] nao enviou sem persistir pedido={pid} alvo={e}: {save_err}")
                    continue
                ok, _ = _waha_enviar(FUNCS_PEDIDO[e], msg)
                if ok:
                    enviados_pedido += 1
                    ja_dur.add(e)   # registra no controle DURÁVEL quem recebeu (não repete após restart)
                else:
                    # WAHA recusou antes de aceitar: libera para tentar no próximo ciclo.
                    ja.discard(e)
                    estado[pid] = list(ja)
                    try:
                        _pedidos_estado_save(state_file, estado)
                    except Exception as save_err:
                        print(f"[PEDIDOS-MANHA] falha ao liberar pedido={pid} alvo={e}: {save_err}")
            if enviados_pedido:
                _disp_map[_k] = sorted(ja_dur)   # persiste no banco QUEM já recebeu este (número+peça)
                _disp_salvar()
                try:
                    requests.patch(
                        f"{_WRX_SB_URL}/rest/v1/pedidos",
                        params={
                            "id": f"eq.{pid}",
                            "status": "eq.aguardando",
                        },
                        headers={**_wrx_headers(), "Prefer": "return=minimal"},
                        json={"status": "verificando"},
                        timeout=10,
                    )
                except Exception as e:
                    print(f"[PEDIDOS-MANHA] falha ao marcar pedido={pid} como verificando: {e}")
                novos += 1
        try:
            _pedidos_estado_save(state_file, estado)
        except Exception:
            pass
        _pedidos_manha_lock.release()
        return jsonify({
            "ok": True,
            "abertos": abertos,
            "pedidos_disparados": novos,
            "duplicados_cancelados": cancelados,
        })

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

    @app.route("/integracoes/marcelo/baixar-estoque", methods=["POST", "OPTIONS"])
    def marcelo_baixar_estoque():
        """Baixa quantidade do estoque quando a peça é vendida no CRM."""
        if request.method == "OPTIONS":
            return _options_resp()

        dados = request.get_json(silent=True) or {}
        sku = str(dados.get("sku") or "").strip()
        produto = str(dados.get("produto") or dados.get("titulo") or dados.get("peca") or "").strip()
        veiculo = str(dados.get("veiculo") or "").strip()
        ano = str(dados.get("ano") or "").strip()
        lado = str(dados.get("lado") or "").strip()
        try:
            qty = int(dados.get("qty") or 1)
        except (TypeError, ValueError):
            qty = 1
        if not sku and not produto:
            return jsonify({"ok": False, "erro": "sku ou produto obrigatorio"}), 400
        if qty <= 0:
            return jsonify({"ok": False, "erro": "qty invalido"}), 400

        if not sku:
            busca = _buscar_estoque_dados(produto, veiculo, ano, lado)
            candidatos = busca.get("candidatos") or []
            candidato = next(
                (
                    item for item in candidatos
                    if item.get("ano_compativel") is not False
                ),
                None,
            )
            if not candidato and len(candidatos) == 1:
                candidato = candidatos[0]
            if not candidato:
                return jsonify({
                    "ok": False,
                    "erro": "nao consegui inferir o SKU a partir do produto",
                    "candidatos": candidatos[:5],
                }), 409
            sku = str(candidato.get("sku") or "").strip()
            if not sku:
                return jsonify({
                    "ok": False,
                    "erro": "candidato sem SKU",
                }), 409

        try:
            consulta = requests.get(
                f"{_WRX_SB_URL}/rest/v1/pecas_estoque",
                params={"select": "sku,qtd", "sku": f"eq.{sku}", "limit": "1"},
                headers=_wrx_headers(),
                timeout=15,
            )
            itens = consulta.json() if consulta.status_code == 200 else []
        except Exception as e:
            return jsonify({"ok": False, "erro": f"falha ao consultar estoque: {e}"}), 502
        if not isinstance(itens, list) or not itens:
            return jsonify({"ok": False, "erro": "sku nao encontrado"}), 404

        item = itens[0]
        qtd_atual = int(item.get("qtd") or 0)
        nova_qtd = max(0, qtd_atual - qty)
        try:
            alteracao = requests.patch(
                f"{_WRX_SB_URL}/rest/v1/pecas_estoque",
                params={"sku": f"eq.{sku}"},
                headers=_wrx_headers(),
                json={
                    "qtd": nova_qtd,
                    "atualizado": _datetime.utcnow().isoformat() + "Z",
                },
                timeout=15,
            )
            if alteracao.status_code not in (200, 204):
                return jsonify({
                    "ok": False,
                    "erro": "falha ao baixar estoque",
                    "http": alteracao.status_code,
                    "detalhe": alteracao.text[:200],
                }), 502
        except Exception as e:
            return jsonify({"ok": False, "erro": f"falha ao baixar estoque: {e}"}), 502

        return jsonify({
            "ok": True,
            "sku": sku,
            "qtd_anterior": qtd_atual,
            "qtd_nova": nova_qtd,
            "zerado": nova_qtd == 0,
        })

    @app.route("/integracoes/whatsapp/enviar", methods=["POST", "OPTIONS"])
    def whatsapp_enviar():
        if request.method == "OPTIONS":
            return _options_resp()
        data = request.get_json(force=True) or {}
        numero = data.get("numero") or _waha_numero_sessao()
        texto = (data.get("texto") or "").strip()
        ok, msg = _waha_enviar(numero, texto)
        return jsonify({"ok": ok, "detalhe": msg, "numero": numero})

    @app.route("/integracoes/whatsapp/enviar-imagem", methods=["POST", "OPTIONS"])
    def whatsapp_enviar_imagem():
        """Envia uma IMAGEM (por URL) + legenda no WhatsApp. Usado pra mandar o
        comprovante de pagamento pro funcionario. Body: {numero, url, legenda}."""
        if request.method == "OPTIONS":
            return _options_resp()
        data = request.get_json(force=True) or {}
        numero = data.get("numero") or ""
        url = data.get("url") or ""
        legenda = (data.get("legenda") or "").strip()
        ok, msg = _waha_enviar_imagem(numero, url, legenda)
        return jsonify({"ok": ok, "detalhe": msg, "numero": numero})

    # ── Divulgação em GRUPOS do WhatsApp ────────────────────────────────────────
    @app.route("/integracoes/whatsapp/grupos", methods=["GET", "OPTIONS"])
    def whatsapp_grupos():
        """Lista os grupos do WhatsApp conectado (WAHA)."""
        if request.method == "OPTIONS":
            return _options_resp()
        try:
            r = requests.get(f"{WAHA_BASE}/api/{WAHA_SESSION}/groups",
                             headers=_waha_h({"Accept": "application/json"}), timeout=25)
            data = r.json() if r.status_code == 200 else {}
        except Exception as e:
            return jsonify({"ok": False, "erro": str(e)}), 502
        grupos = []
        def _add(gid, g):
            gid = gid.get("_serialized") if isinstance(gid, dict) else gid
            if not gid or not str(gid).endswith("@g.us") or not isinstance(g, dict):
                return
            nome = g.get("subject") or (g.get("groupMetadata") or {}).get("subject") or g.get("name") or gid
            tam = g.get("size") or (g.get("groupMetadata") or {}).get("size")
            grupos.append({"id": gid, "nome": nome, "tamanho": tam})
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            for g in data["data"]:
                _add(g.get("id"), g)
        elif isinstance(data, dict):
            for gid, g in data.items():
                _add(gid, g)
        elif isinstance(data, list):
            for g in data:
                _add(g.get("id"), g)
        grupos.sort(key=lambda x: (x.get("nome") or "").lower())
        return jsonify({"ok": True, "total": len(grupos), "grupos": grupos})

    @app.route("/integracoes/whatsapp/oficinas", methods=["GET", "POST", "OPTIONS"])
    def whatsapp_oficinas():
        """Cadastro de OFICINAS (clientes B2B) pra envio de pergunta/divulgação.
        Guardado em dx_config:oficinas. GET lista; POST {nome,telefone,loja,endereco}
        cadastra; POST {_remover:id} remove."""
        if request.method == "OPTIONS":
            return _options_resp()
        lst = []
        try:
            r = requests.get(f"{_WRX_SB_URL}/rest/v1/dx_config",
                             params={"chave": "eq.oficinas", "select": "valor"},
                             headers=_wrx_headers(), timeout=12)
            if r.status_code == 200 and r.json():
                v = r.json()[0].get("valor")
                if isinstance(v, list):
                    lst = v
        except Exception:
            pass
        if request.method == "POST":
            data = request.get_json(force=True) or {}
            if data.get("_remover"):
                lst = [o for o in lst if str(o.get("id")) != str(data["_remover"])]
            else:
                nome = str(data.get("nome") or "").strip()
                tel = "".join(ch for ch in str(data.get("telefone") or "") if ch.isdigit())
                if not nome or len(tel) < 10:
                    return jsonify({"ok": False, "erro": "informe nome e telefone com DDD"}), 400
                if len(tel) in (10, 11):
                    tel = "55" + tel
                lst.append({
                    "id": _secrets.token_hex(4),
                    "nome": nome, "telefone": tel,
                    "loja": str(data.get("loja") or "").strip(),
                    "endereco": str(data.get("endereco") or "").strip(),
                })
            try:
                requests.post(f"{_WRX_SB_URL}/rest/v1/dx_config",
                              headers={**_wrx_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
                              json={"chave": "oficinas", "valor": lst}, timeout=12)
            except Exception as e:
                return jsonify({"ok": False, "erro": str(e)}), 502
            return jsonify({"ok": True, "total": len(lst)})
        return jsonify({"ok": True, "oficinas": lst, "total": len(lst)})

    @app.route("/integracoes/whatsapp/colaboradores", methods=["GET", "POST", "OPTIONS"])
    def whatsapp_colaboradores():
        """Lista/cadastra colaboradores (parceiros) pra divulgação individual.
        GET = junta funcionarios + colaboradores (com whatsapp). POST {nome, whatsapp} cadastra."""
        if request.method == "OPTIONS":
            return _options_resp()
        if request.method == "POST":
            data = request.get_json(force=True) or {}
            nome = str(data.get("nome") or "").strip()
            whats = "".join(ch for ch in str(data.get("whatsapp") or "") if ch.isdigit())
            if not nome or len(whats) < 10:
                return jsonify({"ok": False, "erro": "informe nome e WhatsApp com DDD"}), 400
            try:
                requests.post(f"{_WRX_SB_URL}/rest/v1/colaboradores",
                              headers={**_wrx_headers(), "Prefer": "return=minimal"},
                              json={"nome": nome, "whatsapp": whats, "disponivel": True}, timeout=12)
            except Exception as e:
                return jsonify({"ok": False, "erro": str(e)}), 502
            return jsonify({"ok": True})
        out, visto = [], set()
        for tab in ("funcionarios", "colaboradores"):
            try:
                rows = requests.get(f"{_WRX_SB_URL}/rest/v1/{tab}",
                                    params={"select": "nome,whatsapp", "limit": "200"},
                                    headers=_wrx_headers(), timeout=12).json()
            except Exception:
                rows = []
            for c in (rows if isinstance(rows, list) else []):
                nome = str(c.get("nome") or "").strip()
                whats = "".join(ch for ch in str(c.get("whatsapp") or "") if ch.isdigit())
                if not nome or len(whats) < 10 or whats in visto:
                    continue
                if len(whats) in (10, 11):
                    whats = "55" + whats
                visto.add(whats)
                out.append({"nome": nome, "whatsapp": whats})
        out.sort(key=lambda x: x["nome"].lower())
        return jsonify({"ok": True, "colaboradores": out, "total": len(out)})

    @app.route("/integracoes/whatsapp/grupos-selecao", methods=["GET", "POST", "OPTIONS"])
    def whatsapp_grupos_selecao():
        """Seleção durável de grupos pra divulgação (dx_config:grupos_selecao).
        GET retorna a lista de ids; POST {grupos:[...]} salva."""
        if request.method == "OPTIONS":
            return _options_resp()
        if request.method == "POST":
            data = request.get_json(force=True) or {}
            ids = [str(g) for g in (data.get("grupos") or []) if str(g).endswith("@g.us")]
            try:
                requests.post(f"{_WRX_SB_URL}/rest/v1/dx_config",
                              headers={**_wrx_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
                              json={"chave": "grupos_selecao", "valor": ids}, timeout=12)
            except Exception as e:
                return jsonify({"ok": False, "erro": str(e)}), 502
            return jsonify({"ok": True, "total": len(ids)})
        ids = []
        try:
            r = requests.get(f"{_WRX_SB_URL}/rest/v1/dx_config",
                             params={"chave": "eq.grupos_selecao", "select": "valor"},
                             headers=_wrx_headers(), timeout=12)
            if r.status_code == 200 and r.json():
                v = r.json()[0].get("valor")
                if isinstance(v, list):
                    ids = [str(x) for x in v]
        except Exception:
            pass
        return jsonify({"ok": True, "grupos": ids, "total": len(ids)})

    @app.route("/integracoes/whatsapp/enviar-grupos", methods=["POST", "OPTIONS"])
    def whatsapp_enviar_grupos():
        """Dispara um produto (imagem + mensagem) pros grupos selecionados, com
        ESPAÇAMENTO aleatório entre cada envio e pequena VARIAÇÃO na mensagem
        (anti-bloqueio). Roda em background; status em dx_config:grupos_envio_status."""
        if request.method == "OPTIONS":
            return _options_resp()
        data = request.get_json(force=True) or {}
        grupos = [str(g) for g in (data.get("grupos") or []) if str(g).endswith(("@g.us", "@c.us"))]
        imagem = str(data.get("imagem") or "").strip()
        mensagem = str(data.get("mensagem") or "").strip()
        if not grupos:
            return jsonify({"ok": False, "erro": "selecione ao menos 1 grupo"}), 400
        if not mensagem and not imagem.startswith("http"):
            return jsonify({"ok": False, "erro": "sem conteudo para enviar"}), 400
        job = {"total": len(grupos), "enviados": 0, "ok": 0, "falhas": 0, "rodando": True, "inicio": time.time()}
        def _status():
            try:
                requests.post(f"{_WRX_SB_URL}/rest/v1/dx_config",
                              headers={**_wrx_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
                              json={"chave": "grupos_envio_status", "valor": job}, timeout=10)
            except Exception:
                pass
        _status()
        INTROS = ["", "🔥 ", "✅ ", "🚗 ", "📢 ", "👇 ", "⚙️ ", "💥 "]
        def _worker():
            import random as R, time as T
            for i, gid in enumerate(grupos):
                cap = R.choice(INTROS) + mensagem
                try:
                    if imagem.startswith("http"):
                        rr = requests.post(f"{WAHA_BASE}/api/sendImage",
                                           headers=_waha_h({"Content-Type": "application/json"}),
                                           json={"session": WAHA_SESSION, "chatId": gid,
                                                 "file": {"url": imagem}, "caption": cap}, timeout=40)
                    else:
                        rr = requests.post(f"{WAHA_BASE}/api/sendText",
                                           headers=_waha_h({"Content-Type": "application/json"}),
                                           json={"session": WAHA_SESSION, "chatId": gid, "text": cap}, timeout=40)
                    ok = rr.status_code in (200, 201)
                except Exception:
                    ok = False
                job["enviados"] += 1
                job["ok"] += 1 if ok else 0
                job["falhas"] += 0 if ok else 1
                _status()
                if i < len(grupos) - 1:
                    T.sleep(R.uniform(8, 20))   # espaçamento anti-bloqueio
            job["rodando"] = False
            job["fim"] = time.time()
            _status()
        _threading.Thread(target=_worker, daemon=True).start()
        return jsonify({"ok": True, "msg": f"Disparo iniciado para {len(grupos)} grupos", "total": len(grupos)})

    @app.route("/integracoes/whatsapp/enviar-grupos/status", methods=["GET", "OPTIONS"])
    def whatsapp_enviar_grupos_status():
        if request.method == "OPTIONS":
            return _options_resp()
        try:
            r = requests.get(f"{_WRX_SB_URL}/rest/v1/dx_config",
                             params={"chave": "eq.grupos_envio_status", "select": "valor"},
                             headers=_wrx_headers(), timeout=10)
            v = r.json()[0].get("valor") if (r.status_code == 200 and r.json()) else {}
        except Exception:
            v = {}
        return jsonify({"ok": True, "status": v or {}})

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
            if primeira_vez:
                avisados.add(key)
                return  # pós-restart: só registra, não envia
            if data_item is not None and not _recente(data_item):
                avisados.add(key)
                return  # antigo demais
            ok, _ = _waha_enviar(numero, texto)
            if ok:
                avisados.add(key)
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

    def _shopee_atualizar_preco_item(shop_id, item_id, preco):
        access_token, shop_id_int = _shopee_get_token(shop_id)
        if not access_token:
            return False, "token invalido"
        ts = int(time.time())
        path = "/api/v2/product/update_price"
        sign = _shopee_sign(path, ts, access_token, shop_id_int)
        r = requests.post(
            f"{_SHOPEE_BASE}{path}",
            params={"partner_id": SHOPEE_PARTNER_ID, "timestamp": ts,
                    "access_token": access_token, "shop_id": shop_id_int, "sign": sign},
            json={"item_id": int(item_id), "price_list": [{"model_id": 0, "original_price": float(preco)}]},
            timeout=20
        )
        d = r.json()
        ok = r.status_code == 200 and not d.get("error")
        if ok:
            requests.patch(
                f"{_WRX_SB_URL}/rest/v1/shopee_anuncios?shop_id=eq.{shop_id}&item_id=eq.{item_id}",
                headers=_wrx_headers(),
                json={"preco": float(preco), "sync_at": _datetime.utcnow().isoformat() + "Z"},
                timeout=10
            )
        return ok, d.get("message", "") if not ok else ""

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

    _shopee_cat_folhas_cache = {}  # shop_id_int -> (set(folhas), {parent_id: [child_ids]})
    _shopee_cat_nomes_cache = {}   # shop_id_int -> {category_id: nome}

    def _shopee_categorias_folha(access_token, shop_id_int):
        """(set de category_id FOLHA, mapa parent->filhos) da loja.
        A Shopee so aceita add_item em categoria-folha. Cacheia por loja."""
        cache = _shopee_cat_folhas_cache.get(shop_id_int)
        if cache is not None:
            return cache
        folhas = set()
        filhos = {}
        nomes = {}
        try:
            ts = int(time.time())
            path = "/api/v2/product/get_category"
            sign = _shopee_sign(path, ts, access_token, shop_id_int)
            r = requests.get(f"{_SHOPEE_BASE}{path}",
                params={"partner_id": SHOPEE_PARTNER_ID, "timestamp": ts,
                        "access_token": access_token, "shop_id": shop_id_int,
                        "sign": sign, "language": "pt-br"}, timeout=25)
            d = r.json()
            lista = (d.get("response", {}) or {}).get("category_list", []) or []
            for c in lista:
                cid = c.get("category_id")
                if not cid:
                    continue
                nomes[cid] = c.get("display_category_name") or c.get("original_category_name") or ""
                pid = c.get("parent_category_id")
                if pid is not None:
                    filhos.setdefault(pid, []).append(cid)
                # has_children=False => é folha. Aceita variações de nome do campo.
                tem_filho = c.get("has_children")
                if tem_filho is None:
                    tem_filho = c.get("has_child")
                if not tem_filho:
                    folhas.add(cid)
        except Exception as _e:
            print(f"[SHOPEE-CAT] erro get_category: {_e}")
        if folhas:
            _shopee_cat_folhas_cache[shop_id_int] = (folhas, filhos)
            _shopee_cat_nomes_cache[shop_id_int] = nomes
        return (folhas, filhos)

    def _shopee_descer_ate_folha(cat_id, folhas, filhos, _prof=0):
        """Se cat_id nao for folha, desce pelo 1o filho ate achar uma folha."""
        if _prof > 12 or not cat_id:
            return None
        if cat_id in folhas:
            return cat_id
        for f in filhos.get(cat_id, []):
            r = _shopee_descer_ate_folha(f, folhas, filhos, _prof + 1)
            if r:
                return r
        return None

    def _shopee_categoria_recomendada(access_token, shop_id_int, nome_produto):
        """Pergunta à Shopee qual a categoria-folha certa pro produto (recommend_category).
        Retorna o category_id (int) ou None. SEMPRE valida que e categoria-FOLHA, senao
        o add_item rejeita com 'should use leaf category' (causava algumas variacoes
        falharem enquanto outras passavam, pq o ranking muda com o texto do titulo)."""
        tentativas = []
        bruto = str(nome_produto or "").strip()
        if bruto:
            # Normaliza p/ TODAS as variacoes do mesmo produto pegarem a MESMA categoria:
            # remove os sufixos de marketplace (Pronta Entrega/Envio Imediato/Nota Fiscal)
            # antes de qualquer coisa, senao cada variacao recomenda categoria diferente.
            bruto = _re.sub(r"\b(pronta entrega|envio imediato|nota fiscal|frete gr[aá]tis)\b", " ", bruto, flags=_re.IGNORECASE)
            bruto = _re.sub(r"\s+", " ", bruto).strip()
            tentativas.append(bruto)
            limpo = _re.sub(r"\([^)]*\)", " ", bruto)
            limpo = _re.sub(r"\b\d{4}\b", " ", limpo)
            limpo = _re.sub(r"[/\-–—]", " ", limpo)
            limpo = _re.sub(r"\b(usad[ao]|novo|original|manual|elétrico|eletrico|lado|direito|esquerdo)\b", " ", limpo, flags=_re.IGNORECASE)
            limpo = _re.sub(r"\s+", " ", limpo).strip()
            if limpo and limpo not in tentativas:
                tentativas.append(limpo)
            if "retrovisor" in bruto.lower() and "retrovisor" not in limpo.lower():
                tentativas.append("retrovisor automotivo")
        folhas, filhos = _shopee_categorias_folha(access_token, shop_id_int)
        try:
            path = "/api/v2/product/category_recommend"
            for nome in tentativas or [str(nome_produto or "")[:120]]:
                ts = int(time.time())
                sign = _shopee_sign(path, ts, access_token, shop_id_int)
                r = requests.get(
                    f"{_SHOPEE_BASE}{path}",
                    params={"partner_id": SHOPEE_PARTNER_ID, "timestamp": ts,
                            "access_token": access_token, "shop_id": shop_id_int,
                            "sign": sign, "item_name": nome[:120]},
                    timeout=15
                )
                d = r.json()
                cats = (d.get("response", {}) or {}).get("category_id", []) or []
                if not cats:
                    continue
                # A API devolve uma lista. NEM SEMPRE o 1º é folha. Pega o 1º que SEJA folha.
                if folhas:
                    nomes = _shopee_cat_nomes_cache.get(shop_id_int, {})
                    candidatos = [c for c in cats if c in folhas]
                    # EVITA categorias "Tuning": são as únicas que EXIGEM compatibilidade de
                    # veículo (a API de atributos hoje está suspensa e não deixa preencher).
                    # O integrador de referência (PartHub) usa as categorias específicas/"Outros",
                    # que ficam QUALIFICADAS sem compatibilidade. Só evita se houver alternativa.
                    nao_tuning = [c for c in candidatos if "tuning" not in str(nomes.get(c, "")).lower()]
                    if nao_tuning:
                        return nao_tuning[0]
                    if candidatos:
                        return candidatos[0]
                    # Nenhum recomendado e folha: DESCE a arvore a partir dos recomendados
                    # (do mais especifico p/ o mais generico) ate achar uma folha real.
                    for c in reversed(cats):
                        leaf = _shopee_descer_ate_folha(c, folhas, filhos)
                        if leaf:
                            return leaf
                else:
                    # Sem arvore de categorias (falhou get_category): mantem o comportamento antigo.
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
        # MANTÉM A ORDEM ORIGINAL das fotos: a 1ª é a CAPA escolhida pelo usuário (a Shopee
        # usa a 1ª imagem como foto principal). Antes o código REORDENAVA por "tipo"
        # (data/partshub/supabase) e isso trocava a foto principal por uma secundária.
        fotos_ordem = [str(u or "").strip() for u in (urls or [])[:9] if str(u or "").strip()]
        for u in fotos_ordem:
            if not u:
                continue
            try:
                # FOTO EDITADA vem em base64 (data:image/...): decodifica direto.
                # Antes só fazia requests.get(url) e o data: falhava -> nenhuma foto subia -> publicação falhava.
                if str(u).startswith("data:"):
                    import base64 as _b64img
                    try:
                        content = _b64img.b64decode(str(u).split(",", 1)[1])
                    except Exception as _ed:
                        print(f"[SHOPEE-IMG] base64 invalido: {_ed}")
                        continue
                else:
                    u = u.replace("http://", "https://")
                    img = requests.get(u, timeout=20)
                    if img.status_code != 200 or not img.content:
                        print(f"[SHOPEE-IMG] falha baixar {u[:60]} status={img.status_code}")
                        continue
                    content = img.content
                # A Shopee NAO aceita webp (as fotos vindas do Mercado Livre vem em webp).
                # Converte qualquer formato (webp/png/etc) para JPEG real antes de subir.
                try:
                    from PIL import Image as _PILImg
                    import io as _ioimg
                    _im = _PILImg.open(_ioimg.BytesIO(content)).convert("RGB")
                    _buf = _ioimg.BytesIO()
                    _im.save(_buf, format="JPEG", quality=90)
                    content = _buf.getvalue()
                except Exception as _ec:
                    print(f"[SHOPEE-IMG] falha conversao p/ jpeg (segue com original): {_ec}")
                ts = int(time.time())
                path = "/api/v2/media_space/upload_image"
                sign = _shopee_sign(path, ts, access_token, shop_id_int)
                r = requests.post(
                    f"{_SHOPEE_BASE}{path}",
                    params={"partner_id": SHOPEE_PARTNER_ID, "timestamp": ts,
                            "access_token": access_token, "shop_id": shop_id_int, "sign": sign},
                    files={"image": ("foto.jpg", content, "image/jpeg")},
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

    @app.route("/integracoes/shopee/diag-item", methods=["GET"])
    def shopee_diag_item():
        """Diagnóstico: detalhes COMPLETOS de um anúncio existente (categoria, atributos,
        compatibilidade) — usado para descobrir como um integrador que funciona (PartHub)
        montou o anúncio e replicar a categoria/compatibilidade correta."""
        item_id = request.args.get("item_id", "")
        sid = request.args.get("shop_id", "")
        tokens = _shopee_load_tokens()
        if not sid:
            sid = list(tokens.keys())[0] if tokens else ""
        access_token, shop_id_int = _shopee_get_token(sid)
        if not access_token:
            return jsonify({"erro": "sem token", "shop": sid}), 400
        if not item_id:
            return jsonify({"erro": "item_id obrigatorio"}), 400
        out = {"item_id": item_id, "shop_id": sid}
        try:
            dets = _shopee_get_item_details(access_token, shop_id_int, [int(item_id)])
            it = dets[0] if dets else {}
            out["category_id"] = it.get("category_id")
            out["attribute_list"] = it.get("attribute_list")
            out["brand"] = it.get("brand")
            out["condition"] = it.get("condition")
            out["compatibility_info"] = it.get("compatibility_info")  # como o integrador que funciona montou a compat
            out["raw_keys"] = sorted(list(it.keys()))
            # nomes das categorias relevantes (a usada + as recomendadas pelo titulo)
            try:
                folhas, filhos = _shopee_categorias_folha(access_token, shop_id_int)
                nomes = {}
                ts2 = int(time.time())
                p2 = "/api/v2/product/get_category"
                s2 = _shopee_sign(p2, ts2, access_token, shop_id_int)
                rc = requests.get(f"{_SHOPEE_BASE}{p2}", params={"partner_id": SHOPEE_PARTNER_ID, "timestamp": ts2, "access_token": access_token, "shop_id": shop_id_int, "sign": s2, "language": "pt-br"}, timeout=25)
                for c in (rc.json().get("response", {}) or {}).get("category_list", []) or []:
                    nomes[c.get("category_id")] = {"nome": c.get("display_category_name") or c.get("original_category_name"), "folha": not (c.get("has_children") or c.get("has_child"))}
                alvos = [it.get("category_id"), 102431, 102528, 102242]
                out["categorias_nomes"] = {str(a): nomes.get(a) for a in alvos}
            except Exception as _ec:
                out["categorias_nomes_err"] = str(_ec)
        except Exception as e:
            out["erro_detalhe"] = str(e)
        # Compatibilidade de veículo costuma vir de uma API separada — tenta buscar.
        for path in ("/api/v2/product/get_item_compatibility", "/api/v2/product/get_compatibility"):
            try:
                ts = int(time.time())
                sign = _shopee_sign(path, ts, access_token, shop_id_int)
                r = requests.get(f"{_SHOPEE_BASE}{path}",
                    params={"partner_id": SHOPEE_PARTNER_ID, "timestamp": ts,
                            "access_token": access_token, "shop_id": shop_id_int,
                            "sign": sign, "item_id": int(item_id)}, timeout=20)
                out["compat_" + path.split("/")[-1]] = r.json()
            except Exception as e:
                out["compat_err_" + path.split("/")[-1]] = str(e)
        return jsonify(out)

    # === #4 ORQUESTRADOR: publica em TODAS as plataformas EM BACKGROUND (no servidor) ===
    # Recebe os payloads JA MONTADOS pelo front e repassa pros endpoints que ja existem,
    # em sequencia (ML -> Shopee -> OLX). Roda em thread daemon: NAO para se a aba fechar.
    # body: { sku, quem, simular:bool, mercadolivre:{...}|None, shopee:{...}|None, olx:{...}|None }
    # simular=True NAO publica de verdade (so registra os passos) — pra testar com seguranca.
    def _pubjob_set(job_id, data):
        try:
            requests.post(
                f"{_WRX_SB_URL}/rest/v1/dx_config",
                headers={**_wrx_headers(), "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates"},
                json={"chave": "pubjob:" + str(job_id), "valor": data}, timeout=15,
            )
        except Exception as _e:
            print(f"[PUBLICAR-TUDO] erro ao gravar status: {_e}")

    @app.route("/integracoes/publicar-tudo", methods=["POST", "OPTIONS"])
    def publicar_tudo():
        if request.method == "OPTIONS":
            return _options_resp()
        body = request.get_json(force=True, silent=True) or {}
        simular = bool(body.get("simular", False))
        sku = str(body.get("sku") or "")
        quem = str(body.get("quem") or "")
        alvos = []  # ordem fixa: ML, depois Shopee, depois OLX
        for plat in ("mercadolivre", "shopee", "olx"):
            if body.get(plat) is not None:
                alvos.append((plat, body.get(plat)))
        if not alvos:
            return jsonify({"ok": False, "erro": "nenhuma plataforma no pacote"}), 400
        job_id = (sku or "s") + "-" + str(int(time.time() * 1000))
        plats = [a[0] for a in alvos]
        _pubjob_set(job_id, {"sku": sku, "quem": quem, "status": "iniciando",
                             "simular": simular, "plataformas": plats, "resultados": {}})

        def _worker():
            resultados = {}
            for plat, payload in alvos:
                # cada plataforma pode ter VARIOS anuncios (Shopee=8, ML=N planos, OLX=1)
                itens = payload if isinstance(payload, list) else [payload]
                res = []
                for item in itens:
                    try:
                        if simular:
                            time.sleep(0.2)
                            res.append({"ok": True, "simulado": True})
                        else:
                            r = requests.post(f"http://127.0.0.1:{PORT}/integracoes/{plat}/publicar",
                                              json=item, timeout=300)
                            res.append({"ok": bool(r.ok), "status": r.status_code, "resp": (r.text or "")[:300]})
                    except Exception as e:
                        res.append({"ok": False, "erro": str(e)})
                resultados[plat] = {"total": len(itens), "ok": sum(1 for x in res if x.get("ok")), "itens": res}
                _pubjob_set(job_id, {"sku": sku, "quem": quem, "status": "publicando",
                                     "simular": simular, "plataformas": plats, "resultados": resultados})
            _pubjob_set(job_id, {"sku": sku, "quem": quem, "status": "concluido",
                                 "simular": simular, "plataformas": plats, "resultados": resultados})

        threading.Thread(target=_worker, daemon=True).start()
        return jsonify({"ok": True, "job_id": job_id, "simular": simular, "plataformas": plats})

    @app.route("/integracoes/publicar-tudo/status", methods=["GET", "OPTIONS"])
    def publicar_tudo_status():
        if request.method == "OPTIONS":
            return _options_resp()
        job_id = request.args.get("job_id", "")
        try:
            r = requests.get(
                f"{_WRX_SB_URL}/rest/v1/dx_config?chave=eq.pubjob:{job_id}&select=valor",
                headers=_wrx_headers(), timeout=15,
            )
            rows = r.json() if r.ok else []
            return jsonify(rows[0]["valor"] if rows else {"status": "desconhecido"})
        except Exception as e:
            return jsonify({"status": "erro", "erro": str(e)})

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

        # Publica só na loja solicitada. Se vier shop_ids, aceita lista explícita.
        # Sem shop_id, usa apenas a primeira loja autorizada para evitar duplicação em massa.
        shop_id_param = data.get("shop_id")
        shop_ids_param = data.get("shop_ids")
        if isinstance(shop_ids_param, list):
            shop_ids = [str(s).strip() for s in shop_ids_param if str(s).strip()]
        elif shop_id_param:
            shop_ids = [str(shop_id_param).strip()]
        else:
            shop_ids = [next(iter(tokens.keys()))] if tokens else []
        resultados = []
        erros = []
        for sid in shop_ids:
            access_token, shop_id_int = _shopee_get_token(sid)
            if not access_token:
                erros.append(f"shop {sid}: token invalido")
                continue
            # EVITA DUPLICAR a mesma copia. O item_sku continua sendo o SKU base;
            # a referencia SH1..SH6 fica separada no Auto-Part Number.
            if not data.get("forcar"):
                try:
                    # IMPORTANTE: ignora anuncios DELETED. Senao uma variacao (SH3/SH4) que ja
                    # foi publicada e depois deletada bloqueia a republicacao ("ja_existia"),
                    # fazendo a Shopee mandar so 2 das 4 variacoes.
                    _chk = requests.get(
                        f"{_WRX_SB_URL}/rest/v1/shopee_anuncios?sku=eq.{sku_interno}&shop_id=eq.{sid}&status=neq.DELETED&select=item_id,titulo",
                        headers=_auth_sb_headers(), timeout=10)
                    _existentes = _chk.json() if _chk.status_code == 200 else []
                    _ids_existentes = [x.get("item_id") for x in _existentes if x.get("item_id")]
                    if _ids_existentes:
                        _detalhes = _shopee_get_item_details(access_token, shop_id_int, _ids_existentes)
                        _por_id = {str(x.get("item_id")): x for x in _detalhes}
                        _duplicado = None
                        for _row in _existentes:
                            _det = _por_id.get(str(_row.get("item_id")), {})
                            _ref_publicada = _shopee_extract_part_number(_det)
                            if _ref_publicada and _ref_publicada.strip().upper() == str(sku).strip().upper():
                                _duplicado = _row
                                break
                            # Compatibilidade com o primeiro anuncio criado antes de
                            # a referencia exclusiva existir. Nao aplica a SH2+ para nao
                            # bloquear variacoes que eventualmente tenham titulo igual.
                            if (not _ref_publicada and str(sku).upper().endswith("-SH1")
                                    and str(_row.get("titulo") or "").strip() == str(titulo).strip()):
                                _duplicado = _row
                                break
                        if _duplicado:
                            resultados.append({
                                "shop_id": sid,
                                "item_id": _duplicado.get("item_id"),
                                "ja_existia": True,
                            })
                            continue
                except Exception:
                    pass
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
                # Todas as copias pertencem ao mesmo produto no estoque.
                "item_sku": str(sku_interno)[:50],
                "description": descricao[:2000],
                "original_price": float(preco),
                "seller_stock": [{"stock": 1}],
                "condition": condicao_shopee,
                "category_id": cat_id,
                "brand": {"brand_id": 0, "original_brand_name": "NoBrand"},  # autopeça usada: sem marca
                "image": {"image_id_list": image_ids},
                "logistic_info": logistics,
                # "Auto-Part Number" (102293) REMOVIDO (15/06/2026): a Shopee passou a rejeitar este
                # atributo ("The attribute Auto-Part Number(102293) is not mapped with the category.
                # Please remove it.") — estava quebrando TODA publicação Shopee. Diagnóstico via teste real.
                "attribute_list": [],
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
                def _do_add(_pl):
                    _ts2 = int(time.time())
                    _sg2 = _shopee_sign(path, _ts2, access_token, shop_id_int)
                    _rr = requests.post(
                        f"{_SHOPEE_BASE}{path}",
                        params={"partner_id": SHOPEE_PARTNER_ID, "timestamp": _ts2, "access_token": access_token, "shop_id": shop_id_int, "sign": _sg2},
                        json=_pl, timeout=20)
                    return _rr, _rr.json()
                _r, _d = _do_add(payload_shopee)
                # Algumas categorias EXIGEM o "Auto-Part Number" (102293); outras o rejeitam.
                # Tentamos sem; se a Shopee disser que é obrigatório, mandamos o número da peça e
                # tentamos de novo. Assim cobre os dois casos sem buscar a API de atributos (bloqueada).
                _m0 = str(_d.get("message", "")).lower()
                if _d.get("error") and "auto-part number" in _m0 and ("mandatory" in _m0 or "required" in _m0):
                    _pn = str(data.get("oem") or sku_interno or sku or "").strip()
                    if _pn:
                        payload_shopee["attribute_list"] = [{
                            "attribute_id": 102293,
                            "attribute_value_list": [{"value_id": 0, "original_value_name": _pn[:100]}]
                        }]
                        _r, _d = _do_add(payload_shopee)
                if _r.status_code == 200 and not _d.get("error"):
                    item_id = _d.get("response", {}).get("item_id", "")
                    print(f"[SHOPEE] SKU '{sku}' publicado no shop {sid}. item_id={item_id}, condicao={condicao_shopee}")
                    resultados.append({"shop_id": sid, "item_id": item_id, "condicao": condicao_shopee})
                    # Grava JÁ na shopee_anuncios (como o ML faz) — senão o anúncio nao aparece
                    # no painel do produto até o sync rodar. Chave SKU = sku_interno (base da peça).
                    try:
                        _sku_base = str(sku_interno or sku or "").strip().upper()
                        if item_id and _sku_base:
                            requests.post(
                                f"{_WRX_SB_URL}/rest/v1/shopee_anuncios?on_conflict=shop_id,item_id",
                                headers={**_wrx_headers(), "Prefer": "resolution=merge-duplicates"},
                                json=[{
                                    "shop_id": str(sid), "item_id": str(item_id), "sku": _sku_base,
                                    "titulo": str(titulo)[:200], "preco": float(preco or 0),
                                    "estoque": 1, "status": "NORMAL",
                                    # Guarda as URLs de origem para o painel interno e histórico.
                                    # A Shopee usa os image_id da publicação, mas o painel precisa
                                    # mostrar o que foi enviado sem depender do sync posterior.
                                    "fotos": [str(u).strip() for u in (fotos or []) if str(u).strip()]
                                }],
                                timeout=15
                            )
                    except Exception as _eg:
                        print(f"[SHOPEE] aviso: nao gravou shopee_anuncios: {_eg}")
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

    def _shopee_extract_part_number(item):
        """Referencia exclusiva da copia (SH1..SH6), sem alterar o SKU de estoque."""
        for attr in item.get("attribute_list", []):
            if (attr.get("attribute_id") == 102293
                    or attr.get("attribute_name", "").strip().lower() == "auto-part number"):
                vals = attr.get("attribute_value_list", [])
                if vals:
                    return str(
                        vals[0].get("original_value_name")
                        or vals[0].get("value")
                        or vals[0].get("display_value_name")
                        or ""
                    ).strip()
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

    @app.route("/integracoes/shopee/violacoes", methods=["GET", "OPTIONS"])
    def shopee_violacoes():
        # Lista produtos em VIOLACAO (BANNED) das lojas Shopee, com nome+SKU.
        # O MOTIVO de cada um a API nao devolve confiavel -> ver no Seller Centre.
        if request.method == "OPTIONS":
            return _options_resp()
        tokens = _shopee_load_tokens()
        if not tokens:
            return jsonify({"ok": False, "erro": "Shopee nao autorizada"}), 401
        statuses = (request.args.get("status") or "BANNED").upper().split(",")
        todos = []
        for sid in list(tokens.keys()):
            access_token, shop_id_int = _shopee_get_token(sid)
            if not access_token:
                continue
            for st in statuses:
                refs = _shopee_list_items_all(access_token, shop_id_int, status=st.strip())
                ids = [x["item_id"] for x in refs if x.get("item_id")]
                if not ids:
                    continue
                for item in _shopee_get_item_details(access_token, shop_id_int, ids):
                    todos.append({
                        "shop_id": sid,
                        "item_id": item.get("item_id"),
                        "titulo": item.get("item_name", ""),
                        "sku": _shopee_extract_sku(item),
                        "status": item.get("item_status", st),
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
                sku_base = re.sub(r'-(SH|DML|GML)\d+$', '', sku_raw, flags=re.I)
                sku_up = sku_base.upper() if sku_base else ""
                sku_vinculado = sku_base if sku_up in skus_sistema else None
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

    @app.route("/integracoes/estoque/sincronizar-preco", methods=["POST", "OPTIONS"])
    def estoque_sincronizar_preco():
        if request.method == "OPTIONS":
            return _options_resp()
        data = request.get_json(force=True) or {}
        sku = str(data.get("sku") or "").strip()
        try:
            preco = float(data.get("preco", 0))
        except (TypeError, ValueError):
            preco = 0.0
        if not sku:
            return jsonify({"ok": False, "erro": "sku obrigatorio"}), 400
        if preco < 0:
            return jsonify({"ok": False, "erro": "preco invalido"}), 400

        avisos = []
        atualizado = {"produto": False, "ml": [], "shopee": []}

        try:
            r_prod = requests.patch(
                f"{_WRX_SB_URL}/rest/v1/pecas_estoque?sku=eq.{sku}",
                headers={**_wrx_headers(), "Content-Type": "application/json", "Prefer": "return=minimal"},
                json={"preco": preco, "atualizado": _datetime.utcnow().isoformat() + "Z"},
                timeout=15,
            )
            atualizado["produto"] = r_prod.status_code in (200, 204)
        except Exception as e:
            avisos.append(f"estoque: {e}")

        try:
            r_ml = requests.get(
                f"{_WRX_SB_URL}/rest/v1/ml_anuncios",
                params={"select": "ml_id,conta,sku", "sku": f"eq.{sku}"},
                headers=_wrx_headers(),
                timeout=15,
            )
            for row in (r_ml.json() if r_ml.status_code == 200 else []):
                ml_id = str(row.get("ml_id") or "").strip()
                conta = str(row.get("conta") or "default").strip()
                if not ml_id:
                    continue
                ok, err = _ml_atualizar_preco_item(ml_id, conta, preco)
                atualizado["ml"].append({"ml_id": ml_id, "conta": conta, "ok": ok, "erro": err})
                if not ok:
                    avisos.append(f"ML {ml_id}: {err}")
                else:
                    try:
                        requests.patch(
                            f"{_WRX_SB_URL}/rest/v1/ml_anuncios?ml_id=eq.{ml_id}",
                            headers={**_wrx_headers(), "Content-Type": "application/json", "Prefer": "return=minimal"},
                            json={"preco": preco, "sync_at": _datetime.utcnow().isoformat() + "Z"},
                            timeout=10,
                        )
                    except Exception:
                        pass
        except Exception as e:
            avisos.append(f"ml: {e}")

        try:
            r_sh = requests.get(
                f"{_WRX_SB_URL}/rest/v1/shopee_anuncios",
                params={"select": "shop_id,item_id,sku", "sku": f"eq.{sku}"},
                headers=_wrx_headers(),
                timeout=15,
            )
            for row in (r_sh.json() if r_sh.status_code == 200 else []):
                sid = str(row.get("shop_id") or "").strip()
                item_id = str(row.get("item_id") or "").strip()
                if not sid or not item_id:
                    continue
                ok, err = _shopee_atualizar_preco_item(sid, item_id, preco)
                atualizado["shopee"].append({"shop_id": sid, "item_id": item_id, "ok": ok, "erro": err})
                if not ok:
                    avisos.append(f"Shopee {sid}/{item_id}: {err}")
        except Exception as e:
            avisos.append(f"shopee: {e}")

        return jsonify({
            "ok": True,
            "sku": sku,
            "preco": preco,
            "atualizado": atualizado,
            "avisos": avisos,
        })

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
                # Item sem variacao usa item_sku; anuncios antigos podem ter -SH1..-SH6.
                sku_venda = (item.get("model_sku") or item.get("item_sku")
                             or item.get("seller_sku") or "").strip()
                sku = re.sub(r'-(SH|DML|GML)\d+$', '', sku_venda, flags=re.I)
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

    @app.route("/integracoes/finalizar-sku", methods=["POST", "OPTIONS"])
    def finalizar_sku_todas_plataformas():
        """Encerra todos os anuncios ML e Shopee vinculados ao SKU."""
        if request.method == "OPTIONS":
            return _options_resp()
        data = request.get_json(force=True) or {}
        sku = str(data.get("sku") or "").strip().upper()
        confirmacao = str(data.get("confirmacao") or "").strip().upper()
        if not sku:
            return jsonify({"ok": False, "erro": "sku obrigatorio"}), 400
        if confirmacao != sku:
            return jsonify({"ok": False, "erro": "confirmacao do SKU invalida"}), 400

        resultado = {"ml": [], "shopee": []}
        try:
            # Os anuncios sao gravados com SKU SUFIXADO por conta/variacao (ex 109437-DML1, 109437-GML2).
            # Buscar por "eq.{sku}" (exato) NAO achava nada -> "nenhum anuncio vinculado". Casa o SKU base
            # exato OU base + sufixo "-XXXn". URL montada na mao p/ o wildcard '*' chegar literal ao PostgREST.
            r_ml = requests.get(
                f"{_WRX_SB_URL}/rest/v1/ml_anuncios?select=ml_id,conta,status&or=(sku.eq.{sku},sku.like.{sku}-*)",
                headers=_wrx_headers(), timeout=15,
            )
            anuncios_ml = r_ml.json() if r_ml.status_code == 200 else []
        except Exception as e:
            anuncios_ml = []
            resultado["ml"].append({"ok": False, "erro": f"consulta: {e}"})

        for anuncio in anuncios_ml:
            ml_id = str(anuncio.get("ml_id") or "").strip()
            conta = str(anuncio.get("conta") or "default").strip()
            if not ml_id:
                continue
            if str(anuncio.get("status") or "").lower() == "closed":
                resultado["ml"].append({"ok": True, "id": ml_id, "conta": conta, "ja_finalizado": True})
                continue
            token = _ml_get_user_token(conta)
            if not token:
                resultado["ml"].append({"ok": False, "id": ml_id, "conta": conta, "erro": "conta sem token"})
                continue
            headers_ml = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            try:
                requests.put(
                    f"https://api.mercadolibre.com/items/{ml_id}",
                    headers=headers_ml,
                    json={"status": "paused", "available_quantity": 0},
                    timeout=15,
                )
                r = requests.put(
                    f"https://api.mercadolibre.com/items/{ml_id}",
                    headers=headers_ml,
                    json={"status": "closed"},
                    timeout=15,
                )
                ok = r.status_code == 200
                erro = "" if ok else f"ML {r.status_code}: {r.text[:180]}"
                resultado["ml"].append({"ok": ok, "id": ml_id, "conta": conta, "erro": erro})
                if ok:
                    requests.patch(
                        f"{_WRX_SB_URL}/rest/v1/ml_anuncios",
                        params={"ml_id": f"eq.{ml_id}", "conta": f"eq.{conta}"},
                        headers=_wrx_headers(),
                        json={"status": "closed", "estoque": 0},
                        timeout=10,
                    )
            except Exception as e:
                resultado["ml"].append({"ok": False, "id": ml_id, "conta": conta, "erro": str(e)})

        try:
            r_sh = requests.get(
                f"{_WRX_SB_URL}/rest/v1/shopee_anuncios?select=shop_id,item_id,status&or=(sku.eq.{sku},sku.like.{sku}-*)",
                headers=_wrx_headers(), timeout=15,
            )
            anuncios_sh = r_sh.json() if r_sh.status_code == 200 else []
        except Exception as e:
            anuncios_sh = []
            resultado["shopee"].append({"ok": False, "erro": f"consulta: {e}"})

        for anuncio in anuncios_sh:
            sid = str(anuncio.get("shop_id") or "").strip()
            item_id = str(anuncio.get("item_id") or "").strip()
            if not sid or not item_id:
                continue
            if str(anuncio.get("status") or "").upper() == "DELETED":
                resultado["shopee"].append({"ok": True, "id": item_id, "shop_id": sid, "ja_finalizado": True})
                continue
            access_token, shop_id_int = _shopee_get_token(sid)
            if not access_token:
                resultado["shopee"].append({"ok": False, "id": item_id, "shop_id": sid, "erro": "loja sem token"})
                continue
            try:
                ts = int(time.time())
                path = "/api/v2/product/delete_item"
                sign = _shopee_sign(path, ts, access_token, shop_id_int)
                r = requests.post(
                    f"{_SHOPEE_BASE}{path}",
                    params={"partner_id": SHOPEE_PARTNER_ID, "timestamp": ts,
                            "access_token": access_token, "shop_id": shop_id_int, "sign": sign},
                    json={"item_id": int(item_id)}, timeout=20,
                )
                d = r.json()
                ok = r.status_code == 200 and not d.get("error")
                erro = d.get("message", "") if not ok else ""
                if not ok:
                    ts = int(time.time())
                    path = "/api/v2/product/unlist_item"
                    sign = _shopee_sign(path, ts, access_token, shop_id_int)
                    r = requests.post(
                        f"{_SHOPEE_BASE}{path}",
                        params={"partner_id": SHOPEE_PARTNER_ID, "timestamp": ts,
                                "access_token": access_token, "shop_id": shop_id_int, "sign": sign},
                        json={"item_list": [{"item_id": int(item_id), "unlist": True}]},
                        timeout=20,
                    )
                    d = r.json()
                    ok = r.status_code == 200 and not d.get("error")
                    erro = "" if ok else d.get("message", r.text[:180])
                resultado["shopee"].append({"ok": ok, "id": item_id, "shop_id": sid, "erro": erro})
                if ok:
                    requests.patch(
                        f"{_WRX_SB_URL}/rest/v1/shopee_anuncios",
                        params={"shop_id": f"eq.{sid}", "item_id": f"eq.{item_id}"},
                        headers=_wrx_headers(),
                        json={"status": "DELETED", "estoque": 0,
                              "sync_at": _datetime.utcnow().isoformat() + "Z"},
                        timeout=10,
                    )
            except Exception as e:
                resultado["shopee"].append({"ok": False, "id": item_id, "shop_id": sid, "erro": str(e)})

        todos = resultado["ml"] + resultado["shopee"]
        sucessos = sum(1 for item in todos if item.get("ok"))
        falhas = sum(1 for item in todos if not item.get("ok"))
        return jsonify({
            "ok": falhas == 0,
            "sku": sku,
            "total": len(todos),
            "sucessos": sucessos,
            "falhas": falhas,
            "resultado": resultado,
        })

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

    # ── LOGIN DE FUNCIONÁRIO (usuário + senha, validado NO SERVIDOR) ──────────────
    # Senha guardada com HASH (sha256+salt) no Supabase. Admin define as senhas.
    import hashlib as _hashlib_auth
    _AUTH_SALT = os.environ.get("AUTH_SALT", "dx-wrx-2026-salt-troque-isso")
    _ADMIN_SENHA = os.environ.get("ADMIN_SENHA", "admin123")
    def _func_hash(senha):
        return _hashlib_auth.sha256((_AUTH_SALT + str(senha or "")).encode("utf-8")).hexdigest()
    def _auth_sb_headers():
        k = os.environ.get("SUPABASE_SERVICE_KEY") or _WRX_SB_KEY
        return {"apikey": k, "Authorization": f"Bearer {k}", "Content-Type": "application/json"}

    @app.route("/auth/func-setup", methods=["POST", "GET", "OPTIONS"])
    def auth_func_setup():
        if request.method == "OPTIONS":
            return _options_resp()
        service_key = os.environ.get("SUPABASE_SERVICE_KEY", "")
        sql = ("CREATE TABLE IF NOT EXISTS func_auth ("
               "usuario TEXT PRIMARY KEY, senha_hash TEXT NOT NULL, nome TEXT, funcao TEXT, "
               "criado_em TIMESTAMPTZ DEFAULT NOW());")
        if service_key:
            r = requests.post(f"{_WRX_SB_URL}/rest/v1/rpc/exec",
                              headers={"apikey": service_key, "Authorization": f"Bearer {service_key}", "Content-Type": "application/json"},
                              json={"sql": sql}, timeout=15)
            if r.status_code in (200, 201, 204):
                return jsonify({"ok": True, "msg": "Tabela func_auth criada"})
        return jsonify({"ok": False, "msg": "Rode o SQL no Supabase (SQL Editor) ou configure SUPABASE_SERVICE_KEY", "sql": sql})

    @app.route("/auth/func-login", methods=["POST", "OPTIONS"])
    def auth_func_login():
        if request.method == "OPTIONS":
            return _options_resp()
        d = request.get_json(force=True) or {}
        usuario = str(d.get("usuario") or "").strip().lower()
        senha = str(d.get("senha") or "")
        if not usuario or not senha:
            return jsonify({"ok": False, "erro": "Informe usuário e senha"}), 400
        try:
            r = requests.get(f"{_WRX_SB_URL}/rest/v1/func_auth?usuario=eq.{usuario}&select=usuario,senha_hash,nome,funcao",
                             headers=_auth_sb_headers(), timeout=12)
            rows = r.json() if r.status_code == 200 else []
        except Exception:
            rows = []
        if not rows or rows[0].get("senha_hash") != _func_hash(senha):
            return jsonify({"ok": False, "erro": "Usuário ou senha incorretos"}), 401
        u = rows[0]
        return jsonify({"ok": True, "usuario": u["usuario"], "nome": u.get("nome") or u["usuario"], "funcao": u.get("funcao") or ""})

    # ── Cadastro de usuários da EXTENSÃO (tabela usuarios_ext, login_ext) ─────────
    # A extensão valida com sha256(senha) SEM salt (diferente do func_auth do site).
    # Usa a service key (via _auth_sb_headers) pra furar o RLS da usuarios_ext.
    @app.route("/auth/ext-cadastrar", methods=["POST", "OPTIONS"])
    def auth_ext_cadastrar():
        if request.method == "OPTIONS":
            return _options_resp()
        d = request.get_json(force=True) or {}
        if str(d.get("admin") or "") != _ADMIN_SENHA:
            return jsonify({"ok": False, "erro": "Senha de admin incorreta"}), 401
        usuarios = d.get("usuarios") or []
        if not usuarios:
            return jsonify({"ok": False, "erro": "Lista de usuarios vazia"}), 400
        def _ext_hash(s):
            return _hashlib_auth.sha256(str(s or "").encode("utf-8")).hexdigest()
        ok, err = [], []
        for u in usuarios:
            email = str(u.get("email") or "").strip().lower()
            senha = str(u.get("senha") or "")
            nome = str(u.get("nome") or email)
            if not email or not senha:
                err.append({"email": email, "erro": "email/senha faltando"}); continue
            body = {"email": email, "senha_hash": _ext_hash(senha), "nome": nome}
            try:
                r = requests.post(f"{_WRX_SB_URL}/rest/v1/usuarios_ext?on_conflict=email",
                                  headers={**_auth_sb_headers(), "Prefer": "resolution=merge-duplicates"},
                                  json=body, timeout=12)
                if r.status_code in (200, 201, 204):
                    ok.append(email)
                else:
                    err.append({"email": email, "erro": f"{r.status_code} {r.text[:120]}"})
            except Exception as e:
                err.append({"email": email, "erro": str(e)[:120]})
        return jsonify({"ok": not err, "cadastrados": ok, "erros": err})

    @app.route("/auth/func-list", methods=["POST", "OPTIONS"])
    def auth_func_list():
        if request.method == "OPTIONS":
            return _options_resp()
        d = request.get_json(force=True) or {}
        if str(d.get("admin") or "") != _ADMIN_SENHA:
            return jsonify({"ok": False, "erro": "Senha de admin incorreta"}), 401
        r = requests.get(f"{_WRX_SB_URL}/rest/v1/func_auth?select=usuario,nome,funcao&order=nome.asc",
                         headers=_auth_sb_headers(), timeout=12)
        return jsonify({"ok": True, "funcionarios": r.json() if r.status_code == 200 else []})

    @app.route("/auth/func-set", methods=["POST", "OPTIONS"])
    def auth_func_set():
        if request.method == "OPTIONS":
            return _options_resp()
        d = request.get_json(force=True) or {}
        if str(d.get("admin") or "") != _ADMIN_SENHA:
            return jsonify({"ok": False, "erro": "Senha de admin incorreta"}), 401
        usuario = str(d.get("usuario") or "").strip().lower()
        nome = str(d.get("nome") or usuario).strip()
        senha = str(d.get("senha") or "")
        funcao = str(d.get("funcao") or "").strip()
        if not usuario or not senha:
            return jsonify({"ok": False, "erro": "Informe usuário e senha"}), 400
        rec = {"usuario": usuario, "senha_hash": _func_hash(senha), "nome": nome, "funcao": funcao}
        r = requests.post(f"{_WRX_SB_URL}/rest/v1/func_auth?on_conflict=usuario",
                          headers={**_auth_sb_headers(), "Prefer": "resolution=merge-duplicates"},
                          json=[rec], timeout=12)
        if r.status_code in (200, 201, 204):
            return jsonify({"ok": True, "msg": f"Login de {nome} salvo"})
        return jsonify({"ok": False, "erro": f"Erro ao salvar ({r.status_code}): {r.text[:200]}"}), 500

    @app.route("/auth/func-del", methods=["POST", "OPTIONS"])
    def auth_func_del():
        if request.method == "OPTIONS":
            return _options_resp()
        d = request.get_json(force=True) or {}
        if str(d.get("admin") or "") != _ADMIN_SENHA:
            return jsonify({"ok": False, "erro": "Senha de admin incorreta"}), 401
        usuario = str(d.get("usuario") or "").strip().lower()
        requests.delete(f"{_WRX_SB_URL}/rest/v1/func_auth?usuario=eq.{usuario}", headers=_auth_sb_headers(), timeout=12)
        return jsonify({"ok": True})

    # ── SILHUETAS de carro na NUVEM (recortes reaproveitaveis em QUALQUER aparelho) ──
    @app.route("/silhuetas/setup", methods=["POST", "GET", "OPTIONS"])
    def silhuetas_setup():
        if request.method == "OPTIONS":
            return _options_resp()
        service_key = os.environ.get("SUPABASE_SERVICE_KEY", "")
        sql = ("CREATE TABLE IF NOT EXISTS silhuetas ("
               "chave TEXT PRIMARY KEY, veiculo TEXT, ano TEXT, png TEXT NOT NULL, "
               "criado_em TIMESTAMPTZ DEFAULT NOW());")
        if service_key:
            r = requests.post(f"{_WRX_SB_URL}/rest/v1/rpc/exec",
                              headers={"apikey": service_key, "Authorization": f"Bearer {service_key}", "Content-Type": "application/json"},
                              json={"sql": sql}, timeout=15)
            if r.status_code in (200, 201, 204):
                return jsonify({"ok": True, "msg": "Tabela silhuetas criada"})
        return jsonify({"ok": False, "msg": "Rode o SQL no Supabase (SQL Editor) ou configure SUPABASE_SERVICE_KEY", "sql": sql})

    @app.route("/silhuetas/listar", methods=["GET", "OPTIONS"])
    def silhuetas_listar():
        if request.method == "OPTIONS":
            return _options_resp()
        try:
            r = requests.get(f"{_WRX_SB_URL}/rest/v1/silhuetas?select=chave,veiculo,ano,png&order=criado_em.desc&limit=60",
                             headers=_auth_sb_headers(), timeout=20)
            return jsonify({"ok": r.status_code == 200, "silhuetas": r.json() if r.status_code == 200 else []})
        except Exception as e:
            return jsonify({"ok": False, "silhuetas": [], "erro": str(e)})

    @app.route("/silhuetas/salvar", methods=["POST", "OPTIONS"])
    def silhuetas_salvar():
        if request.method == "OPTIONS":
            return _options_resp()
        d = request.get_json(force=True) or {}
        chave = str(d.get("chave") or "").strip()
        png = str(d.get("png") or "")
        if not chave or not png.startswith("data:"):
            return jsonify({"ok": False, "erro": "chave e png (data:) obrigatorios"}), 400
        rec = {"chave": chave, "veiculo": str(d.get("veiculo") or ""), "ano": str(d.get("ano") or ""), "png": png}
        try:
            r = requests.post(f"{_WRX_SB_URL}/rest/v1/silhuetas?on_conflict=chave",
                              headers={**_auth_sb_headers(), "Prefer": "resolution=merge-duplicates"},
                              json=[rec], timeout=25)
            ok = r.status_code in (200, 201, 204)
            return jsonify({"ok": ok, "erro": (None if ok else r.text[:200])}), (200 if ok else 502)
        except Exception as e:
            return jsonify({"ok": False, "erro": str(e)}), 500

    @app.route("/silhuetas/excluir", methods=["POST", "OPTIONS"])
    def silhuetas_excluir():
        if request.method == "OPTIONS":
            return _options_resp()
        d = request.get_json(force=True) or {}
        chave = str(d.get("chave") or "").strip()
        if chave:
            try:
                requests.delete(f"{_WRX_SB_URL}/rest/v1/silhuetas?chave=eq.{chave}", headers=_auth_sb_headers(), timeout=15)
            except Exception:
                pass
        return jsonify({"ok": True})

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
                try:
                    requests.post(
                        f"http://127.0.0.1:{PORT}/integracoes/whatsapp/processar-respostas-funcionarios",
                        timeout=60,
                    )
                except Exception as _e:
                    print(f"[CRON-RESPOSTAS-FUNC] erro: {_e}")
                time.sleep(120)  # 2 minutos
        t = threading.Thread(target=_loop, daemon=True)
        t.start()
        print("[STARTUP] cron WhatsApp ativo (checa novidades a cada 2 min)")

    def _cron_expedicao_loop():
        """Thread: a cada 10 min sincroniza o status REAL do ML com a fila de expedicao
        (tira da fila o que ja foi enviado/cancelado) — sem precisar abrir a tela."""
        import threading
        def _loop():
            time.sleep(75)  # espera o servidor subir
            while True:
                try:
                    requests.get(f"http://127.0.0.1:{PORT}/integracoes/expedicao-sync-status", timeout=240)
                except Exception as _e:
                    print(f"[CRON-EXPEDICAO] erro: {_e}")
                time.sleep(600)  # 10 minutos
        t = threading.Thread(target=_loop, daemon=True)
        t.start()
        print("[STARTUP] cron Expedicao ativo (sincroniza status ML a cada 10 min)")

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

    def _cron_keepalive_loop():
        """Thread: a cada 4 min faz uma consulta levissima ao Supabase pra o banco (plano free)
        nao 'dormir'. Sem isso, a 1a tela do dia leva ~15-20s (cold start); com isso, abre rapido."""
        import threading
        def _loop():
            time.sleep(30)  # espera o servidor subir
            while True:
                try:
                    requests.get(
                        f"{_WRX_SB_URL}/rest/v1/dx_config?select=chave&limit=1",
                        headers={"apikey": _WRX_SB_KEY, "Authorization": f"Bearer {_WRX_SB_KEY}"},
                        timeout=30,
                    )
                except Exception as _e:
                    print(f"[CRON-KEEPALIVE] erro: {_e}")
                time.sleep(240)  # 4 minutos
        t = threading.Thread(target=_loop, daemon=True)
        t.start()
        print("[STARTUP] cron keep-alive Supabase ativo (a cada 4 min, evita cold start)")

    def _cron_shopee_sync_loop():
        """Thread: a cada 30 min sincroniza os anuncios da Shopee (puxa item_id/sku/loja das 2 lojas
        pra a tabela shopee_anuncios). Sem isso a BOLINHA do Shopee no card ficava cinza mesmo com o
        anuncio no ar — a lista do servidor nao atualizava sozinha (a dona reclamou: 'nao ta sincronizando')."""
        import threading
        def _loop():
            time.sleep(120)  # espera o servidor subir (e nao colidir com publicacao)
            while True:
                try:
                    requests.post(f"http://127.0.0.1:{PORT}/integracoes/shopee/sincronizar",
                                  json={}, timeout=300)
                except Exception as _e:
                    print(f"[CRON-SHOPEE-SYNC] erro: {_e}")
                time.sleep(1800)  # 30 minutos
        t = threading.Thread(target=_loop, daemon=True)
        t.start()
        print("[STARTUP] cron Shopee sync ativo (atualiza anuncios/bolinha a cada 30 min)")

    def main():
        sys.stdout.reconfigure(encoding="utf-8", errors="replace") if hasattr(sys.stdout, "reconfigure") else None
        print(f"WRX-Search API Server - porta {PORT}")
        print(f"  http://localhost:{PORT}/buscar?q=CODIGO")
        print(f"  http://localhost:{PORT}/carros?q=fiat+uno")
        print(f"  http://localhost:{PORT}/ping")
        host = "0.0.0.0" if _IS_RAILWAY else "127.0.0.1"
        _cron_whatsapp_loop()
        _cron_shopee_etiquetas_loop()
        _cron_expedicao_loop()
        _cron_keepalive_loop()
        _cron_shopee_sync_loop()
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
