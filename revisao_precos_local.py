# -*- coding: utf-8 -*-
"""
REVISÃO DE PREÇOS — raspagem LOCAL (roda no PC, de graça).

Por que local: o Mercado Livre bloqueia (403) busca de servidor/datacenter.
No seu PC o IP é residencial, então o ML responde normal. Usa o navegador
(Playwright) que já está instalado.

O que faz:
  1) pega seus anúncios ATIVOS com estoque (mais antigos primeiro);
  2) abre o ML e raspa os preços da concorrência (3 páginas);
  3) manda pro sistema (/revisao-precos/salvar-item), que aplica os filtros
     (produto certo pela palavra-cabeça, tira atacado, corta outlier) e grava.

NÃO altera preço de nada. Só preenche a tela de Revisão de Preços pra você
aprovar/editar/ignorar.

Como usar (no PowerShell, dentro da pasta wrx-api):
    python revisao_precos_local.py            # revisa 10 anúncios
    python revisao_precos_local.py 30         # revisa 30
"""
import sys
import time
import re
import requests

API = "https://wrx-api-production.up.railway.app"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# JS que raspa os cards da página de lista do ML (título, preço, vendedor), sem repetir.
JS_RASPAR = r"""
() => {
  const cards = document.querySelectorAll('li.ui-search-layout__item, .poly-card, div.ui-search-result');
  const seen = new Set(); const pares = [];
  cards.forEach(c => {
    const t = c.querySelector('.poly-component__title, .ui-search-item__title');
    const fr = c.querySelector('.andes-money-amount__fraction');
    const cents = c.querySelector('.andes-money-amount__cents');
    const seller = c.querySelector('.poly-component__seller');
    if(!t || !fr) return;
    let p = parseFloat(fr.textContent.replace(/\./g,'').replace(',','.'));
    if(cents) p += parseFloat(cents.textContent.replace(',','.'))/100;
    const titulo = t.textContent.trim();
    const key = titulo + '|' + p;
    if(seen.has(key)) return; seen.add(key);
    pares.push({titulo, preco: Math.round(p*100)/100, vendedor: seller ? seller.textContent.trim() : ''});
  });
  return pares;
}
"""


def query_do_titulo(titulo):
    """Monta um termo de busca limpo a partir do título (6 primeiras palavras úteis)."""
    t = (titulo or "").lower()
    t = re.sub(r"[^a-z0-9çãõáéíóúâêôà ]", " ", t)
    stop = {"do", "da", "de", "para", "com", "sem", "e", "p", "original", "novo", "nova",
            "usado", "usada", "a", "o", "os", "as"}
    palavras = [w for w in t.split() if len(w) >= 2 and w not in stop]
    return " ".join(palavras[:6])


def carregar_anuncios(limite):
    print("Buscando seus anúncios ativos...")
    r = requests.get(f"{API}/integracoes/mercadolivre/anuncios-db", timeout=60)
    por_sku = (r.json() or {}).get("anunciosPorSku", {})
    # já está na lista (qualquer status)? pula — pra re-checar é só "Limpar lista"
    try:
        jr = requests.get(f"{API}/revisao-precos/listar?status=todos", timeout=30)
        ja = {(x.get("sku") or "").upper() for x in (jr.json().get("itens") or [])}
    except Exception:
        ja = set()
    itens = []
    vistos = set()
    for sku, anuncios in por_sku.items():
        for a in anuncios:
            if (a.get("status") or "") != "active":
                continue
            if (a.get("estoque") or 0) <= 0:
                continue
            s = (sku or "").upper()
            if s in vistos or s in ja:
                continue
            vistos.add(s)
            itens.append({
                "sku": sku,
                "ml_id": a.get("mlId") or a.get("externalListingId") or "",
                "conta": a.get("integrationId") or "default",
                "titulo": a.get("titulo") or "",
                "thumbnail": a.get("thumbnail") or "",
                "meu_preco": float(a.get("preco") or 0),
            })
            break
    # mais antigos primeiro (SKU sequencial)
    itens.sort(key=lambda x: str(x["sku"]))
    return itens[:limite]


def _eh_captcha(pagina):
    try:
        u = (pagina.url or "").lower()
        t = (pagina.title() or "").lower()
        return ("captcha" in u) or ("seguridad" in t) or ("security" in t)
    except Exception:
        return False


def _reaquecer(pagina):
    """Volta na home do ML pra renovar cookies (reduz captcha)."""
    try:
        pagina.goto("https://www.mercadolivre.com.br/", timeout=20000, wait_until="domcontentloaded")
        pagina.wait_for_timeout(1500)
    except Exception:
        pass


