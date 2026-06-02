"""
sync_multicanal.py — BLOCO B: Anti-venda-dupla (sincronização multi-canal)
═══════════════════════════════════════════════════════════════════════════
Objetivo: quando uma peça (SKU) é vendida em UM canal, pausar o anúncio dessa
mesma peça em TODOS os outros canais (ML + Shopee) — peça de desmonte é única
(qtd 1), então não pode continuar à venda em outro lugar.

DESACOPLADO de propósito (o usuário pediu "blocos separados"):
  - NÃO depende das closures do api_server.py.
  - Lê o mapeamento SKU→anúncio direto do Supabase REST (tabelas `ml_anuncios`
    e `shopee_anuncios`).
  - Para PAUSAR de verdade precisa de token; recebe os provedores via init_sync()
    (injeção de dependência). Sem provedores → modo DRY-RUN (só planeja).

Dois modos:
  • planejar_pausa(sku, origem)      → lista o que SERIA pausado (testável local,
                                        só precisa da chave Supabase, sem token).
  • executar_sincronizacao(sku, ...) → planeja + pausa de verdade (só com token,
                                        roda no Railway).

Teste local (dry-run, sem mexer em nada):
    python sync_multicanal.py 8593 ml
"""

import os
import time
import json
import hmac
import hashlib
import requests

# ─── Config Supabase (chave publishable = só leitura do mapeamento p/ planejar) ──
SB_URL = os.environ.get("WRX_SB_URL", "https://uthsiihzpsgarargegcw.supabase.co")
SB_KEY = os.environ.get("WRX_SB_KEY", "sb_publishable_gOQgHrv2IVRgbiVV2Myhzg_BmzCXmXe")

def _sb_headers():
    # service key (se existir no ambiente) tem prioridade p/ escrita; senão publishable
    key = os.environ.get("SUPABASE_SERVICE_KEY") or SB_KEY
    return {"apikey": key, "Authorization": "Bearer " + key}

# ─── B4: gatilho automático (detecta venda → dispara cascata) ────────────────────
# Self-call: no Railway, chamar a própria URL PÚBLICA costuma falhar — usa localhost
# (o Flask roda threaded, então atende a si mesmo em outra thread).
SELF_BASE = os.environ.get("WRX_SELF_URL") or f"http://127.0.0.1:{os.environ.get('PORT', '5678')}"
CONTAS_ML = ["default", "geisa"]
_PROC_FILE = os.path.join(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "/tmp"), "wrx_vendas_processadas.json")

