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
import threading
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
SHOPS_SHOPEE = ["1545866669", "234248614"]  # as 2 lojas Shopee

def _shopee_call(shop_id, path, params=None, raw=False):
    """Chamada GET assinada à API Shopee. raw=True retorna o objeto Response (p/ baixar PDF)."""
    if not _shopee_token_provider or not _shopee_partner_id:
        return None
    access_token, shop_id_int = _shopee_token_provider(shop_id)
    if not access_token:
        return None
    ts = int(time.time())
    base = f"{_shopee_partner_id}{path}{ts}{access_token}{shop_id_int}"
    sign = hmac.new(_shopee_partner_key.encode(), base.encode(), hashlib.sha256).hexdigest()
    p = {"partner_id": _shopee_partner_id, "timestamp": ts, "access_token": access_token,
         "shop_id": shop_id_int, "sign": sign}
    if params:
        p.update(params)
    try:
        r = requests.get(f"{_shopee_base}{path}", params=p, timeout=25)
        if raw:
            return r
        return r.json()
    except Exception as e:
        print(f"[SHOPEE] call erro {path}: {e}")
        return None

def _shopee_post(shop_id, path, body, raw=False):
    """Chamada POST assinada à API Shopee. raw=True retorna o Response (p/ baixar PDF)."""
    if not _shopee_token_provider or not _shopee_partner_id:
        return None
    access_token, shop_id_int = _shopee_token_provider(shop_id)
    if not access_token:
        return None
    ts = int(time.time())
    base = f"{_shopee_partner_id}{path}{ts}{access_token}{shop_id_int}"
    sign = hmac.new(_shopee_partner_key.encode(), base.encode(), hashlib.sha256).hexdigest()
    p = {"partner_id": _shopee_partner_id, "timestamp": ts, "access_token": access_token,
         "shop_id": shop_id_int, "sign": sign}
    try:
        r = requests.post(f"{_shopee_base}{path}", params=p, json=body, timeout=30)
        return r if raw else r.json()
    except Exception as e:
        print(f"[SHOPEE] post erro {path}: {e}")
        return None
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

# B5: cancelamentos (idempotência separada das vendas)
_CANC_FILE = os.path.join(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "/tmp"), "wrx_cancelamentos_processados.json")

