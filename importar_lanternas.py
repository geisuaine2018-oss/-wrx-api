# -*- coding: utf-8 -*-
"""
Importa a FAIXA de peças do PartsHub (SKU PartsHub 109207..109250) para o NOSSO
pecas_estoque, dando SKU NOVO da nossa sequência (a partir de 109339) e guardando
o SKU original do PartsHub em origem="partshub:<sku_ph>" (pra etiqueta).

Uso:
  python importar_lanternas.py            -> PREVIEW (nao insere, so mostra/salva json)
  python importar_lanternas.py --inserir  -> INSERE de verdade no banco
"""
import sys, json, requests
from datetime import datetime, timezone
from sync_partshub import (
    ph_get_jwt, ph_headers, FIELDS, PH_HOST, PH_ANON,
    ph_fetch_locais, ph_fetch_all_photos, montar_peca,
    carregar_locais_existentes, SB_URL, SB_KEY,
)

PH_SKU_MIN = 109207
PH_SKU_MAX = 109250
NOSSO_SKU_INICIAL = 109339   # ultimo criado nosso = 109338 (a dona informou)

def puxar_range(jwt):
    hdrs = ph_headers(jwt)
    url = (f"https://{PH_HOST}/rest/v1/parts?select={FIELDS}"
           f"&sku=gte.{PH_SKU_MIN}&sku=lte.{PH_SKU_MAX}&order=sku.asc&limit=300")
    r = requests.get(url, headers=hdrs, timeout=40)
    r.raise_for_status()
    bruto = r.json()
    # filtro NUMERICO real (remove "10921","10925" etc que entram na ordenacao string)
    out = []
    for p in bruto:
        s = str(p.get("sku") or "").strip()
        if s.isdigit() and PH_SKU_MIN <= int(s) <= PH_SKU_MAX:
            out.append(p)
    out.sort(key=lambda p: int(p["sku"]))
    return out

def main():
    inserir = "--inserir" in sys.argv
    print("== Login PartsHub ==")
    jwt = ph_get_jwt()
    print("== Puxando faixa", PH_SKU_MIN, "a", PH_SKU_MAX, "==")
    parts = puxar_range(jwt)
    print(f"  {len(parts)} pecas reais no range")
    print("== Fotos ==")
    photos = ph_fetch_all_photos(jwt, parts)
    print("== Mapas de locais/categorias ==")
    mapas = ph_fetch_locais(jwt)
    locais_existentes = carregar_locais_existentes()

    registros = []
    for i, p in enumerate(parts):
        sku_ph = str(p["sku"])
        peca = montar_peca(p, photos, locais_existentes, mapas, quantidades_existentes=None)
        nosso_sku = str(NOSSO_SKU_INICIAL + i)
        peca["sku"] = nosso_sku
        peca["origem"] = f"partshub:{sku_ph}"   # <- SKU PartsHub p/ etiqueta
        peca["_sku_ph"] = sku_ph
        registros.append(peca)

    print("\n=== PREVIEW (nosso SKU  <-  PartsHub) ===")
    print(f"{'NOSSO':>7}  {'PH':>7}  {'R$':>9}  fts  titulo")
    zerados = []
    for p in registros:
        nf = len(p.get("fotos") or [])
        pr = p.get("preco") or 0
        if not pr: zerados.append(p["sku"])
        print(f"{p['sku']:>7}  {p['_sku_ph']:>7}  {pr:>9.2f}  {nf:>3}  {(p['titulo'] or '')[:50]}")
    print(f"\nTotal: {len(registros)} pecas | nosso SKU {registros[0]['sku']}..{registros[-1]['sku']}")
    print(f"Sem foto: {[p['sku'] for p in registros if not (p.get('fotos'))]}")
    print(f"Preco R$0 (precisa precificar): {zerados}")

    # salvar json pra conferencia / reuso
    with open("importar_lanternas_preview.json", "w", encoding="utf-8") as f:
        json.dump(registros, f, ensure_ascii=False, indent=1)
    print("\n[salvo] importar_lanternas_preview.json")

    if not inserir:
        print("\n>>> PREVIEW apenas. Nada foi inserido. Rode com --inserir para gravar.")
        return

    # ---- INSERCAO REAL ----
    print("\n== INSERINDO no banco ==")
    hdrs = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal"}
    lote = []
    for p in registros:
        lote.append({
            "sku": p["sku"], "titulo": p["titulo"], "marca": p["marca"],
            "modelo": p["modelo"], "ano": p["ano"], "preco": p["preco"],
            "qtd": p["qtd"], "cond": p["cond"], "loc": p.get("loc") or "",
            "categoria": p.get("cat") or "", "oem": p.get("oem") or "",
            "fotos": p.get("fotos") or [], "compatibilidade": p.get("compatibilidade") or [],
            "medidas": p.get("medidas"), "peso": p.get("peso"), "custo": p.get("custo"),
            "lado": p.get("lado") or "", "posicao": p.get("posicao") or "",
            "ncm": p.get("ncm") or "", "cest": p.get("cest") or "",
            "origem": p["origem"],
            "atualizado": datetime.now(timezone.utc).isoformat(),
        })
    r = requests.post(f"{SB_URL}/rest/v1/pecas_estoque", headers=hdrs, json=lote, timeout=60)
    print("HTTP", r.status_code, r.text[:300] if not r.ok else "OK")

if __name__ == "__main__":
    main()
