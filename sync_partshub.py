"""
Sincroniza dados do Parts Hub → cache local + Supabase pecas_estoque
Uso: python sync_partshub.py
"""
import requests, json, time, re, os, sys
from datetime import datetime, timezone

# ── Credenciais ───────────────────────────────────────────────────────────────
PH_HOST   = "iftzoceaalhpyckuznae.supabase.co"
PH_ANON   = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImlmdHpvY2VhYWxocHlja3V6bmFlIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NjA0MzMwNjcsImV4cCI6MjA3NjAwOTA2N30.VZY9NLFvRMX-lr9FQUlOkMfE0RfdGxk0HVpslxMYDYg"
PH_EMAIL  = "geisuaine2025@gmail.com"
PH_SENHA  = "Vitoria12$"
PH_COMPANY = "KLM6SNKys1kn8txwJ4VP"

SB_URL    = "https://uthsiihzpsgarargegcw.supabase.co"
SB_KEY    = "sb_publishable_gOQgHrv2IVRgbiVV2Myhzg_BmzCXmXe"

NETLIFY   = r"C:\Users\Geisuane\Desktop\netlify-deploy"

# ── Autenticação Parts Hub ────────────────────────────────────────────────────
def ph_get_jwt():
    r = requests.post(
        f"https://{PH_HOST}/auth/v1/token?grant_type=password",
        headers={"apikey": PH_ANON, "Content-Type": "application/json"},
        json={"email": PH_EMAIL, "password": PH_SENHA},
        timeout=15
    )
    r.raise_for_status()
    return r.json()["access_token"]

def ph_headers(jwt):
    return {
        "apikey": PH_ANON,
        "Authorization": f"Bearer {jwt}",
        "Accept-Profile": "public",
        "x-application": "partshub-web"
    }

# ── Buscar partes paginado ────────────────────────────────────────────────────
FIELDS = (
    "sku,id,name,vehicle_brand,vehicle_model,vehicle_year,"
    "price_sale,price_cost,quantity,condition,is_available,is_deleted,"
    "location,stock_yard,stock_corridor,stock_section,stock_shelf,stock_position,"
    "stock_yards_id,stock_corridors_id,stock_sections_id,stock_shelfs_id,stock_position_id,"
    "stock_box_id,stock_slot_id,"
    "weight,height,width,depth,"
    "oem_part_number,oem_code,oem_codes,part_number,"
    "compatible_vehicles,category_id,category_name,subcategory_id,subcategory_name,classification,"
    "parts_position_right_left,parts_position_front_back,"
    "ncm,cest,image_urls,sell_online,created_at,updated_at,"
    "detail_description,search_text"
)

def ph_fetch_all_parts(jwt):
    parts = []
    offset = 0
    batch  = 1000
    hdrs   = ph_headers(jwt)
    while True:
        url = (
            f"https://{PH_HOST}/rest/v1/parts"
            f"?select={FIELDS}"
            f"&is_deleted=eq.false"
            f"&is_available=eq.true"
            f"&quantity=gt.0"
            f"&order=sku.asc"
            f"&limit={batch}&offset={offset}"
        )
        r = requests.get(url, headers=hdrs, timeout=30)
        if not r.ok:
            print(f"  ERRO HTTP {r.status_code}: {r.text[:200]}")
            break
        batch_data = r.json()
        if not batch_data:
            break
        parts.extend(batch_data)
        print(f"  Partes carregadas: {len(parts)}")
        if len(batch_data) < batch:
            break
        offset += batch
        time.sleep(0.3)
    return parts