def _carregar_canc():
    try:
        with open(_CANC_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()

def _salvar_canc(s):
    try:
        with open(_CANC_FILE, "w", encoding="utf-8") as f:
            json.dump(list(s)[-5000:], f)
    except Exception as e:
        print(f"[B5] nao salvou cancelamentos: {e}")

def _estoque_atual(sku):
    """qtd atual da peça em `pecas`, ou None se o SKU não está lá (isca/avulso)."""
    try:
        r = requests.get(f"{SB_URL}/rest/v1/pecas_estoque",
                         params={"sku": f"eq.{sku}", "select": "qtd"}, headers=_sb_headers(), timeout=10)
        if r.status_code == 200 and r.json():
            return int(r.json()[0].get("qtd") or 0)
    except Exception as e:
        print(f"[estoque] atual erro: {e}")
    return None


def _anuncios_paused_do_sku(sku):
    """Anúncios ML paused desse SKU (candidatos a reativar no cancelamento)."""
    try:
        r = requests.get(f"{SB_URL}/rest/v1/ml_anuncios",
                         params={"status": "eq.paused", "sku": f"eq.{sku}", "select": "ml_id,conta,titulo"},
                         headers=_sb_headers(), timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"[B5] paused_do_sku erro: {e}")
    return []

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
        r = requests.get(f"{SB_URL}/rest/v1/pecas_estoque",
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
            _atualizar_cache_ml(ml_id, "paused")  # mantém ml_anuncios fiel (evita re-flag)
            return True, "pausado"
        # Se o ML diz que o item já está CLOSED/inválido, o cache estava desatualizado:
        # corrige pra 'closed' (não é furo real, o anúncio já não está à venda).
        txt = r.text[:200]
        if r.status_code == 400 and ("closed" in txt or "item.status.invalid" in txt):
            _atualizar_cache_ml(ml_id, "closed")
            return False, "ja fechado no ML (cache corrigido)"
        return False, f"HTTP {r.status_code}: {txt}"
    except Exception as e:
        return False, str(e)

def _atualizar_cache_ml(ml_id, status):
    """Reflete no cache ml_anuncios o status real após pausar/reativar no ML."""
    try:
        requests.patch(f"{SB_URL}/rest/v1/ml_anuncios?ml_id=eq.{ml_id}",
                       headers={**_sb_headers(), "Content-Type": "application/json"},
                       json={"status": status}, timeout=10)
    except Exception:
        pass

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
            _atualizar_cache_ml(ml_id, "active")
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
        rj = {}
        try:
            rj = r.json()
        except Exception:
            pass
        resp = rj.get("response") or {}
        # sucesso = sem erro de topo E o item esta na success_list
        sucesso = (r.status_code == 200 and not rj.get("error")
                   and any(str(s.get("item_id")) == str(item_id) for s in (resp.get("success_list") or [])))
        # motivo de falha por item (Shopee devolve failure_list com failed_reason)
        fl = resp.get("failure_list") or []
        motivo = " | ".join(
            f"{f.get('item_id')}: {f.get('failed_reason') or f.get('failed_message') or f}"
            for f in fl) if fl else (rj.get("message") or r.text[:200])
        low = (motivo or "").lower()
        # ja inativo no Shopee (vendido/unlisted/inexistente) -> nao e furo: corrige cache
        ja_inativo = any(k in low for k in
                         ["unlist", "not exist", "not found", "deleted", "banned",
                          "sold", "not in", "invalid item", "out of stock",
                          "abnormal", "status is", "prohibited", "frozen", "review"])
        if sucesso or ja_inativo:
            try:
                requests.patch(f"{SB_URL}/rest/v1/shopee_anuncios?shop_id=eq.{shop_id}&item_id=eq.{item_id}",
                               headers={**_sb_headers(), "Content-Type": "application/json"},
                               json={"status": "UNLIST"}, timeout=10)
            except Exception:
                pass
            return True, ("unlist" if sucesso else f"ja inativo ({motivo})")
        return False, f"shopee unlist falhou: {motivo}"
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

# ─── Webhook de venda ML (anti-venda-dupla INSTANTÂNEO) ──────────────────────────
# O ML chama nossa URL no segundo em que vende. Reagimos na hora: busca o pedido,
# pega o SKU e dispara a cascata (pausa os OUTROS anúncios do mesmo SKU). A trava
# de peça única vive em planejar_pausa/_peca_unica (não pausa peça multi-unidade).
_ml_user2conta = {}   # cache user_id(str) -> conta ML

def _ml_conta_por_user(user_id):
    """Descobre qual conta (default/geisa) é dona do user_id do ML (cacheado)."""
    uid = str(user_id or "")
    if uid in _ml_user2conta:
        return _ml_user2conta[uid]
    if not _ml_token_provider:
        return None
    for conta in CONTAS_ML:
        token = _ml_token_provider(conta)
        if not token:
            continue
        try:
            me = requests.get("https://api.mercadolibre.com/users/me",
                              headers={"Authorization": f"Bearer {token}"}, timeout=10)
            if me.status_code == 200:
                _ml_user2conta[str(me.json().get("id"))] = conta
        except Exception:
            pass
    return _ml_user2conta.get(uid)

def _processar_venda_ml_webhook(resource, user_id):
    """Roda em thread (não trava a resposta ao ML): pedido -> SKU(s) -> cascata."""
    try:
        order_id = str(resource or "").rstrip("/").split("/")[-1]
        if not order_id.isdigit():
            return
        if not _ml_token_provider:
            print(f"[WEBHOOK] sem token provider (order {order_id})")
            return
        # A dona do pedido e a UNICA conta cujo token devolve 200. Tenta a conta
        # mapeada pelo user_id primeiro (rapido) e, se falhar, as demais (robusto
        # mesmo se o user_id nao resolver).
        c = _ml_conta_por_user(user_id)
        contas = ([c] + [x for x in CONTAS_ML if x != c]) if c else CONTAS_ML
        od = None
        for conta in contas:
            token = _ml_token_provider(conta)
            if not token:
                continue
            o = requests.get(f"https://api.mercadolibre.com/orders/{order_id}",
                             headers={"Authorization": f"Bearer {token}"}, timeout=15)
            if o.status_code == 200:
                od = o.json()
                break
        if not od:
            print(f"[WEBHOOK] order {order_id} nao encontrado em nenhuma conta ML")
            return
        if od.get("status") != "paid":
            return  # só reage a venda PAGA
        for it in (od.get("order_items") or []):
            item = it.get("item") or {}
            sku = str(item.get("seller_sku") or item.get("seller_custom_field") or "").strip()
            if not sku:
                continue
            rel = executar_sincronizacao(sku, "ml")
            print(f"[WEBHOOK] order {order_id} sku {sku} -> pausados {rel.get('pausados')}/{rel.get('total')}")
    except Exception as e:
        print(f"[WEBHOOK] erro: {e}")

# ─── Flask Blueprint (registrado pelo api_server com 1 linha) ────────────────────
def get_blueprint():
    from flask import Blueprint, request, jsonify, Response
    bp = Blueprint("sync_multicanal", __name__)

    def _cors():
        return Response(status=204, headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization"})

    @bp.route("/integracoes/mercadolivre/webhook", methods=["POST", "GET", "OPTIONS"])
    def ml_webhook():
        """Recebe as notificações do ML. GET = validação/health; POST = notificação.
        Responde 200 RÁPIDO e processa a venda em thread (o ML exige resposta ágil)."""
        if request.method == "OPTIONS":
            return _cors()
        if request.method == "GET":
            return jsonify({"ok": True, "webhook": "ml"}), 200
        data = request.get_json(force=True, silent=True) or {}
        topic = (data.get("topic") or "").lower()
        if topic in ("orders_v2", "orders"):
            threading.Thread(
                target=_processar_venda_ml_webhook,
                args=(data.get("resource"), data.get("user_id")),
                daemon=True).start()
        return jsonify({"ok": True}), 200

    @bp.route("/integracoes/shopee-envio-diag", methods=["GET", "OPTIONS"])
    def shopee_envio_diag():
        """Diagnóstico do ENVIO (logística) do pedido: método (pickup/dropoff), se já tem tracking."""
        if request.method == "OPTIONS":
            return _cors()
        order_sn = request.args.get("order_sn", "").strip()
        shop = request.args.get("shop", "").strip()
        sp = _shopee_call(shop, "/api/v2/logistics/get_shipping_parameter", {"order_sn": order_sn})
        tn = _shopee_call(shop, "/api/v2/logistics/get_tracking_number", {"order_sn": order_sn})
        od = _shopee_call(shop, "/api/v2/order/get_order_detail",
                          {"order_sn_list": order_sn, "response_optional_fields": "order_status,shipping_carrier"})
        ti = _shopee_call(shop, "/api/v2/logistics/get_tracking_info", {"order_sn": order_sn})
        r = jsonify({"shipping_parameter": sp, "tracking_number": tn, "order_detail": od, "tracking_info": ti})
        r.headers["Access-Control-Allow-Origin"] = "*"; return r

    @bp.route("/integracoes/shopee-etiqueta-diag", methods=["GET", "OPTIONS"])
    def shopee_etiqueta_diag():
        """Diagnóstico do fluxo de etiqueta Shopee: tipos suportados, create, result."""
        if request.method == "OPTIONS":
            return _cors()
        order_sn = request.args.get("order_sn", "").strip()
        shop = request.args.get("shop", "").strip()
        b = {"order_list": [{"order_sn": order_sn}]}
        param = _shopee_post(shop, "/api/v2/logistics/get_shipping_document_parameter", b)
        # tipo sugerido pelo parameter
        info = (((param or {}).get("response", {}) or {}).get("result_list", []) or [{}])[0].get("info_list", [{}])
        tipos = info[0].get("selectable_shipping_document_type") if info else None
        sugerido = info[0].get("suggest_shipping_document_type") if info else None
        tipo = request.args.get("tipo", "").strip() or sugerido or (tipos[0] if tipos else "NORMAL_AIR_WAYBILL")
        # pega o package_number (muitos pedidos exigem no create)
        od = _shopee_call(shop, "/api/v2/order/get_order_detail",
                          {"order_sn_list": order_sn, "response_optional_fields": "package_list"})
        pkgs = (((od or {}).get("response", {}) or {}).get("order_list", [{}]) or [{}])[0].get("package_list", []) or []
        pkg = pkgs[0].get("package_number") if pkgs else None
        item = {"order_sn": order_sn, "shipping_document_type": tipo}
        if pkg:
            item["package_number"] = pkg
        bt = {"order_list": [item]}
        create = _shopee_post(shop, "/api/v2/logistics/create_shipping_document", bt)
        time.sleep(2)
        result = _shopee_post(shop, "/api/v2/logistics/get_shipping_document_result", bt)
        r = jsonify({"tipo_usado": tipo, "package_number": pkg, "param": param, "create": create, "result": result})
        r.headers["Access-Control-Allow-Origin"] = "*"; return r

    @bp.route("/integracoes/shopee-etiqueta", methods=["GET", "OPTIONS"])
    def shopee_etiqueta():
        """Baixa e serve a etiqueta de envio (PDF) de um pedido Shopee.
        ?order_sn=..&shop=.. (cria o documento se preciso, depois baixa)."""
        from flask import Response
        if request.method == "OPTIONS":
            return _cors()
        order_sn = request.args.get("order_sn", "").strip()
        shop = request.args.get("shop", "").strip()
        if not (order_sn and shop):
            return Response('{"erro":"order_sn e shop obrigatorios"}', status=400,
                            mimetype="application/json", headers={"Access-Control-Allow-Origin": "*"})
        # 0) GARANTE o despacho (ship_order) — sem tracking a etiqueta NUNCA fica pronta
        #    (erro "logistics.package_can_not_print"). Método dropoff (info_needed vazio).
        tn = _shopee_call(shop, "/api/v2/logistics/get_tracking_number", {"order_sn": order_sn})
        tracking = (((tn or {}).get("response", {}) or {}).get("tracking_number") or "")
        if not tracking:
            sp = _shopee_call(shop, "/api/v2/logistics/get_shipping_parameter", {"order_sn": order_sn})
            resp = (sp or {}).get("response", {}) or {}
            # escolhe método disponível: dropoff (padrão) ou pickup
            if resp.get("pickup") is not None and resp.get("dropoff") is None:
                _shopee_post(shop, "/api/v2/logistics/ship_order", {"order_sn": order_sn, "pickup": {}})
            else:
                _shopee_post(shop, "/api/v2/logistics/ship_order", {"order_sn": order_sn, "dropoff": {}})
            time.sleep(3)
        # 1) descobre tipos de documento válidos + package_number (pedido pode exigir)
        pinfo = _shopee_post(shop, "/api/v2/logistics/get_shipping_document_parameter",
                             {"order_list": [{"order_sn": order_sn}]})
        info = (((pinfo or {}).get("response", {}) or {}).get("result_list", []) or [{}])[0].get("info_list", [{}]) or [{}]
        sel = info[0].get("selectable_shipping_document_type") or []
        sug = info[0].get("suggest_shipping_document_type")
        tipos = []
        for t in ([sug] + list(sel) + ["NORMAL_AIR_WAYBILL", "THERMAL_AIR_WAYBILL"]):
            if t and t not in tipos:
                tipos.append(t)
        od = _shopee_call(shop, "/api/v2/order/get_order_detail",
                          {"order_sn_list": order_sn, "response_optional_fields": "package_list"})
        pkgs = (((od or {}).get("response", {}) or {}).get("order_list", [{}]) or [{}])[0].get("package_list", []) or []
        pkg = pkgs[0].get("package_number") if pkgs else None

        def _pdf_resp(r):
            return (r is not None and r.status_code == 200
                    and r.headers.get("content-type", "").lower().startswith("application/pdf"))
        def _combos():
            for tipo in tipos:
                for usar_pkg in ([True, False] if pkg else [False]):
                    item = {"order_sn": order_sn, "shipping_document_type": tipo}
                    if usar_pkg and pkg:
                        item["package_number"] = pkg
                    yield {"order_list": [item]}

        ultimo = "sem resposta"
        # FASE 1 (rápida): doc pode JÁ existir (gerado antes/painel/cron) → só baixa
        for body in _combos():
            r = _shopee_post(shop, "/api/v2/logistics/download_shipping_document", body, raw=True)
            if _pdf_resp(r):
                return Response(r.content, mimetype="application/pdf", headers={
                    "Access-Control-Allow-Origin": "*",
                    "Content-Disposition": f'inline; filename="etiqueta_shopee_{order_sn}.pdf"'})
            try:
                ultimo = (r.json() if r is not None else {}).get("message", "")[:160] or ultimo
            except Exception:
                ultimo = (r.text[:160] if r is not None else ultimo)
        # FASE 2: não existe ainda → cria no tipo sugerido e espera pouco (evita timeout)
        _item = {"order_sn": order_sn, "shipping_document_type": tipos[0] if tipos else "NORMAL_AIR_WAYBILL"}
        if pkg:
            _item["package_number"] = pkg
        body = {"order_list": [_item]}
        _shopee_post(shop, "/api/v2/logistics/create_shipping_document", body)
        for _ in range(4):
            res = _shopee_post(shop, "/api/v2/logistics/get_shipping_document_result", body)
            rl = ((res or {}).get("response", {}) or {}).get("result_list", [])
            if rl and rl[0].get("status") == "READY":
                break
            time.sleep(3)
        r = _shopee_post(shop, "/api/v2/logistics/download_shipping_document", body, raw=True)
        if _pdf_resp(r):
            return Response(r.content, mimetype="application/pdf", headers={
                "Access-Control-Allow-Origin": "*",
                "Content-Disposition": f'inline; filename="etiqueta_shopee_{order_sn}.pdf"'})
        try:
            ultimo = (r.json() if r is not None else {}).get("message", "")[:160] or ultimo
        except Exception:
            ultimo = (r.text[:160] if r is not None else ultimo)
        return Response('{"erro":"etiqueta nao disponivel","detalhe":"%s"}' % ultimo, status=422,
                        mimetype="application/json", headers={"Access-Control-Allow-Origin": "*"})

    @bp.route("/integracoes/shopee-a-enviar", methods=["GET", "OPTIONS"])
    def shopee_a_enviar():
        """Lista pedidos Shopee prontos pra enviar (READY_TO_SHIP) — order_sn, sku, cliente, package."""
        if request.method == "OPTIONS":
            return _cors()
        debug = request.args.get("debug") == "1"
        pedidos = []
        diag = []
        agora = int(time.time())
        for shop in SHOPS_SHOPEE:
            sns = []
            _at = (_shopee_token_provider(shop) if _shopee_token_provider else (None, None))
            shop_diag = {"shop": shop, "token": bool(_at and _at[0]), "por_status": {}, "erros": []}
            for status in ("READY_TO_SHIP", "PROCESSED"):  # a despachar
                d = _shopee_call(shop, "/api/v2/order/get_order_list", {
                    "order_status": status, "page_size": 50, "time_range_field": "create_time",
                    "time_from": agora - 14 * 86400, "time_to": agora})
                _ol = ((d or {}).get("response", {}) or {}).get("order_list", [])
                shop_diag["por_status"][status] = len(_ol)
                if d and d.get("error"):
                    shop_diag["erros"].append({"status": status, "error": d.get("error"), "message": d.get("message")})
                for o in _ol:
                    if o.get("order_sn"):
                        sns.append(o["order_sn"])
            diag.append(shop_diag)
            for i in range(0, len(sns), 50):
                dd = _shopee_call(shop, "/api/v2/order/get_order_detail", {
                    "order_sn_list": ",".join(sns[i:i+50]),
                    "response_optional_fields": "order_status,buyer_username,item_list,recipient_address,package_list"})
                for o in ((dd or {}).get("response", {}) or {}).get("order_list", []):
                    pkg = (o.get("package_list") or [{}])
                    pedidos.append({"order_sn": o.get("order_sn"), "shop": shop,
                                    "cliente": o.get("buyer_username") or (o.get("recipient_address") or {}).get("name"),
                                    "package": pkg[0].get("package_number") if pkg else None,
                                    "itens": [{"sku": x.get("item_sku"), "titulo": x.get("item_name")}
                                              for x in (o.get("item_list") or [])]})
        out = {"ok": True, "total": len(pedidos), "pedidos": pedidos}
        if debug:
            out["diag"] = diag
        r = jsonify(out)
        r.headers["Access-Control-Allow-Origin"] = "*"
        return r

    @bp.route("/integracoes/shopee-pregerar-etiquetas", methods=["GET", "POST", "OPTIONS"])
    def shopee_pregerar_etiquetas():
        """CRON: pré-gera as etiquetas dos pedidos Shopee a despachar (ship_order + create_shipping_document).
        A Shopee demora pra validar o tracking/gerar o doc — pré-gerando, na conferência a etiqueta já está pronta."""
        if request.method == "OPTIONS":
            return _cors()
        feitos = []; jatinha = []; erros = []
        agora = int(time.time())
        for shop in SHOPS_SHOPEE:
            sns = []
            for status in ("READY_TO_SHIP", "PROCESSED"):
                d = _shopee_call(shop, "/api/v2/order/get_order_list", {
                    "order_status": status, "page_size": 50, "time_range_field": "create_time",
                    "time_from": agora - 14 * 86400, "time_to": agora})
                for o in ((d or {}).get("response", {}) or {}).get("order_list", []):
                    if o.get("order_sn"):
                        sns.append(o["order_sn"])
            for sn in sns:
                try:
                    tn = _shopee_call(shop, "/api/v2/logistics/get_tracking_number", {"order_sn": sn})
                    tracking = (((tn or {}).get("response", {}) or {}).get("tracking_number") or "")
                    if not tracking:
                        sp = _shopee_call(shop, "/api/v2/logistics/get_shipping_parameter", {"order_sn": sn})
                        resp = (sp or {}).get("response", {}) or {}
                        if resp.get("pickup") is not None and resp.get("dropoff") is None:
                            _shopee_post(shop, "/api/v2/logistics/ship_order", {"order_sn": sn, "pickup": {}})
                        else:
                            _shopee_post(shop, "/api/v2/logistics/ship_order", {"order_sn": sn, "dropoff": {}})
                        feitos.append(sn)
                    else:
                        jatinha.append(sn)
                    # inicia a geração do documento (assíncrono) — não baixa aqui
                    _shopee_post(shop, "/api/v2/logistics/create_shipping_document",
                                 {"order_list": [{"order_sn": sn, "shipping_document_type": "NORMAL_AIR_WAYBILL"}]})
                except Exception as e:
                    erros.append({"order_sn": sn, "erro": str(e)[:120]})
        r = jsonify({"ok": True, "despachados_agora": len(feitos), "ja_despachados": len(jatinha), "erros": erros})
        r.headers["Access-Control-Allow-Origin"] = "*"
        return r

    @bp.route("/integracoes/ml-a-enviar", methods=["GET", "OPTIONS"])
    def ml_a_enviar():
        """Lista pedidos ML prontos pra enviar (ready_to_ship) — pra tela de expedição.
        Retorna order_id, conta, cliente, itens (titulo+sku) e shipment_id."""
        if request.method == "OPTIONS":
            return _cors()
        contas = [request.args.get("conta")] if request.args.get("conta") else CONTAS_ML
        pedidos = []
        for conta in contas:
            token = _ml_token_provider(conta) if _ml_token_provider else None
            if not token:
                continue
            H = {"Authorization": f"Bearer {token}"}
            try:
                rv = requests.get(f"{SELF_BASE}/integracoes/mercadolivre/vendas-recentes",
                                  params={"conta": conta, "dias": 7}, timeout=60)
                vendas = rv.json().get("vendas", []) if rv.status_code == 200 else []
            except Exception:
                vendas = []
            for v in vendas[:25]:
                if v.get("status") != "paid":
                    continue
                oid = v.get("order_id")
                try:
                    o = requests.get(f"https://api.mercadolibre.com/orders/{oid}", headers=H, timeout=12)
                    if o.status_code != 200:
                        continue
                    ship_id = (o.json().get("shipping") or {}).get("id")
                    if not ship_id:
                        continue
                    sr = requests.get(f"https://api.mercadolibre.com/shipments/{ship_id}", headers=H, timeout=12)
                    if sr.status_code != 200 or sr.json().get("status") != "ready_to_ship":
                        continue
                    pedidos.append({"order_id": oid, "conta": conta, "cliente": v.get("comprador"),
                                    "data": v.get("data"), "shipment_id": ship_id,
                                    "itens": [{"titulo": it.get("titulo"), "sku": it.get("sku")}
                                              for it in (v.get("itens") or [])]})
                except Exception:
                    continue
        r = jsonify({"ok": True, "total": len(pedidos), "pedidos": pedidos})
        r.headers["Access-Control-Allow-Origin"] = "*"
        return r

    @bp.route("/integracoes/ml-nota", methods=["GET", "OPTIONS"])
    def ml_nota():
        """SERVE o DANFE (PDF) ou XML da nota fiscal emitida de um pedido ML.
        ?order_id=..&conta=..&fmt=pdf|xml. Requer nota emitida pelo Faturador do ML."""
        from flask import Response
        if request.method == "OPTIONS":
            return _cors()
        order_id = request.args.get("order_id", "").strip()
        conta = request.args.get("conta", "default").strip()
        fmt = request.args.get("fmt", "pdf").strip()
        token = _ml_token_provider(conta) if _ml_token_provider else None
        if not token:
            return Response('{"erro":"Mercado Livre desconectado"}', status=409,
                            mimetype="application/json", headers={"Access-Control-Allow-Origin": "*"})
        H = {"Authorization": f"Bearer {token}"}
        me = requests.get("https://api.mercadolibre.com/users/me", headers=H, timeout=15)
        uid = me.json().get("id") if me.status_code == 200 else None
        o = requests.get(f"https://api.mercadolibre.com/orders/{order_id}", headers=H, timeout=15)
        ship_id = (o.json().get("shipping") or {}).get("id") if o.status_code == 200 else None
        if not (uid and ship_id):
            return Response('{"erro":"pedido/envio nao encontrado"}', status=404,
                            mimetype="application/json", headers={"Access-Control-Allow-Origin": "*"})
        inv = requests.get(f"https://api.mercadolibre.com/users/{uid}/invoices/shipments/{ship_id}", headers=H, timeout=15)
        if inv.status_code != 200:
            return Response('{"erro":"nota nao encontrada (emitida por fora do Faturador do ML?)"}',
                            status=404, mimetype="application/json", headers={"Access-Control-Allow-Origin": "*"})
        attrs = (inv.json() or {}).get("attributes") or {}
        loc = attrs.get("danfe_location") if fmt == "pdf" else attrs.get("xml_location")
        if not loc:
            return Response('{"erro":"documento nao disponivel"}', status=422,
                            mimetype="application/json", headers={"Access-Control-Allow-Origin": "*"})
        doc = requests.get(f"https://api.mercadolibre.com{loc}", headers=H, timeout=25)
        if doc.status_code != 200:
            return Response('{"erro":"falha ao baixar o documento","http":%d}' % doc.status_code,
                            status=422, mimetype="application/json", headers={"Access-Control-Allow-Origin": "*"})
        ct = "application/pdf" if fmt == "pdf" else "application/xml"
        return Response(doc.content, mimetype=ct, headers={
            "Access-Control-Allow-Origin": "*",
            "Content-Disposition": f'inline; filename="nota_{order_id}.{ "pdf" if fmt=="pdf" else "xml" }"'})

    @bp.route("/integracoes/ml-etiqueta", methods=["GET", "OPTIONS"])
    def ml_etiqueta():
        """Baixa e SERVE a etiqueta de envio (PDF) de um pedido ML, pronta pra imprimir.
        ?order_id=...&conta=... (&fmt=pdf|zpl2). Requer token ML conectado."""
        from flask import Response
        if request.method == "OPTIONS":
            return _cors()
        order_id = request.args.get("order_id", "").strip()
        conta = request.args.get("conta", "default").strip()
        fmt = request.args.get("fmt", "pdf").strip()
        token = _ml_token_provider(conta) if _ml_token_provider else None
        if not token:
            return Response('{"erro":"Mercado Livre desconectado — reconecte a conta"}',
                            status=409, mimetype="application/json", headers={"Access-Control-Allow-Origin": "*"})
        H = {"Authorization": f"Bearer {token}"}
        # pega o shipment_id do pedido
        o = requests.get(f"https://api.mercadolibre.com/orders/{order_id}", headers=H, timeout=15)
        if o.status_code != 200:
            return Response('{"erro":"pedido nao encontrado"}', status=404, mimetype="application/json",
                            headers={"Access-Control-Allow-Origin": "*"})
        ship_id = (o.json().get("shipping") or {}).get("id")
        if not ship_id:
            return Response('{"erro":"pedido sem envio do Mercado Envios (etiqueta indisponivel)"}',
                            status=422, mimetype="application/json", headers={"Access-Control-Allow-Origin": "*"})
        # baixa a etiqueta
        lr = requests.get("https://api.mercadolibre.com/shipment_labels",
                          params={"shipment_ids": ship_id, "response_type": fmt}, headers=H, timeout=25)
        if lr.status_code != 200:
            return Response('{"erro":"etiqueta ainda nao disponivel (emita a nota / aguarde pronto p/ envio)","http":%d}' % lr.status_code,
                            status=422, mimetype="application/json", headers={"Access-Control-Allow-Origin": "*"})
        ct = "application/pdf" if fmt == "pdf" else "application/octet-stream"
        return Response(lr.content, mimetype=ct, headers={
            "Access-Control-Allow-Origin": "*",
            "Content-Disposition": f'inline; filename="etiqueta_{order_id}.{ "pdf" if fmt=="pdf" else "zpl" }"'})

    @bp.route("/integracoes/ml-nota-info", methods=["GET", "OPTIONS"])
    def ml_nota_info():
        """TESTE: acha a nota fiscal emitida de um pedido (via pack_id → fiscal_documents)."""
        if request.method == "OPTIONS":
            return _cors()
        order_id = request.args.get("order_id", "").strip()
        conta = request.args.get("conta", "default").strip()
        token = _ml_token_provider(conta) if _ml_token_provider else None
        if not token:
            r = jsonify({"erro": "sem token"}); r.headers["Access-Control-Allow-Origin"] = "*"; return r
        H = {"Authorization": f"Bearer {token}"}
        me = requests.get("https://api.mercadolibre.com/users/me", headers=H, timeout=15)
        uid = me.json().get("id") if me.status_code == 200 else None
        o = requests.get(f"https://api.mercadolibre.com/orders/{order_id}", headers=H, timeout=15)
        order = o.json() if o.status_code == 200 else {}
        ship_id = (order.get("shipping") or {}).get("id")
        inv = requests.get(f"https://api.mercadolibre.com/users/{uid}/invoices/shipments/{ship_id}", headers=H, timeout=15)
        out = {"order_id": order_id, "shipment_id": ship_id, "user_id": uid,
               "invoice_http": inv.status_code,
               "invoice_raw": inv.json() if inv.status_code == 200 else inv.text[:400]}
        r = jsonify(out); r.headers["Access-Control-Allow-Origin"] = "*"; return r

    @bp.route("/integracoes/ml-billing-teste", methods=["GET", "OPTIONS"])
    def ml_billing_teste():
        """TESTE CRÍTICO: o ML libera o CPF/CNPJ do comprador (pra NF-e)?"""
        if request.method == "OPTIONS":
            return _cors()
        order_id = request.args.get("order_id", "").strip()
        conta = request.args.get("conta", "default").strip()
        token = _ml_token_provider(conta) if _ml_token_provider else None
        if not token:
            r = jsonify({"erro": "sem token"}); r.headers["Access-Control-Allow-Origin"] = "*"; return r
        H = {"Authorization": f"Bearer {token}", "x-version": "2"}
        bi = requests.get(f"https://api.mercadolibre.com/orders/{order_id}/billing_info", headers=H, timeout=15)
        body = bi.json() if bi.status_code == 200 else {}
        binfo = (body.get("buyer") or {}).get("billing_info") or {}
        ident = binfo.get("identification") or {}
        addr = binfo.get("address") or {}
        doc_number = ident.get("number")
        nome = " ".join(filter(None, [binfo.get("name"), binfo.get("last_name")])) or None
        out = {"http": bi.status_code, "tem_doc": bool(doc_number),
               "doc_type": ident.get("type"),
               "doc_number": ("***" + str(doc_number)[-3:]) if doc_number else None,
               "nome": nome,
               "tem_endereco": bool(addr.get("zip_code") or addr.get("street_name")),
               "cep": addr.get("zip_code"), "cidade": addr.get("city"), "estado": addr.get("state"),
               "campos_address": list(addr.keys())}
        r = jsonify(out); r.headers["Access-Control-Allow-Origin"] = "*"; return r

    @bp.route("/integracoes/ml-etiqueta-info", methods=["GET", "OPTIONS"])
    def ml_etiqueta_info():
        """TESTE: dado um order_id, verifica se a etiqueta de envio do ML está disponível."""
        if request.method == "OPTIONS":
            return _cors()
        order_id = request.args.get("order_id", "").strip()
        conta = request.args.get("conta", "default").strip()
        token = _ml_token_provider(conta) if _ml_token_provider else None
        if not token:
            r = jsonify({"erro": "sem token"}); r.headers["Access-Control-Allow-Origin"] = "*"; return r
        H = {"Authorization": f"Bearer {token}"}
        # teste de saúde do token
        me = requests.get("https://api.mercadolibre.com/users/me", headers=H, timeout=15)
        o = requests.get(f"https://api.mercadolibre.com/orders/{order_id}", headers=H, timeout=15)
        order = o.json() if o.status_code == 200 else {}
        _order_http = o.status_code
        _order_err = (o.text[:160] if o.status_code != 200 else None)
        _me_http = me.status_code
        shipping = order.get("shipping") or {}
        ship_id = shipping.get("id")
        shipment = {}
        if ship_id:
            sr = requests.get(f"https://api.mercadolibre.com/shipments/{ship_id}", headers=H, timeout=15)
            shipment = sr.json() if sr.status_code == 200 else {}
        lab_http, lab_bytes, lab_ct = None, 0, None
        if ship_id:
            lr = requests.get(f"https://api.mercadolibre.com/shipment_labels",
                              params={"shipment_ids": ship_id, "response_type": "pdf"}, headers=H, timeout=20)
            lab_http = lr.status_code
            lab_ct = lr.headers.get("Content-Type")
            lab_bytes = len(lr.content) if lr.status_code == 200 else 0
        out = {"order_id": order_id, "me_http": _me_http, "order_http": _order_http, "order_err": _order_err,
               "order_status": order.get("status"),
               "shipment_id": ship_id, "ship_status": shipment.get("status"),
               "ship_substatus": shipment.get("substatus"), "logistic_type": shipment.get("logistic_type"),
               "etiqueta_http": lab_http, "etiqueta_content_type": lab_ct, "etiqueta_bytes": lab_bytes}
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

    @bp.route("/integracoes/reconciliar-estoque-zerado", methods=["POST", "GET", "OPTIONS"])
    def reconciliar_estoque_zerado():
        """Cobre o BALCÃO e qualquer baixa: acha peças com estoque ZERADO que ainda
        têm anúncio ATIVO em algum marketplace e pausa — não importa como zerou.
        Em LOTE (sku=in.(...)) pra ser rápido. ?modo=observacao | armado."""
        if request.method == "OPTIONS":
            return _cors()
        modo = request.args.get("modo", "observacao").strip()
        try:
            # Supabase corta em 1000/req — pagina com Range pra pegar TODAS as zeradas.
            skus_set, offset = set(), 0
            while True:
                r = requests.get(f"{SB_URL}/rest/v1/pecas_estoque",
                                 params={"qtd": "lte.0", "select": "sku"},
                                 headers={**_sb_headers(), "Range-Unit": "items",
                                          "Range": f"{offset}-{offset+999}"}, timeout=30)
                if r.status_code not in (200, 206):
                    break
                lote = r.json()
                for p in lote:
                    if p.get("sku"):
                        skus_set.add(str(p["sku"]).strip())
                if len(lote) < 1000:
                    break
                offset += 1000
            skus = sorted(skus_set)
        except Exception as e:
            rr = jsonify({"ok": False, "erro": str(e)}); rr.headers["Access-Control-Allow-Origin"] = "*"; return rr

        risco_ml, risco_shopee = [], []
        for i in range(0, len(skus), 80):  # chunks p/ não estourar a URL
            lista = ",".join(skus[i:i+80])
            try:
                rm = requests.get(f"{SB_URL}/rest/v1/ml_anuncios",
                                  params={"status": "eq.active", "sku": f"in.({lista})",
                                          "select": "ml_id,conta,sku,titulo"},
                                  headers=_sb_headers(), timeout=30)
                if rm.status_code == 200:
                    risco_ml += rm.json()
            except Exception:
                pass
            try:  # Shopee (tabela pode não existir ainda)
                rs = requests.get(f"{SB_URL}/rest/v1/shopee_anuncios",
                                  params={"status": "neq.UNLIST", "sku": f"in.({lista})",
                                          "select": "shop_id,item_id,sku,titulo"},
                                  headers=_sb_headers(), timeout=30)
                if rs.status_code == 200:
                    risco_shopee += rs.json()
            except Exception:
                pass

        pausados = 0
        if modo == "armado":
            for a in risco_ml:
                ok, _ = pausar_anuncio_ml(a.get("ml_id"), a.get("conta"))
                pausados += 1 if ok else 0
            for a in risco_shopee:
                ok, _ = pausar_anuncio_shopee(a.get("shop_id"), a.get("item_id"))
                pausados += 1 if ok else 0

        skus_risco = sorted({a.get("sku") for a in (risco_ml + risco_shopee)})
        rr = jsonify({"ok": True, "modo": modo,
                      "skus_zerados": len(skus),
                      "anuncios_em_risco": len(risco_ml) + len(risco_shopee),
                      "ml": len(risco_ml), "shopee": len(risco_shopee),
                      "skus_em_risco": len(skus_risco),
                      "pausados": pausados,
                      "amostra": [{"sku": a.get("sku"), "ml_id": a.get("ml_id"), "conta": a.get("conta"),
                                   "titulo": (a.get("titulo") or "")[:50]} for a in risco_ml[:20]]})
        rr.headers["Access-Control-Allow-Origin"] = "*"
        return rr

    @bp.route("/integracoes/processar-vendas-novas", methods=["POST", "GET", "OPTIONS"])
    def processar_vendas_novas():
        """B4 — GATILHO: busca vendas recentes do ML, e p/ cada venda NOVA (paga, com
        SKU, ainda não processada) dispara a cascata anti-venda-dupla.
        ?modo=observacao (padrão, só planeja+loga) | armado (pausa de verdade).
        Idempotência por order_id (arquivo no volume)."""
        if request.method == "OPTIONS":
            return _cors()
        modo = request.args.get("modo", "observacao").strip()
        forcar = request.args.get("forcar") == "1"  # ignora idempotência e NÃO marca (só p/ ver o estado)
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
                if not oid or v.get("status") != "paid" or (oid in processadas and not forcar):
                    continue
                for it in (v.get("itens") or []):
                    sku = str(it.get("sku") or "").strip()
                    if not sku:
                        continue
                    atual = _estoque_atual(sku)
                    if atual is None:
                        continue  # SKU fora de `pecas` (isca/avulso) → ignora
                    # NÃO baixa estoque: o PartsHub já baixa na venda (sincronizado c/ ML).
                    # Só pausa os OUTROS anúncios se a peça JÁ esgotou (qtd<=0 no PartsHub).
                    if modo == "armado":
                        pausados = 0
                        if atual <= 0:
                            rel = executar_sincronizacao(sku, "ml")
                            pausados = rel.get("pausados", 0)
                        resultados.append({"order_id": oid, "conta": conta, "sku": sku,
                                           "estoque": atual, "pausados": pausados})
                    else:
                        pausaria = planejar_pausa(sku, "ml")["total"] if atual <= 0 else 0
                        resultados.append({"order_id": oid, "conta": conta, "sku": sku,
                                           "estoque": atual, "pausaria": pausaria})
                novas.append(oid)
                if not forcar:
                    processadas.add(oid)
        if not forcar:
            _salvar_processadas(processadas)
        r = jsonify({"ok": True, "modo": modo, "vendas_novas": len(novas), "diag": diag, "resultados": resultados})
        r.headers["Access-Control-Allow-Origin"] = "*"
        return r

    @bp.route("/integracoes/processar-cancelamentos", methods=["POST", "GET", "OPTIONS"])
    def processar_cancelamentos():
        """B5 — CANCELAMENTO: venda cancelada no ML → devolve a peça ao estoque e
        reativa os anúncios que tinham sido pausados. Idempotente por order_id.
        ?modo=observacao (padrão, só lista) | armado (executa)."""
        if request.method == "OPTIONS":
            return _cors()
        modo = request.args.get("modo", "observacao").strip()
        forcar = request.args.get("forcar") == "1"  # ignora idempotência e NÃO marca
        proc = _carregar_canc()
        novos, resultados = [], []
        for conta in CONTAS_ML:
            try:
                rv = requests.get(f"{SELF_BASE}/integracoes/mercadolivre/vendas-recentes",
                                  params={"conta": conta, "dias": 1}, timeout=90)
                vendas = rv.json().get("vendas", []) if rv.status_code == 200 else []
            except Exception as e:
                resultados.append({"conta": conta, "erro": str(e)}); continue
            for v in vendas:
                oid = str(v.get("order_id") or "")
                if not oid or v.get("status") != "cancelled" or (oid in proc and not forcar):
                    continue
                for it in (v.get("itens") or []):
                    sku = str(it.get("sku") or "").strip()
                    if not sku:
                        continue
                    paused = _anuncios_paused_do_sku(sku)
                    atual = _estoque_atual(sku)
                    # NÃO devolve estoque: o PartsHub já devolve no cancelamento (sincronizado).
                    # Só reativa os anúncios pausados SE a peça realmente voltou (qtd>0).
                    tem_estoque = atual is not None and atual > 0
                    if modo == "armado":
                        reativados = []
                        if tem_estoque:
                            reativados = [a["ml_id"] for a in paused
                                          if reativar_anuncio_ml(a["ml_id"], a["conta"])[0]]
                        resultados.append({"order_id": oid, "conta": conta, "sku": sku,
                                           "estoque": atual, "reativados": len(reativados)})
                    else:
                        resultados.append({"order_id": oid, "conta": conta, "sku": sku,
                                           "estoque": atual, "reativaria": len(paused) if tem_estoque else 0})
                novos.append(oid)
                if not forcar:
                    proc.add(oid)
        if not forcar:
            _salvar_canc(proc)
        r = jsonify({"ok": True, "modo": modo, "cancelamentos_novos": len(novos), "resultados": resultados})
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
