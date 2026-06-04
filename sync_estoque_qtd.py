"""
sync_estoque_qtd.py — Sync LEVE de estoque PartsHub -> pecas_estoque.

Objetivo unico: ZERAR no nosso banco as pecas que sairam de estoque (venderam),
pra fechar a janela de venda-dupla ENTRE as rodadas do sync completo
(sync_partshub.py, que e pesado/manual). Rapido: busca so SKUs com quantity>0;
o que estava com estoque no nosso banco e sumiu do PartsHub vira qtd=0.

Roda no GitHub Actions (cron). Uso local: python sync_estoque_qtd.py
NAO mexe em fotos, titulos, localizacao nem em arquivos do site.
"""
import requests

# ── Credenciais (mesmas do sync_partshub.py) ────────────────────────────────
PH_HOST  = "iftzoceaalhpyckuznae.supabase.co"
PH_ANON  = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImlmdHpvY2VhYWxocHlja3V6bmFlIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NjA0MzMwNjcsImV4cCI6MjA3NjAwOTA2N30.VZY9NLFvRMX-lr9FQUlOkMfE0RfdGxk0HVpslxMYDYg"
PH_EMAIL = "geisuaine2025@gmail.com"
PH_SENHA = "Vitoria12$"
SB_URL   = "https://uthsiihzpsgarargegcw.supabase.co"
SB_KEY   = "sb_publishable_gOQgHrv2IVRgbiVV2Myhzg_BmzCXmXe"


def ph_jwt():
    r = requests.post(
        f"https://{PH_HOST}/auth/v1/token?grant_type=password",
        headers={"apikey": PH_ANON, "Content-Type": "application/json"},
        json={"email": PH_EMAIL, "password": PH_SENHA}, timeout=15)
    r.raise_for_status()
    return r.json()["access_token"]


def ph_skus_em_estoque(jwt):
    """SKUs com estoque real no PartsHub (quantity>0, disponivel, nao deletado)."""
    hdrs = {"apikey": PH_ANON, "Authorization": f"Bearer {jwt}",
            "Accept-Profile": "public", "x-application": "partshub-web"}
    skus, offset = set(), 0
    while True:
        url = (f"https://{PH_HOST}/rest/v1/parts?select=sku"
               f"&is_deleted=eq.false&is_available=eq.true&quantity=gt.0"
               f"&order=sku.asc&limit=1000&offset={offset}")
        r = requests.get(url, headers=hdrs, timeout=30)
        r.raise_for_status()
        rows = r.json()
        if not rows:
            break
        for x in rows:
            if x.get("sku") is not None:
                skus.add(str(x["sku"]))
        if len(rows) < 1000:
            break
        offset += 1000
    return skus


def zerar_vendidas(em_estoque):
    # Trava de seguranca: so zera se o fetch trouxe um volume coerente
    # (evita zerar tudo se o PartsHub falhar/retornar vazio).
    if len(em_estoque) < 5000:
        print(f"[ABORT] fetch trouxe so {len(em_estoque)} SKUs (<5000) — nao vou zerar nada.")
        return 0
    ghdr = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"}
    phdr = {**ghdr, "Content-Type": "application/json", "Prefer": "return=minimal"}
    # Nossos SKUs PartsHub (origem null) marcados COM estoque
    nossos, offset = [], 0
    while True:
        r = requests.get(
            f"{SB_URL}/rest/v1/pecas_estoque?select=sku&origem=is.null&qtd=gt.0"
            f"&order=sku.asc&limit=1000&offset={offset}", headers=ghdr, timeout=30)
        r.raise_for_status()
        rows = r.json()
        if not rows:
            break
        nossos += [str(x["sku"]) for x in rows if x.get("sku") is not None]
        if len(rows) < 1000:
            break
        offset += 1000
    stale = [s for s in nossos if s not in em_estoque]
    print(f"[LIMPEZA] {len(stale)} vendidas -> qtd=0 "
          f"(nosso banco c/ estoque: {len(nossos)} | PartsHub c/ estoque: {len(em_estoque)})")
    zer = 0
    for i in range(0, len(stale), 120):
        lote = stale[i:i + 120]
        r = requests.patch(
            f"{SB_URL}/rest/v1/pecas_estoque?origem=is.null&sku=in.({','.join(lote)})",
            headers=phdr, json={"qtd": 0}, timeout=30)
        if r.ok:
            zer += len(lote)
    print(f"  zeradas: {zer}")
    return zer


if __name__ == "__main__":
    jwt = ph_jwt()
    em = ph_skus_em_estoque(jwt)
    print(f"PartsHub: {len(em)} SKUs com estoque")
    zerar_vendidas(em)
    print("[OK]")