# ── Buscar tabelas de localização ─────────────────────────────────────────────
def ph_fetch_locais(jwt):
    """Retorna dicts id→nome para yards/corridors/sections/shelfs/positions"""
    hdrs  = ph_headers(jwt)
    mapas = {}
    tabelas = [
        ("stockyards",     "yards"),
        ("stockcorridors", "corridors"),
        ("stocksections",  "sections"),
        ("stockshelfs",    "shelfs"),
        ("stockpositions", "positions"),
    ]
    for tbl, chave in tabelas:
        all_rows = []
        offset = 0
        while True:
            r = requests.get(
                f"https://{PH_HOST}/rest/v1/{tbl}"
                f"?company_id=eq.{PH_COMPANY}&select=id,name&limit=1000&offset={offset}",
                headers=hdrs, timeout=30
            )
            if not r.ok:
                break
            rows = r.json()
            if not rows:
                break
            all_rows.extend(rows)
            if len(rows) < 1000:
                break
            offset += 1000
        mapa = {row["id"]: (row.get("name") or "").strip() for row in all_rows}
        mapas[chave] = mapa
        print(f"  {tbl}: {len(mapa)} registros")
    # Categorias: tabela global (company_id = null), NAO filtra por empresa
    cat_map = {}
    offset = 0
    while True:
        r = requests.get(
            f"https://{PH_HOST}/rest/v1/categories?select=id,name&limit=1000&offset={offset}",
            headers=ph_headers(jwt), timeout=30
        )
        if not r.ok:
            break
        rows = r.json()
        if not rows:
            break
        for row in rows:
            cat_map[row["id"]] = (row.get("name") or "").strip()
        if len(rows) < 1000:
            break
        offset += 1000
    mapas["categorias"] = cat_map
    print(f"  categories: {len(cat_map)} registros")
    return mapas