def _carregar_processadas():
    try:
        with open(_PROC_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()

def _salvar_processadas(s):
    try:
        # mantém só os últimos 5000 order_ids (evita crescer infinito)
        with open(_PROC_FILE, "w", encoding="utf-8") as f:
            json.dump(list(s)[-5000:], f)
    except Exception as e:
        print(f"[B4] nao salvou processadas: {e}")

# ─── Provedores injetados pelo api_server (None = dry-run) ────────────────────────
_ml_token_provider = None       # fn(conta) -> access_token | None
_shopee_token_provider = None   # fn(shop_id) -> (access_token, shop_id_int) | (None, 0)
_shopee_partner_id = None
_shopee_partner_key = None
_shopee_base = "https://partner.shopeemobile.com"

def init_sync(ml_token_provider=None, shopee_token_provider=None,
              shopee_partner_id=None, shopee_partner_key=None, shopee_base=None):
    """Chamado pelo api_server.py ao registrar o blueprint, injetando os tokens reais."""
    global _ml_token_provider, _shopee_token_provider
    global _shopee_partner_id, _shopee_partner_key, _shopee_base
    _ml_token_provider = ml_token_provider
    _shopee_token_provider = shopee_token_provider
    _shopee_partner_id = shopee_partner_id
    _shopee_partner_key = shopee_partner_key
    if shopee_base:
        _shopee_base = shopee_base

# ─── Leitura do mapeamento SKU → anúncios em cada canal ──────────────────────────
def _buscar_anuncios_ml(sku):
    """Anúncios ML ativos desse SKU: [{ml_id, conta, titulo, status}]."""
    try:
        r = requests.get(
            f"{SB_URL}/rest/v1/ml_anuncios",
            params={"sku": f"eq.{sku}", "select": "ml_id,conta,titulo,status"},
            headers=_sb_headers(), timeout=12)
        if r.status_code == 200:
            return [a for a in r.json() if (a.get("status") or "").lower() == "active"]
    except Exception as e:
        print(f"[SYNC] erro ml_anuncios: {e}")
    return []

def _buscar_anuncios_shopee(sku):
    """Anúncios Shopee desse SKU: [{shop_id, item_id, titulo, status}]. Tabela pode não existir ainda."""
    try:
        r = requests.get(
            f"{SB_URL}/rest/v1/shopee_anuncios",
            params={"sku": f"eq.{sku}", "select": "shop_id,item_id,titulo,status"},
            headers=_sb_headers(), timeout=12)
        if r.status_code == 200:
            return [a for a in r.json() if (a.get("status") or "NORMAL").upper() != "UNLIST"]
    except Exception as e:
        print(f"[SYNC] erro shopee_anuncios: {e}")
    return []

def _peca_unica(sku):
    """True só se o SKU é peça de estoque ÚNICO (existe em `pecas` com qtd<=1).
    Peças com várias unidades (qtd>1) e SKUs fora de `pecas` (ex: anúncio-isca
    'Calota fake') NÃO entram na cascata — pausar elas seria errado."""
    try:
        r = requests.get(f"{SB_URL}/rest/v1/pecas",
                         params={"sku": f"eq.{sku}", "select": "qtd"},
                         headers=_sb_headers(), timeout=10)
        if r.status_code == 200 and r.json():
            return int(r.json()[0].get("qtd") or 0) <= 1
    except Exception as e:
        print(f"[SYNC] _peca_unica erro: {e}")
    return False  # não está em pecas (isca/avulso) → não pausa

def planejar_pausa(sku, origem):
    """
    Monta a lista de ações de pausa SEM executar. `origem` = canal onde vendeu
    ('ml' ou 'shopee'). SÓ pausa peça ÚNICA de estoque real (ver _peca_unica).
    Retorna {sku, origem, acoes: [{canal, alvo, titulo}], total}.
    """
    sku = str(sku).strip()
    acoes = []
    if not _peca_unica(sku):
        return {"sku": sku, "origem": origem, "acoes": [], "total": 0,
                "motivo": "peca nao-unica ou fora do estoque (nao pausa)"}
    for a in _buscar_anuncios_ml(sku):
        # se vendeu numa conta ML, ainda assim pausa as OUTRAS contas ML (peça é a mesma física)
        acoes.append({"canal": "ml", "conta": a.get("conta"), "alvo": a.get("ml_id"),
                      "titulo": a.get("titulo", "")})
    for a in _buscar_anuncios_shopee(sku):
        acoes.append({"canal": "shopee", "shop_id": a.get("shop_id"), "alvo": a.get("item_id"),
                      "titulo": a.get("titulo", "")})
    return {"sku": sku, "origem": origem, "acoes": acoes, "total": len(acoes)}

# ─── Execução real (precisa de token) ────────────────────────────────────────────
def pausar_anuncio_ml(ml_id, conta):
    """Pausa um anúncio ML (status=paused). Retorna (ok, detalhe)."""
    if not _ml_token_provider:
        return False, "sem token (dry-run)"
    token = _ml_token_provider(conta or "default")
    if not token:
        return False, f"conta ML '{conta}' sem token"
    try:
        r = requests.put(
            f"https://api.mercadolibre.com/items/{ml_id}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"status": "paused"}, timeout=15)
        if r.status_code == 200:
            return True, "pausado"
        return False, f"HTTP {r.status_code}: {r.text[:160]}"
    except Exception as e:
        return False, str(e)

def reativar_anuncio_ml(ml_id, conta):
    """Reativa um anúncio ML (status=active). Usado no teste e em cancelamento de venda."""
    if not _ml_token_provider:
        return False, "sem token (dry-run)"
    token = _ml_token_provider(conta or "default")
    if not token:
        return False, f"conta ML '{conta}' sem token"
    try:
        r = requests.put(
            f"https://api.mercadolibre.com/items/{ml_id}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"status": "active"}, timeout=15)
        if r.status_code == 200:
            return True, "reativado"
        return False, f"HTTP {r.status_code}: {r.text[:160]}"
    except Exception as e:
        return False, str(e)

def pausar_anuncio_shopee(shop_id, item_id):
    """Pausa (unlist) um anúncio Shopee. Retorna (ok, detalhe)."""
    if not _shopee_token_provider or not _shopee_partner_id:
        return False, "sem token Shopee (dry-run)"
    access_token, shop_id_int = _shopee_token_provider(shop_id)
    if not access_token:
        return False, f"shop {shop_id} sem token"
    try:
        ts = int(time.time())
        path = "/api/v2/product/unlist_item"
        base = f"{_shopee_partner_id}{path}{ts}{access_token}{shop_id_int}"
        sign = hmac.new(_shopee_partner_key.encode(), base.encode(), hashlib.sha256).hexdigest()
        r = requests.post(
            f"{_shopee_base}{path}",
            params={"partner_id": _shopee_partner_id, "timestamp": ts,
                    "access_token": access_token, "shop_id": shop_id_int, "sign": sign},
            json={"item_list": [{"item_id": int(item_id), "unlist": True}]}, timeout=15)
        if r.status_code == 200 and not r.json().get("error"):
            return True, "unlist"
        return False, f"HTTP {r.status_code}: {r.text[:160]}"
    except Exception as e:
        return False, str(e)

def executar_sincronizacao(sku, origem, dry_run=False):
    """
    Planeja e (se não dry_run e houver token) executa as pausas. Registra em
    `sync_log`. Retorna o relatório com o resultado de cada ação.
    """
    plano = planejar_pausa(sku, origem)
    resultados = []
    for ac in plano["acoes"]:
        if dry_run:
            ok, det = None, "dry-run (não executado)"
        elif ac["canal"] == "ml":
            ok, det = pausar_anuncio_ml(ac["alvo"], ac.get("conta"))
        elif ac["canal"] == "shopee":
            ok, det = pausar_anuncio_shopee(ac.get("shop_id"), ac["alvo"])
        else:
            ok, det = False, "canal desconhecido"
        resultados.append({**ac, "ok": ok, "detalhe": det})

    relatorio = {"sku": sku, "origem": origem, "dry_run": dry_run,
                 "total": len(resultados), "resultados": resultados,
                 "pausados": sum(1 for r in resultados if r.get("ok"))}
    if not dry_run:
        _registrar_log(relatorio)
    return relatorio

def _registrar_log(relatorio):
    """Grava o resultado da sincronização em `sync_log` (best-effort)."""
    try:
        requests.post(
            f"{SB_URL}/rest/v1/sync_log",
            headers={**_sb_headers(), "Content-Type": "application/json", "Prefer": "return=minimal"},
            json={"sku": relatorio["sku"], "origem": relatorio["origem"],
                  "pausados": relatorio["pausados"], "total": relatorio["total"],
                  "detalhe": json.dumps(relatorio["resultados"], ensure_ascii=False)},
            timeout=10)
    except Exception as e:
        print(f"[SYNC] log falhou: {e}")

# ─── Flask Blueprint (registrado pelo api_server com 1 linha) ────────────────────
def get_blueprint():
    from flask import Blueprint, request, jsonify, Response
    bp = Blueprint("sync_multicanal", __name__)

    def _cors():
        return Response(status=204, headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization"})

    @bp.route("/integracoes/ml-status-count", methods=["GET", "OPTIONS"])
    def ml_status_count():
        """Quantos anúncios há por status em cada conta ML (active/paused/closed/...)."""
        if request.method == "OPTIONS":
            return _cors()
        conta = request.args.get("conta", "default").strip()
        token = _ml_token_provider(conta) if _ml_token_provider else None
        if not token:
            r = jsonify({"erro": "sem token"}); r.headers["Access-Control-Allow-Origin"] = "*"; return r
        me = requests.get("https://api.mercadolibre.com/users/me",
                          headers={"Authorization": f"Bearer {token}"}, timeout=10)
        uid = me.json().get("id") if me.status_code == 200 else None
        out = {}
        for st in ["active", "paused", "closed", "under_review", "inactive"]:
            rr = requests.get(f"https://api.mercadolibre.com/users/{uid}/items/search",
                              params={"status": st, "limit": 1},
                              headers={"Authorization": f"Bearer {token}"}, timeout=12)
            out[st] = rr.json().get("paging", {}).get("total") if rr.status_code == 200 else f"http{rr.status_code}"
        r = jsonify({"conta": conta, "uid": uid, "por_status": out})
        r.headers["Access-Control-Allow-Origin"] = "*"; return r

    @bp.route("/integracoes/ml-debug-multiget", methods=["GET", "OPTIONS"])
    def ml_debug_multiget():
        """Compara multiget COM e SEM projeção p/ o mesmo item — prova onde o SELLER_SKU some."""
        if request.method == "OPTIONS":
            return _cors()
        ml_id = request.args.get("ml_id", "").strip()
        conta = request.args.get("conta", "default").strip()
        token = _ml_token_provider(conta) if _ml_token_provider else None
        if not token:
            r = jsonify({"erro": "sem token"}); r.headers["Access-Control-Allow-Origin"] = "*"; return r
        def _probe(params):
            rr = requests.get("https://api.mercadolibre.com/items",
                              params=params, headers={"Authorization": f"Bearer {token}"}, timeout=20)
            body = {}
            if rr.status_code == 200:
                arr = rr.json()
                if arr and arr[0].get("code") == 200:
                    body = arr[0].get("body", {})
            attrs = body.get("attributes") or []
            return {"http": rr.status_code, "n_attrs": len(attrs),
                    "SELLER_SKU": [a.get("value_name") for a in attrs if a.get("id") == "SELLER_SKU"]}
        out = {
            "sem_projecao": _probe({"ids": ml_id}),
            "com_projecao": _probe({"ids": ml_id, "attributes": "id,title,attributes"}),
        }
        r = jsonify(out); r.headers["Access-Control-Allow-Origin"] = "*"; return r

    @bp.route("/integracoes/ml-debug-item", methods=["GET", "OPTIONS"])
    def ml_debug_item():
        """Diagnóstico: GET individual autenticado de 1 item ML — mostra onde o SKU está."""
        if request.method == "OPTIONS":
            return _cors()
        ml_id = request.args.get("ml_id", "").strip()
        conta = request.args.get("conta", "default").strip()
        if not _ml_token_provider:
            r = jsonify({"erro": "sem token provider"}); r.headers["Access-Control-Allow-Origin"] = "*"; return r
        token = _ml_token_provider(conta)
        if not token:
            r = jsonify({"erro": f"conta {conta} sem token"}); r.headers["Access-Control-Allow-Origin"] = "*"; return r
        rr = requests.get(f"https://api.mercadolibre.com/items/{ml_id}",
                          headers={"Authorization": f"Bearer {token}"}, timeout=15)
        d = rr.json() if rr.status_code == 200 else {}
        out = {
            "http": rr.status_code,
            "seller_custom_field": d.get("seller_custom_field"),
            "seller_sku": d.get("seller_sku"),
            "attrs_sku": [{"id": a.get("id"), "v": a.get("value_name")}
                         for a in (d.get("attributes") or [])
                         if "SKU" in (a.get("id") or "")],
            "variations_sku": [{"scf": v.get("seller_custom_field"), "ssku": v.get("seller_sku")}
                               for v in (d.get("variations") or [])],
            "n_attrs": len(d.get("attributes") or []),
        }
        r = jsonify(out); r.headers["Access-Control-Allow-Origin"] = "*"; return r

    @bp.route("/integracoes/anuncio-acao", methods=["POST", "OPTIONS"])
    def anuncio_acao():
        """Pausa/reativa UM anúncio específico — usado p/ teste controlado e cancelamento.
        Body: {canal:'ml', alvo:'MLB...', conta:'default', acao:'pausar'|'reativar'}"""
        if request.method == "OPTIONS":
            return _cors()
        data = request.get_json(force=True) or {}
        canal = (data.get("canal") or "ml").strip()
        alvo = (data.get("alvo") or "").strip()
        conta = (data.get("conta") or "default").strip()
        acao = (data.get("acao") or "pausar").strip()
        if not alvo:
            r = jsonify({"ok": False, "erro": "alvo obrigatorio"}); r.headers["Access-Control-Allow-Origin"] = "*"
            return r, 400
        if canal == "ml":
            ok, det = (pausar_anuncio_ml if acao == "pausar" else reativar_anuncio_ml)(alvo, conta)
        elif canal == "shopee":
            ok, det = (False, "shopee reativar nao implementado") if acao == "reativar" \
                else pausar_anuncio_shopee(data.get("shop_id"), alvo)
        else:
            ok, det = False, "canal desconhecido"
        r = jsonify({"ok": ok, "canal": canal, "alvo": alvo, "acao": acao, "detalhe": det})
        r.headers["Access-Control-Allow-Origin"] = "*"
        return r

    @bp.route("/integracoes/processar-vendas-novas", methods=["POST", "GET", "OPTIONS"])
    def processar_vendas_novas():
        """B4 — GATILHO: busca vendas recentes do ML, e p/ cada venda NOVA (paga, com
        SKU, ainda não processada) dispara a cascata anti-venda-dupla.
        ?modo=observacao (padrão, só planeja+loga) | armado (pausa de verdade).
        Idempotência por order_id (arquivo no volume)."""
        if request.method == "OPTIONS":
            return _cors()
        modo = request.args.get("modo", "observacao").strip()
        horas = float(request.args.get("horas", "1") or 1)
        processadas = _carregar_processadas()
        novas, resultados = [], []
        diag = []
        for conta in CONTAS_ML:
            try:
                rv = requests.get(f"{SELF_BASE}/integracoes/mercadolivre/vendas-recentes",
                                  params={"conta": conta, "dias": 1}, timeout=90)
                vendas = rv.json().get("vendas", []) if rv.status_code == 200 else []
                diag.append({"conta": conta, "http": rv.status_code, "vendas": len(vendas)})
            except Exception as e:
                resultados.append({"conta": conta, "erro": str(e)})
                diag.append({"conta": conta, "erro": str(e)}); continue
            for v in vendas:
                oid = str(v.get("order_id") or "")
                if not oid or v.get("status") != "paid" or oid in processadas:
                    continue
                for it in (v.get("itens") or []):
                    sku = str(it.get("sku") or "").strip()
                    if not sku:
                        continue
                    if modo == "armado":
                        rel = executar_sincronizacao(sku, "ml")
                        resultados.append({"order_id": oid, "conta": conta, "sku": sku,
                                           "pausados": rel.get("pausados"), "total": rel.get("total")})
                    else:
                        plano = planejar_pausa(sku, "ml")
                        _registrar_log({"sku": sku, "origem": "ml-observacao",
                                        "pausados": 0, "total": plano["total"], "resultados": plano["acoes"]})
                        resultados.append({"order_id": oid, "conta": conta, "sku": sku,
                                           "pausaria": plano["total"]})
                processadas.add(oid)
                novas.append(oid)
        _salvar_processadas(processadas)
        r = jsonify({"ok": True, "modo": modo, "vendas_novas": len(novas), "diag": diag, "resultados": resultados})
        r.headers["Access-Control-Allow-Origin"] = "*"
        return r

    @bp.route("/integracoes/sincronizar-venda", methods=["POST", "GET", "OPTIONS"])
    def sincronizar_venda():
        if request.method == "OPTIONS":
            return _cors()
        if request.method == "GET":
            sku = request.args.get("sku", "")
            origem = request.args.get("origem", "")
            dry = request.args.get("dry", "1") == "1"   # GET é dry-run por padrão (seguro)
        else:
            data = request.get_json(force=True) or {}
            sku = (data.get("sku") or "").strip()
            origem = (data.get("origem") or "").strip()
            dry = bool(data.get("dry_run", False))
        if not sku:
            resp = jsonify({"ok": False, "erro": "sku obrigatorio"})
            resp.headers["Access-Control-Allow-Origin"] = "*"
            return resp, 400
        rel = executar_sincronizacao(sku, origem, dry_run=dry)
        resp = jsonify({"ok": True, **rel})
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

    return bp

# ─── CLI: teste local em dry-run (não pausa nada de verdade) ─────────────────────
if __name__ == "__main__":
    import sys
    sku = sys.argv[1] if len(sys.argv) > 1 else "8593"
    origem = sys.argv[2] if len(sys.argv) > 2 else "ml"
    print(f"\n[DRY-RUN] o que seria pausado se o SKU {sku} vendesse em '{origem}':\n")
    plano = planejar_pausa(sku, origem)
    if not plano["acoes"]:
        print("  (nenhum anuncio encontrado pra esse SKU em ml_anuncios/shopee_anuncios)")
    for a in plano["acoes"]:
        alvo = a.get("alvo")
        onde = a.get("conta") or a.get("shop_id")
        titulo = (a.get("titulo", "") or "")[:55].encode("ascii", "ignore").decode()
        print(f"  - {a['canal'].upper():7} [{onde}] {alvo}  -- {titulo}")
    print(f"\n  Total: {plano['total']} anuncio(s) seriam pausados.\n")