def _raspar_produto(pagina, slug):
    """Raspa as 3 páginas de um produto. Trata captcha (espera + reaquece + 1 retry).
    Retorna (pares, bloqueado)."""
    pares = []
    bloqueado = False
    for pag in ("", "_Desde_51", "_Desde_101"):
        url = f"https://lista.mercadolivre.com.br/{slug}{pag}"
        for tentativa in (1, 2):
            try:
                pagina.goto(url, timeout=30000, wait_until="domcontentloaded")
                if _eh_captcha(pagina):
                    if tentativa == 1:
                        print("   (ML pediu captcha — esperando e tentando de novo...)")
                        pagina.wait_for_timeout(6000)
                        _reaquecer(pagina)
                        continue  # tenta a mesma página de novo
                    bloqueado = True
                    break
                try:
                    pagina.wait_for_selector(
                        "li.ui-search-layout__item, .ui-search-result__wrapper, .poly-card",
                        timeout=12000)
                except Exception:
                    pass
                pagina.wait_for_timeout(1000)
                pares.extend(pagina.evaluate(JS_RASPAR) or [])
                break
            except Exception as e:
                print(f"   (página falhou: {e})")
                break
    return pares, bloqueado


def main():
    import random
    arg = sys.argv[1] if len(sys.argv) > 1 else "10"
    limite = 99999 if str(arg).strip().lower() in ("tudo", "todos", "all") else int(arg)
    itens = carregar_anuncios(limite)
    if not itens:
        print("Nenhum anúncio ativo novo pra revisar (talvez já estejam todos na lista).")
        return
    print(f"Vou revisar {len(itens)} anúncios.\n")

    from playwright.sync_api import sync_playwright
    com_preco = sem_preco = bloqueados = 0
    with sync_playwright() as pw:
        # Receita anti-bloqueio do local_compat_server (_raspar_ml), que já funciona:
        # flag AutomationControlled off + esconde navigator.webdriver + home p/ cookies.
        navegador = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        ctx = navegador.new_context(
            locale="pt-BR", user_agent=UA,
            extra_http_headers={"Accept-Language": "pt-BR,pt;q=0.9"},
        )
        pagina = ctx.new_page()
        pagina.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        _reaquecer(pagina)
        for i, it in enumerate(itens, 1):
            termo = query_do_titulo(it["titulo"]) or it["titulo"]
            slug = re.sub(r"\s+", "-", termo.strip())
            pares, bloqueado = _raspar_produto(pagina, slug)
            # dedup global
            vistos, limpos = set(), []
            for p in pares:
                k = f"{p.get('titulo')}|{p.get('preco')}"
                if k in vistos:
                    continue
                vistos.add(k)
                limpos.append(p)
            body = {
                "sku": it["sku"], "ml_id": it["ml_id"], "conta": it["conta"],
                "titulo": it["titulo"], "thumbnail": it["thumbnail"],
                "oem": "", "meu_preco": it["meu_preco"], "pares": limpos,
            }
            try:
                rr = requests.post(f"{API}/revisao-precos/salvar-item", json=body, timeout=30)
                resp = rr.json() or {}
                if resp.get("pulado"):
                    print(f"[{i}/{len(itens)}] {it['titulo'][:42]:42} | PULADO ({resp.get('motivo')})")
                else:
                    linha = resp.get("linha", {})
                    q = linha.get('fonte_qtd', 0)
                    com_preco += 1 if q else 0
                    sem_preco += 0 if q else 1
                    print(f"[{i}/{len(itens)}] {it['titulo'][:42]:42} | "
                          f"meu R${it['meu_preco']:.0f}  menor R${linha.get('menor_mercado',0):.0f}  "
                          f"média R${linha.get('media_mercado',0):.0f}  sug R${linha.get('sugestao',0):.0f}  "
                          f"({q} preços){' [BLOQUEADO]' if bloqueado else ''} -> {linha.get('prioridade','')}")
            except Exception as e:
                print(f"[{i}/{len(itens)}] ERRO ao salvar {it['sku']}: {e}")
            if bloqueado:
                bloqueados += 1
            # pausa variável (não força o ML) + reaquece a cada 10
            time.sleep(1.2 + random.random() * 1.3)
            if i % 10 == 0:
                _reaquecer(pagina)
        navegador.close()
    print(f"\nPronto! {com_preco} com preço, {sem_preco} sem preço"
          + (f", {bloqueados} bloqueados pelo ML" if bloqueados else "")
          + ". Abra a tela REVISÃO DE PREÇOS no painel.")


if __name__ == "__main__":
    main()