# ── Buscar fotos paginado ─────────────────────────────────────────────────────
def ph_fetch_all_photos(jwt, parts_raw):
    """Busca fotos por lote de part_id — evita o problema de offset/500"""
    photos = {}
    hdrs   = ph_headers(jwt)
    # Coletar todos os part_ids com UUID
    ids = [p["id"] for p in parts_raw if p.get("id")]
    batch = 80  # PostgREST tem limite de URL; 80 UUIDs por vez é seguro
    total = len(ids)
    ok_parts = 0
    erros = 0
    for i in range(0, total, batch):
        lote_ids = ids[i:i+batch]
        ids_str = ",".join(lote_ids)
        url = (
            f"https://{PH_HOST}/rest/v1/partphotos"
            f"?select=part_id,photo_url,original_photo_url,display_order"
            f"&part_id=in.({ids_str})"
            f"&order=display_order.asc.nullslast"
            f"&limit=2000"
        )
        try:
            r = requests.get(url, headers=hdrs, timeout=60)
            if r.ok:
                for p in r.json():
                    pid  = p.get("part_id")
                    purl = p.get("photo_url") or p.get("original_photo_url") or ""
                    if pid and purl:
                        photos.setdefault(pid, []).append(purl)
                ok_parts += len(lote_ids)
            else:
                erros += 1
                if erros <= 3:
                    print(f"  ERRO HTTP {r.status_code} lote {i//batch}")
        except Exception as e:
            erros += 1
            if erros <= 3:
                print(f"  Timeout lote {i//batch}: {type(e).__name__}")
        if (i // batch) % 20 == 0:
            total_urls = sum(len(v) for v in photos.values())
            print(f"  Fotos: {ok_parts}/{total} partes processadas, {total_urls} URLs")
        time.sleep(0.15)
    total_urls = sum(len(v) for v in photos.values())
    print(f"  Total final: {total_urls} URLs de {len(photos)} partes | erros: {erros}")
    return photos

# ── Carregar cache de locais existente ───────────────────────────────────────
def carregar_locais_existentes():
    # A LOCALIZAÇÃO é do SISTEMA (banco pecas_estoque), não do PartHub.
    # O sync NUNCA sobrescreve uma loc já definida no banco — o PartHub só
    # preenche peça NOVA / sem localização. Lê tudo do banco aqui.
    locs = {}
    off = 0
    hdr = {"apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY}
    while True:
        try:
            r = requests.get(
                f"{SB_URL}/rest/v1/pecas_estoque?select=sku,loc&loc=not.is.null&limit=1000&offset={off}",
                headers=hdr, timeout=30)
            rows = r.json() if r.status_code == 200 else []
        except Exception:
            rows = []
        if not rows:
            break
        for x in rows:
            v = (x.get("loc") or "").strip()
            if v and v.lower() not in ("nao enderecada", "não endereçada", "-", "sem local"):
                locs[str(x.get("sku"))] = v
        if len(rows) < 1000:
            break
        off += 1000
    print(f"  {len(locs)} localizações do SISTEMA (preservadas — PartHub não sobrescreve)")
    return locs

def carregar_quantidades_existentes():
    # A QUANTIDADE é do SISTEMA (banco pecas_estoque), não do PartHub.
    # As vendas dos marketplaces (ML/Shopee) é que baixam a qtd; o PartHub NÃO
    # pode sobrescrever (senão "ressuscita" peça vendida = furo de estoque).
    # O sync só usa a qtd do PartHub para peça NOVA (que ainda não existe aqui).
    # Lê TODOS os SKUs (inclusive qtd=0) para não reanimar uma peça já zerada.
    qtds = {}
    off = 0
    hdr = {"apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY}
    while True:
        try:
            r = requests.get(
                f"{SB_URL}/rest/v1/pecas_estoque?select=sku,qtd&limit=1000&offset={off}",
                headers=hdr, timeout=30)
            rows = r.json() if r.status_code == 200 else []
        except Exception:
            rows = []
        if not rows:
            break
        for x in rows:
            sku = str(x.get("sku") or "")
            if sku:
                qtds[sku] = int(x.get("qtd") or 0)
        if len(rows) < 1000:
            break
        off += 1000
    print(f"  {len(qtds)} quantidades do SISTEMA (preservadas — PartHub não sobrescreve)")
    return qtds

# ── Construir loc string ──────────────────────────────────────────────────────
def construir_loc(p, locais_existentes, mapas_loc=None):
    mapas_loc = mapas_loc or {}
    # 0. SISTEMA MANDA: se a peça já tem localização no banco, mantém (não deixa o
    #    PartHub sobrescrever o que o funcionário endereçou). PartHub só preenche peça nova.
    sku = str(p.get("sku") or "")
    if sku and sku in locais_existentes:
        return locais_existentes[sku]
    partes = []
    # 1. Resolver via IDs nas tabelas de localização (mais preciso)
    yard = ""
    if p.get("stock_yards_id") and mapas_loc.get("yards"):
        yard = mapas_loc["yards"].get(p["stock_yards_id"], "")
        if yard: partes.append(yard)
    if p.get("stock_corridors_id") and mapas_loc.get("corridors"):
        v = mapas_loc["corridors"].get(p["stock_corridors_id"], "")
        if v: partes.append(v)
    if p.get("stock_sections_id") and mapas_loc.get("sections"):
        v = mapas_loc["sections"].get(p["stock_sections_id"], "")
        if v: partes.append(v)
    if p.get("stock_shelfs_id") and mapas_loc.get("shelfs"):
        v = mapas_loc["shelfs"].get(p["stock_shelfs_id"], "")
        if v: partes.append(v)
    if p.get("stock_position_id") and mapas_loc.get("positions"):
        v = mapas_loc["positions"].get(p["stock_position_id"], "")
        if v: partes.append(v)
    if partes:
        return " → ".join(partes)
    # 2. Campos de texto diretos (legado)
    for campo in ["stock_yard", "stock_corridor", "stock_section", "stock_shelf", "stock_position"]:
        v = (p.get(campo) or "").strip()
        if v:
            partes.append(v)
    if partes:
        return " → ".join(partes)
    # 3. Cache antigo
    sku = str(p.get("sku") or "")
    if sku and sku in locais_existentes:
        return locais_existentes[sku]
    # 4. Campo location genérico
    return (p.get("location") or "").strip()

# ── Normalizar condição ───────────────────────────────────────────────────────
def normalizar_cond(c):
    if not c:
        return "Usada"
    c = c.lower().strip()
    if c in ("new", "novo", "nova"):
        return "Nova"
    return "Usada"

# ── Parse compatible_vehicles ─────────────────────────────────────────────────
def parse_compat(cv):
    if not cv:
        return []
    if isinstance(cv, str):
        try:
            cv = json.loads(cv)
        except Exception:
            return []
    if not isinstance(cv, list):
        return []
    result = []
    for v in cv[:20]:
        if isinstance(v, dict):
            brand = v.get("brandName") or v.get("brand") or ""
            model = v.get("modelName") or v.get("model") or ""
            year  = v.get("year") or ""
            parts = [x for x in [brand, model, str(year)] if x]
            if parts:
                result.append(" ".join(parts))
    return result

def _cv_brand_model(cv):
    """(marca, modelo, ano) do PRIMEIRO compatible_vehicle — o PartsHub guarda a marca/modelo
    AQUI, nao em vehicle_brand/vehicle_model (que vem vazio)."""
    if isinstance(cv, str):
        try:
            cv = json.loads(cv)
        except Exception:
            return ("", "", "")
    if isinstance(cv, list) and cv and isinstance(cv[0], dict):
        v = cv[0]
        return (str(v.get("brandName") or v.get("brand") or "").strip(),
                str(v.get("modelName") or v.get("model") or "").strip(),
                str(v.get("year") or "").strip())
    return ("", "", "")

# ── Construir objeto cache ────────────────────────────────────────────────────
def montar_peca(p, photos_map, locais_existentes, mapas_loc=None, quantidades_existentes=None):
    part_id = p.get("id") or ""
    fotos   = photos_map.get(part_id, [])
    # Se não tiver fotos do partphotos, tentar image_urls
    if not fotos and p.get("image_urls"):
        iu = p["image_urls"]
        if isinstance(iu, list):
            fotos = [f for f in iu if f]
        elif isinstance(iu, str):
            try:
                fotos = json.loads(iu) if iu.startswith("[") else [iu]
            except Exception:
                fotos = [iu]

    sku   = str(p.get("sku") or "")
    oem   = (p.get("oem_part_number") or p.get("oem_code") or p.get("part_number") or "").strip()
    # QUANTIDADE: sistema manda. Se a peça já existe no nosso banco, mantém a NOSSA
    # qtd (controlada pelas vendas) — PartHub não sobrescreve. Só peça nova usa a do PartHub.
    if quantidades_existentes is not None and sku in quantidades_existentes:
        qtd_final = quantidades_existentes[sku]
    else:
        qtd_final = int(p.get("quantity") or 0)
    _bm   = _cv_brand_model(p.get("compatible_vehicles"))  # marca/modelo/ano vem da compatibilidade

    medidas = {}
    if p.get("height") is not None: medidas["altura"]    = p["height"]
    if p.get("width")  is not None: medidas["largura"]   = p["width"]
    if p.get("depth")  is not None: medidas["comprimento"] = p["depth"]

    return {
        "sku":             sku,
        "titulo":          (p.get("name") or "").strip(),
        "marca":           ((p.get("vehicle_brand") or p.get("part_brand") or "").strip() or _bm[0]),
        "modelo":          ((p.get("vehicle_model") or p.get("part_model") or "").strip() or _bm[1]),
        "ano":             (str(p.get("vehicle_year") or "").strip() or _bm[2]),
        "oem":             oem,
        "preco":           float(p.get("price_sale") or 0),
        "custo":           float(p.get("price_cost") or 0),
        "qtd":             qtd_final,
        "cond":            normalizar_cond(p.get("condition")),
        "loc":             construir_loc(p, locais_existentes, mapas_loc),
        "peso":            float(p.get("weight") or 0) if p.get("weight") is not None else None,
        "medidas":         medidas if medidas else None,
        "fotos":           fotos[:8],
        "cat":             ((mapas_loc or {}).get("categorias", {}).get(p.get("category_id")) or p.get("category_name") or "").strip(),
        "lado":            (p.get("parts_position_right_left") or "").strip(),
        "posicao":         (p.get("parts_position_front_back") or "").strip(),
        "ncm":             (p.get("ncm") or "").strip(),
        "cest":            (p.get("cest") or "").strip(),
        "compatibilidade": parse_compat(p.get("compatible_vehicles")),
    }

# ── Atualizar pecas_estoque no Supabase do usuário ───────────────────────────
def atualizar_supabase(pecas):
    """Atualiza apenas os campos que existem em pecas_estoque"""
    print(f"\n[SUPABASE] Atualizando {len(pecas)} peças em pecas_estoque...")
    hdrs = {
        "apikey": SB_KEY,
        "Authorization": f"Bearer {SB_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal"
    }
    batch = 200
    ok = 0
    erros = 0
    for i in range(0, len(pecas), batch):
        lote = []
        for p in pecas[i:i+batch]:
            if not p.get("sku"):
                continue
            # Todas as chaves devem estar presentes em TODOS os objetos do lote
            lote.append({
                "sku":             p["sku"],
                "titulo":          p["titulo"],
                "marca":           p["marca"],
                "modelo":          p["modelo"],
                "ano":             p["ano"],
                "preco":           p["preco"],
                "qtd":             p["qtd"],
                "cond":            p["cond"],
                "loc":             p.get("loc") or "",
                "categoria":       p.get("cat") or "",
                "oem":             p.get("oem") or "",
                "fotos":           p.get("fotos") or [],
                "compatibilidade": p.get("compatibilidade") or [],
                "medidas":         p.get("medidas"),
                "peso":            p.get("peso"),
                "custo":           p.get("custo"),
                "lado":            p.get("lado") or "",
                "posicao":         p.get("posicao") or "",
                # NCM/CEST vêm do PartsHub (cadastro) — necessários p/ a loja Shopee com Nota Fiscal (Mauricio).
                # SEMPRE presentes em TODAS as linhas (chaves iguais) p/ não dar PGRST102.
                "ncm":             p.get("ncm") or "",
                "cest":            p.get("cest") or "",
                "atualizado":      datetime.now(timezone.utc).isoformat()
            })
        if not lote:
            continue
        r = requests.post(
            f"{SB_URL}/rest/v1/pecas_estoque",
            headers=hdrs,
            json=lote,
            timeout=30
        )
        # Fallback: se alguma coluna nova ainda nao existe (PGRST204), regrava so o que existe
        if not r.ok and ("PGRST204" in r.text or "column" in r.text.lower()):
            BASE = {"sku","titulo","marca","modelo","ano","preco","qtd","cond","loc",
                    "categoria","oem","fotos","compatibilidade","atualizado"}
            lote2 = [{k: v for k, v in obj.items() if k in BASE} for obj in lote]
            r = requests.post(f"{SB_URL}/rest/v1/pecas_estoque", headers=hdrs, json=lote2, timeout=30)
            if i == 0 and r.ok:
                print("  [aviso] colunas novas (medidas/peso/etc) ainda nao existem - gravando campos base + categoria. Rode o SQL das colunas e sincronize de novo p/ medidas.")
        if r.ok:
            ok += len(lote)
        else:
            erros += 1
            print(f"  ERRO lote {i//batch}: {r.status_code} {r.text[:200]}")
        time.sleep(0.1)
    print(f"  OK: {ok} | Erros: {erros}")

# ── Gerar partshub-estoque-cache.json ────────────────────────────────────────
def gerar_cache_estoque(pecas):
    path = os.path.join(NETLIFY, "partshub-estoque-cache.json")
    data = {
        "version":    3,
        "gerado_em":  datetime.now(timezone.utc).isoformat(),
        "total":      len(pecas),
        "pecas":      pecas
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print(f"[CACHE] partshub-estoque-cache.json gerado com {len(pecas)} peças")

# ── Gerar partshub-locais-cache.js ───────────────────────────────────────────
def gerar_cache_locais(pecas):
    locais_por_sku = {}
    for p in pecas:
        if p.get("loc") and p.get("sku"):
            locais_por_sku[str(p["sku"])] = p["loc"]

    path = os.path.join(NETLIFY, "partshub-locais-cache.js")
    agora = datetime.now().strftime("%Y-%m-%d %H:%M")
    linhas = [
        f"// Cache de Localizacoes do Estoque — gerado automaticamente",
        f"// {len(locais_por_sku)} SKUs com localizacao definida",
        f"window.PARTSHUB_LOCAIS_CACHE = {{",
        f"  totalSkus: {len(locais_por_sku)},",
        f'  geradoEm: "{agora}",',
        f"  locaisPorSku: {{"
    ]
    for sku, loc in sorted(locais_por_sku.items(), key=lambda x: x[0]):
        linhas.append(f'  "{sku}": {json.dumps(loc, ensure_ascii=False)},')
    # remove trailing comma
    if linhas and linhas[-1].endswith(","):
        linhas[-1] = linhas[-1][:-1]
    linhas += ["  }", "}"]

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(linhas) + "\n")
    print(f"[CACHE] partshub-locais-cache.js gerado com {len(locais_por_sku)} SKUs com local")

    # Também gera o .json (formato que o estoque.html consome via fetch: {locaisPorSku:{...}})
    path_json = os.path.join(NETLIFY, "partshub-locais-cache.json")
    with open(path_json, "w", encoding="utf-8") as f:
        json.dump({"totalSkus": len(locais_por_sku), "geradoEm": agora, "locaisPorSku": locais_por_sku},
                  f, ensure_ascii=False, separators=(",", ":"))
    print(f"[CACHE] partshub-locais-cache.json gerado com {len(locais_por_sku)} SKUs com local")

# ── Marcar peças que saíram de estoque (qtd=0) ───────────────────────────────
def marcar_fora_estoque(pecas):
    """Peças do PartsHub que NÃO estão mais no fetch (sem estoque/indisponíveis)
    ficam com qtd=0 — somem do filtro 'Com estoque'. Só mexe em origem IS NULL
    (não toca peças da extensão). Restaurado automaticamente se voltarem ao estoque."""
    em_estoque = {str(p["sku"]) for p in pecas if p.get("sku")}
    # Trava de segurança: só limpa se o fetch trouxe um volume coerente
    if len(em_estoque) < 5000:
        print(f"  [skip limpeza] fetch só trouxe {len(em_estoque)} peças (<5000) — não vou zerar nada.")
        return
    hdrs = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}",
            "Content-Type": "application/json", "Prefer": "return=minimal"}
    ghdr = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"}
    # Buscar todos os SKUs PartsHub (origem null) com qtd>0
    cache_skus, offset = [], 0
    while True:
        r = requests.get(f"{SB_URL}/rest/v1/pecas_estoque?select=sku&origem=is.null&qtd=gt.0"
                         f"&order=sku.asc&limit=1000&offset={offset}", headers=ghdr, timeout=30)
        if not r.ok:
            break
        rows = r.json()
        if not rows:
            break
        cache_skus.extend(str(x["sku"]) for x in rows if x.get("sku"))
        if len(rows) < 1000:
            break
        offset += 1000
    stale = [s for s in cache_skus if s not in em_estoque]
    print(f"\n[LIMPEZA] {len(stale)} peças fora de estoque -> qtd=0 (de {len(cache_skus)} PartsHub com estoque no cache)")
    zer = 0
    for i in range(0, len(stale), 120):
        lote = stale[i:i+120]
        inq = ",".join(lote)
        r = requests.patch(f"{SB_URL}/rest/v1/pecas_estoque?origem=is.null&sku=in.({inq})",
                           headers=hdrs, json={"qtd": 0}, timeout=30)
        if r.ok:
            zer += len(lote)
        time.sleep(0.05)
    print(f"  zeradas: {zer}")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("SYNC PARTS HUB -> SISTEMA")
    print("=" * 60)

    # 1. Autenticar
    print("\n[1/5] Autenticando no Parts Hub...")
    jwt = ph_get_jwt()
    print("  JWT obtido.")

    # 2. Carregar locais existentes (para preservar)
    print("\n[2/5] Carregando locais existentes do cache...")
    locais_existentes = carregar_locais_existentes()
    print(f"  {len(locais_existentes)} SKUs com locais existentes")
    quantidades_existentes = carregar_quantidades_existentes()

    # 3. Buscar partes
    print("\n[3/5] Buscando partes do Parts Hub...")
    parts_raw = ph_fetch_all_parts(jwt)
    print(f"  Total: {len(parts_raw)} partes")

    # 4. Buscar fotos
    print("\n[4/5] Buscando tabelas de localizacao...")
    mapas_loc = ph_fetch_locais(jwt)

    print("\n[5/6] Buscando fotos...")
    photos_map = ph_fetch_all_photos(jwt, parts_raw)
    print(f"  {len(photos_map)} partes com fotos")

    # 6. Montar objetos
    print("\n[6/6] Montando cache...")
    pecas = []
    skus_vistos = set()
    for p in parts_raw:
        sku = str(p.get("sku") or "").strip()
        if not sku or sku in skus_vistos:
            continue
        skus_vistos.add(sku)
        pecas.append(montar_peca(p, photos_map, locais_existentes, mapas_loc, quantidades_existentes))

    print(f"  {len(pecas)} peças únicas montadas")

    # Gerar caches
    gerar_cache_estoque(pecas)
    gerar_cache_locais(pecas)

    # Atualizar Supabase
    atualizar_supabase(pecas)

    # Marcar peças que sairam de estoque (qtd=0) — mantem estoque fiel ao PartsHub
    marcar_fora_estoque(pecas)

    print("\n[OK] SYNC CONCLUIDO")
    print(f"  Arquivos gerados em: {NETLIFY}")
    return pecas

if __name__ == "__main__":
    main()
