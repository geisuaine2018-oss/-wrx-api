"""
local_compat_server.py
Servidor local (Windows) para buscar compatibilidade OEM no Mercado Livre via Playwright.
Roda em localhost:5001. Execute: python local_compat_server.py

Fluxo:
  Frontend → Railway (checa Supabase) → sem dados → chama localhost:5001
  localhost:5001 → Playwright → ML → IA extrai compatibilidade → salva Supabase → retorna
"""

import json, re, os, time, traceback
import requests
from flask import Flask, request, jsonify, Response
from playwright.sync_api import sync_playwright

app = Flask(__name__)

# ─── Configuração ─────────────────────────────────────────────────────────────

# CRM Supabase — tabela oem_compatibilidades com RLS pública (sem auth necessária)
_CRM_HOST = "uthsiihzpsgarargegcw.supabase.co"
_CRM_ANON = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InV0aHNpaWh6cHNnYXJhcmdlZ2N3Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzkxMTAyMzcsImV4cCI6MjA5NDY4NjIzN30.vLsk56k7VUROClBmyg4NJFXHtpTGmr1f0Xl_dARbtZE"

# Keys de IA: env var tem prioridade (Railway); fallback para config.json local
def _carregar_keys():
    cfg = {}
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")) as f:
            cfg = json.load(f)
    except Exception:
        pass
    gemini = os.environ.get("GEMINI_API_KEY") or cfg.get("gemini_key", "")
    claude = os.environ.get("ANTHROPIC_API_KEY") or cfg.get("api_key", "")
    return gemini, claude

_GEMINI_KEY, _CLAUDE_KEY = _carregar_keys()
print(f"[CONFIG] Claude key: {'OK' if _CLAUDE_KEY else 'AUSENTE'} | Gemini key: {'OK' if _GEMINI_KEY else 'AUSENTE'}")


def _crm_headers():
    return {
        "apikey": _CRM_ANON,
        "Authorization": f"Bearer {_CRM_ANON}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }


# ─── Playwright: raspar ML ────────────────────────────────────────────────────

def _raspar_ml(oem: str) -> list:
    """Usa Playwright (Windows local) para raspar títulos do ML."""
    oem_enc = requests.utils.quote(oem)
    # PASSO 1: busca como humano — OEM puro, sem forçar categoria
    url = f"https://lista.mercadolivre.com.br/{oem_enc}"
    print(f"[OEM BUSCA] OEM={oem} | URL={url}")

    anuncios = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"]
        )
        ctx = browser.new_context(
            locale="pt-BR",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            extra_http_headers={"Accept-Language": "pt-BR,pt;q=0.9"}
        )
        page = ctx.new_page()
        page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")

        # Abre ML primeiro para cookies
        page.goto("https://www.mercadolivre.com.br/", timeout=15000, wait_until="domcontentloaded")
        page.wait_for_timeout(800)

        resp = page.goto(url, timeout=30000, wait_until="domcontentloaded")
        print(f"[ML] Status HTTP: {resp.status if resp else 'N/A'}")
        try:
            page.wait_for_selector(
                "li.ui-search-layout__item, .ui-search-result__wrapper, .poly-card",
                timeout=12000
            )
        except Exception:
            pass
        page.wait_for_timeout(2000)
        html = page.content()
        print(f"[ML] HTML recebido: {len(html)} caracteres")

        anuncios = page.evaluate("""() => {
            const res = [];
            const items = document.querySelectorAll(
                'li.ui-search-layout__item, .ui-search-result__wrapper, .poly-card'
            );
            for (let i = 0; i < Math.min(items.length, 30); i++) {
                const t = items[i].querySelector(
                    '.ui-search-item__title, .poly-component__title, h2'
                );
                const a = items[i].querySelector('a[href*="mercadolivre"]');
                const p = items[i].querySelector('.andes-money-amount__fraction');
                const titulo = t ? t.textContent.trim() : '';
                if (titulo.length > 8)
                    res.push({
                        titulo,
                        link: a ? a.href.split('?')[0] : '',
                        preco: p ? p.textContent.trim() : ''
                    });
            }
            return res;
        }""")

        browser.close()

    print(f"[ML] Anúncios encontrados: {len(anuncios)}")
    for i, a in enumerate(anuncios[:30], 1):
        print(f"[TITULO {i}] {a['titulo']}")
    return anuncios


# ─── Preço de mercado (peça NOVA) ────────────────────────────────────────────
# O preço vem dos anúncios já raspados na busca de compatibilidade (1 navegador),
# não de uma busca separada. O ML não separa novo/usado de forma confiável numa
# busca por OEM, então focamos no preço de peça NOVA filtrando os usados pelo título.

# Termos que indicam PARTE/ACESSÓRIO da peça (vêm muito mais baratos e contaminam
# o preço): mangueira da turbina, kit de reparo, junta, etc. Buscar o OEM da turbina
# traz esses junto — o preço deles não pode entrar no cálculo da turbina.
_TERMOS_PARTE = [
    "mangueira", "kit", "reparo", "junta", "anel", "anéis", "parafuso", "sensor",
    "atuador", "abraçadeira", "abracadeira", "tubo", "duto", "retentor", "vareta",
    "jogo", "par de", "capa", "protetor", "adesivo", "emblema", "suporte",
    "carcaça", "carcaca", "valvula", "válvula", "bucha", "rolamento", "presilha",
    "conector", "borracha", "espaçador", "espacador", "gaxeta", "vedação", "vedacao",
]

# Termos que indicam peça USADA/recondicionada. O preço-alvo é de peça NOVA, então
# esses anúncios saem do cálculo (turbina usada custa bem menos que nova).
_TERMOS_USADO = [
    "usado", "usada", "recondicionad", "recondicionada", "retirad", "retirada",
    "revisad", "revisada", "semi nova", "semi-nova", "seminova", "remanufaturad",
    "original retirada", "boa", "funcionando",
]


def _filtrar_precos_da_peca(itens: list, nome_peca: str) -> list:
    """Mantém só os preços dos anúncios que são a peça-alvo NOVA.

    Descarta: (1) PARTES/acessórios (mangueira, kit, reparo...) que vêm baratos e
    puxam a mediana pra baixo; (2) peças USADAS/recondicionadas (o preço-alvo é o de
    NOVA). Também exige que o título cite o núcleo do nome da peça (ex 'turbina').
    Ex: ao buscar o OEM de uma turbina, o ML lista 'Mangueira Turbina' (R$130) e
    'Turbina Usada' (R$800) — ambos saem; sobra a turbina nova.
    Se o filtro deixar poucos dados (<3), devolve tudo — melhor dado ruidoso (avisado)
    que nenhum dado.
    """
    if not itens:
        return []
    nucleo = ""
    if nome_peca:
        # primeira palavra significativa do nome (ex "Caixa Filtro Ar" -> "caixa")
        m = re.search(r'[a-záàâãéêíóôõúç]{3,}', nome_peca.lower())
        nucleo = m.group(0) if m else ""
    bons, fora_parte, fora_usado = [], [], []
    for it in itens:
        t = (it.get("titulo") or "").lower()
        preco = it.get("preco")
        if not preco:
            continue
        tem_parte = any(termo in t for termo in _TERMOS_PARTE)
        tem_usado = any(termo in t for termo in _TERMOS_USADO)
        tem_nucleo = (nucleo in t) if nucleo else True
        if tem_parte or not tem_nucleo:
            fora_parte.append((round(preco), it.get("titulo", "")[:50]))
        elif tem_usado:
            fora_usado.append((round(preco), it.get("titulo", "")[:50]))
        else:
            bons.append(preco)
    if fora_parte:
        print(f"[PREÇO] {len(fora_parte)} descartados (parte/acessório, não é '{nome_peca}'): "
              + "; ".join(f"R${p} {t}" for p, t in fora_parte[:5]) + ("..." if len(fora_parte) > 5 else ""))
    if fora_usado:
        print(f"[PREÇO] {len(fora_usado)} descartados (usado/recondicionado — queremos NOVO): "
              + "; ".join(f"R${p} {t}" for p, t in fora_usado[:5]) + ("..." if len(fora_usado) > 5 else ""))
    if len(bons) < 3:
        # filtro agressivo demais p/ esta peça — usa todos menos as partes (mantém o foco em peça inteira)
        sobra = [it.get("preco") for it in itens
                 if it.get("preco") and not any(termo in (it.get("titulo") or "").lower() for termo in _TERMOS_PARTE)]
        print(f"[PREÇO] filtro de novo deixou só {len(bons)} — usando {len(sobra)} (novo+usado, sem partes)")
        return sobra or [it.get("preco") for it in itens if it.get("preco")]
    return bons


def _faixa_preco(precos: list) -> dict:
    """Calcula faixa de mercado descartando outliers (IQR) e sugere ~3% abaixo da mediana.
    Avisa quando a dispersão é alta (peças misturadas)."""
    p = sorted(float(x) for x in precos if x)
    if not p:
        return {"qtd": 0, "min": None, "max": None, "mediana": None,
                "sugerido": None, "descartados": [], "ruidoso": False}
    if len(p) < 4:
        # poucos dados: sem corte de outlier
        mediana = p[len(p)//2]
        return {"qtd": len(p), "min": round(p[0]), "max": round(p[-1]),
                "mediana": round(mediana), "sugerido": round(mediana * 0.97),
                "descartados": [], "ruidoso": False}
    n = len(p)
    def _q(frac):
        i = frac * (n - 1); lo = int(i); hi = min(lo + 1, n - 1)
        return p[lo] + (p[hi] - p[lo]) * (i - lo)
    q1, q3 = _q(0.25), _q(0.75)
    iqr = q3 - q1
    lim_lo, lim_hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    bons = [x for x in p if lim_lo <= x <= lim_hi]
    descartados = [round(x) for x in p if x < lim_lo or x > lim_hi]
    if not bons:
        bons = p
    bons.sort()
    mediana = bons[len(bons)//2]
    # dispersão alta: faixa boa > 2x a mediana → peças provavelmente misturadas
    ruidoso = (bons[-1] - bons[0]) > mediana
    sugerido = round(mediana * 0.97)
    if descartados:
        print(f"[PREÇO] descartados (outlier): {descartados}")
    print(f"[PREÇO] faixa {round(bons[0])}-{round(bons[-1])} | mediana {round(mediana)} "
          f"| sugerido {sugerido}" + (" | RUIDOSO" if ruidoso else ""))
    return {"qtd": len(bons), "min": round(bons[0]), "max": round(bons[-1]),
            "mediana": round(mediana), "sugerido": sugerido,
            "descartados": descartados, "ruidoso": ruidoso}


def _nome_peca_do_titulo(anuncios: list, oem: str) -> str:
    """Deduz o nome da peça (ex: 'Caixa Filtro Ar', 'Cabeçote') do começo dos títulos.
    Pega as palavras antes da primeira marca de carro / motor / OEM."""
    if not anuncios:
        return ""
    marcas_carro = re.compile(
        r'\b(' + '|'.join(['fiat','jeep','renault','peugeot','citroen','toyota','honda',
        'hyundai','kia','nissan','mitsubishi','volkswagen','vw','chevrolet','gm','ford',
        'ram','dodge','bmw','mercedes','audi','volvo','suzuki','chery','byd','jac',
        'commander','compass','renegade','toro','pulse','fastback','strada','kicks',
        'onix','tracker','corolla','civic','gol','argo','cronos']) + r')\b', re.IGNORECASE)
    from collections import Counter
    nomes = Counter()
    oem_norm = re.sub(r'[^0-9a-z]', '', oem.lower())
    for a in anuncios:
        t = a.get("titulo", "")
        # corta no primeiro: marca de carro, número longo (OEM), ou motor (1.0, 1.3)
        m_marca = marcas_carro.search(t)
        m_motor = re.search(r'\b\d\.\d\b', t)
        m_oem = re.search(r'\b\d{6,}\b', t)
        cortes = [x.start() for x in [m_marca, m_motor, m_oem] if x]
        nome = (t[:min(cortes)] if cortes else t).strip()
        # limpa palavras de ruído comuns
        nome = re.sub(r'\b(original|orig|nova?|usado?|completo|recondicionado|de|do|da)\b', '', nome, flags=re.IGNORECASE)
        nome = re.sub(r'\s+', ' ', nome).strip(' -/')
        if 3 <= len(nome) <= 40:
            nomes[nome.title()] += 1
    if not nomes:
        return ""
    nome = nomes.most_common(1)[0][0]
    print(f"[NOME PEÇA] '{nome}' (de {len(anuncios)} títulos)")
    return nome


# ─── IA: extrair compatibilidade dos títulos ─────────────────────────────────

_MARCAS_RE = [
    r"citroen|citro[eë]n", r"renault", r"peugeot", r"toyota", r"honda",
    r"hyundai", r"kia", r"nissan", r"mitsubishi", r"fiat", r"volkswagen|vw\b",
    r"chevrolet|gm\b", r"ford", r"jeep", r"ram\b", r"dodge",
    r"bmw", r"mercedes|mb\b", r"audi", r"volvo", r"suzuki",
    r"chery", r"byd", r"jac", r"caoa\s*chery"
]

_MARCAS_NORM = {
    "citroen": "Citroen", "citroën": "Citroen",
    "renault": "Renault", "peugeot": "Peugeot",
    "toyota": "Toyota", "honda": "Honda",
    "hyundai": "Hyundai", "kia": "Kia",
    "nissan": "Nissan", "mitsubishi": "Mitsubishi",
    "fiat": "Fiat", "volkswagen": "VW", "vw": "VW",
    "chevrolet": "Chevrolet", "gm": "Chevrolet",
    "ford": "Ford", "jeep": "Jeep", "ram": "Ram", "dodge": "Dodge",
    "bmw": "BMW", "mercedes": "Mercedes", "mb": "Mercedes",
    "audi": "Audi", "volvo": "Volvo", "suzuki": "Suzuki",
    "chery": "Chery", "byd": "BYD", "jac": "JAC", "caoa chery": "Chery"
}

def _extrair_regex_titulos(anuncios: list) -> list:
    """Extração por regex dos títulos — não depende de IA."""
    resultado = []
    for a in anuncios:
        titulo = a.get("titulo", "")
        titulo_lower = titulo.lower()

        # Detecta marca
        marca_raw = None
        for pat in _MARCAS_RE:
            m = re.search(pat, titulo_lower, re.IGNORECASE)
            if m:
                marca_raw = m.group(0).lower().strip()
                break
        if not marca_raw:
            continue

        marca = _MARCAS_NORM.get(marca_raw, marca_raw.title())

        # Extrai modelo: palavra(s) maiúscula(s) após a marca
        modelo = None
        m_modelo = re.search(
            r'\b' + re.escape(marca_raw) + r'\b\s+([A-Z][A-Za-z0-9\-]+(?:\s+[A-Z][A-Za-z0-9\-]+)?)',
            titulo, re.IGNORECASE
        )
        if m_modelo:
            modelo = m_modelo.group(1).strip()

        # Extrai anos — formatos: "2022 A 2025", "2022-2025", "2022/2025", "2022 2025"
        anos = re.findall(r'\b(20\d{2})\b', titulo)
        ano_inicial = int(min(anos)) if anos else None
        ano_final = int(max(anos)) if anos else None

        # Extrai motor: 1.0, 1.6T, 2.0 etc.
        m_motor = re.search(r'\b(\d+[.,]\d\s*(?:T|Turbo)?)\b', titulo, re.IGNORECASE)
        motor = m_motor.group(1).replace(",", ".").strip() if m_motor else None

        resultado.append({
            "titulo_original": titulo,
            "marca": marca,
            "modelo": modelo,
            "motor": motor,
            "cambio": None,
            "ano_inicial": ano_inicial,
            "ano_final": ano_final,
        })

    print(f"[REGEX] {len(resultado)} compatibilidades extraídas de {len(anuncios)} títulos")
    return resultado


def _extrair_compat_ia(oem: str, anuncios: list) -> list:
    """Usa IA para extrair marca/modelo/motor/anos dos títulos — retorna lista de compatibilidades."""
    if not anuncios:
        return []

    # Deduplica títulos idênticos: o ML lista o mesmo produto várias vezes.
    # 30 anúncios costumam ser ~10 títulos únicos → 1 chamada de IA em vez de 3,
    # economiza cota e o score passa a contar anúncios DISTINTOS citando o veículo.
    vistos, anuncios_uni = set(), []
    for a in anuncios:
        chave = re.sub(r'\s+', ' ', a.get("titulo", "").strip().lower())
        if chave and chave not in vistos:
            vistos.add(chave)
            anuncios_uni.append(a)
    anuncios = anuncios_uni
    print(f"[IA] _extrair_compat_ia: {len(anuncios)} títulos únicos")

    def _build_prompt(lote, offset):
        titulos_txt = "\n".join([f"{offset+i+1}. {a['titulo']}" for i, a in enumerate(lote)])
        return f"""Esses são títulos de anúncios do Mercado Livre Brasil encontrados pesquisando pelo código de peça automotiva (OEM): {oem}

Títulos:
{titulos_txt}

REGRA IMPORTANTE: um mesmo título pode citar VÁRIOS veículos compatíveis, de marcas diferentes.
Exemplo: "Caixa Filtro Ar Compass Renegade Toro 1.3 T270" cita 3 veículos:
  - Jeep Compass, Jeep Renegade (Compass e Renegade são Jeep)
  - Fiat Toro (Toro é Fiat)
Você DEVE separar cada veículo em um item próprio, atribuindo a MARCA CORRETA a cada modelo.

Para cada veículo extraia:
- marca: a marca real do modelo (Jeep, Fiat, Renault, Nissan, Toyota, Honda, VW, Ford, Chevrolet, Hyundai, Kia, etc.). Use seu conhecimento: Compass/Renegade/Commander=Jeep, Toro/Pulse/Fastback/Strada/Argo/Cronos=Fiat, etc.
- modelo: UM único modelo (Compass, Renegade, Toro, Corolla...). Nunca junte dois modelos.
- motor: cilindrada normalizada, ex "1.3 Turbo", "1.0", "2.0". O código "T270" é o motor 1.3 Turbo da Fiat/Jeep → use "1.3 Turbo". Ignore potência (185cv) e combustível (Flex) no campo motor.
- cambio: Manual ou Automatico se mencionado, senão null
- ano_inicial / ano_final: anos de 4 dígitos. "21/25" significa 2021 a 2025. "2023 24" significa 2023 a 2024. Um ano só → use o mesmo nos dois campos.

NÃO invente. Se um título não tem nenhum modelo de veículo identificável, omita-o.
RETORNE APENAS um JSON array, um objeto por VEÍCULO (não por título):
[{{"marca":"Jeep","modelo":"Compass","motor":"1.3 Turbo","cambio":null,"ano_inicial":2021,"ano_final":2025}},{{"marca":"Fiat","modelo":"Toro","motor":"1.3 Turbo","cambio":null,"ano_inicial":2021,"ano_final":2025}}]"""

    def _gemini(p):
        for modelo in ["gemini-2.5-flash", "gemini-2.0-flash"]:
            try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent?key={_GEMINI_KEY}"
                r = requests.post(url, json={
                    "contents": [{"parts": [{"text": p}]}],
                    "generationConfig": {"maxOutputTokens": 8192, "temperature": 0.0}
                }, timeout=30)
                print(f"[IA] Gemini {modelo} status: {r.status_code}")
                if r.status_code == 200:
                    print("[IA] JSON COMPLETO GEMINI:")
                    print(r.text[:3000])
                    print(f"[IA] Finish reason: {r.json()['candidates'][0].get('finishReason')}")
                    parts = r.json()["candidates"][0]["content"]["parts"]
                    text = "".join(x["text"] for x in parts if "text" in x)

                    print(f"[IA] TEXTO GEMINI: {text[:1000]}")

                    m = re.search(r'\[.*\]', text, re.DOTALL)
                    if m:
                        return json.loads(m.group(0))
                else:
                    print(f"[IA] Gemini {modelo} resposta: {r.text[:200]}")
            except Exception as e:
                print(f"[IA] Gemini {modelo} erro: {e}")
                traceback.print_exc()

    def _claude(p):
        print("[CLAUDE] Entrou na função")
        if not _CLAUDE_KEY:
            print("[CLAUDE] motivo retorno None: _CLAUDE_KEY vazia")
            return None
        try:
            print("[CLAUDE] Enviando requisição")
            r = requests.post("https://api.anthropic.com/v1/messages",
                headers={"x-api-key": _CLAUDE_KEY, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": "claude-haiku-4-5-20251001", "max_tokens": 2000,
                      "messages": [{"role": "user", "content": p}]}, timeout=30)
            print(f"[CLAUDE] status: {r.status_code}")
            print(f"[CLAUDE] resposta: {r.text[:1000]}")
            print("[CLAUDE] JSON recebido")
            text = r.json()["content"][0]["text"].strip()
            m = re.search(r'\[.*\]', text, re.DOTALL)
            if not m:
                print("[CLAUDE] motivo retorno None: nenhum array JSON encontrado no texto")
                return None
            return json.loads(m.group(0))
        except Exception as e:
            print(f"[CLAUDE] motivo retorno None: exceção — {e}")
            print(f"[IA] Claude erro: {e}")
            traceback.print_exc()
            return None

    LOTE = 10
    ai_results = []
    for start in range(0, len(anuncios), LOTE):
        lote = anuncios[start:start + LOTE]
        lote_num = start // LOTE + 1
        print(f"[IA] Lote {lote_num}: anúncios {start+1}–{start+len(lote)}")
        prompt = _build_prompt(lote, start)
        print("[IA] Tentando Claude primeiro...")
        resultado = _claude(prompt)
        print(f"[IA] Claude retornou: {type(resultado)} len={len(resultado) if resultado else 0}")
        if not resultado:
            print("[IA] Claude falhou, tentando Gemini...")
            resultado = _gemini(prompt)
        print(f"[IA] Resultado final lote {lote_num}: {type(resultado)} len={len(resultado) if resultado else 0}")
        if resultado:
            ai_results.extend(resultado)

    print(f"[IA] Total acumulado após lotes: {len(ai_results)} veículos brutos")

    if not ai_results:
        print("[IA] Sem resultado da IA — usando regex como fallback")
        return _extrair_regex_titulos(anuncios)

    # A IA agora retorna uma lista plana de veículos (1 por modelo, marca já correta).
    compatibilidades = []
    for ai in ai_results:
        if not isinstance(ai, dict) or not ai.get("marca") or not ai.get("modelo"):
            continue
        compatibilidades.append({
            "marca": str(ai.get("marca") or "").strip(),
            "modelo": str(ai.get("modelo") or "").strip(),
            "motor": str(ai.get("motor") or "").strip(),
            "cambio": str(ai.get("cambio") or "").strip(),
            "ano_inicial": ai.get("ano_inicial"),
            "ano_final": ai.get("ano_final"),
        })

    print(f"[IA] {len(compatibilidades)} veículos extraídos de {len(anuncios)} títulos")
    return compatibilidades


def _agrupar_compat(compatibilidades: list) -> list:
    """Agrupa por marca+modelo e expande range de anos."""
    grupos = {}
    for c in compatibilidades:
        chave = f"{(c.get('marca') or '').lower()}|{(c.get('modelo') or '').lower()}"
        if chave not in grupos:
            grupos[chave] = dict(c)
            grupos[chave]["count"] = 1
        else:
            g = grupos[chave]
            g["count"] += 1
            if c.get("ano_inicial") and (not g.get("ano_inicial") or c["ano_inicial"] < g["ano_inicial"]):
                g["ano_inicial"] = c["ano_inicial"]
            if c.get("ano_final") and (not g.get("ano_final") or c["ano_final"] > g["ano_final"]):
                g["ano_final"] = c["ano_final"]
            if not g.get("motor") and c.get("motor"):
                g["motor"] = c["motor"]

    return sorted(grupos.values(), key=lambda x: x["count"], reverse=True)


def _recontar_frequencia_real(grupos: list, anuncios: list) -> list:
    """Reconta quantos anúncios REAIS (não títulos únicos) citam cada modelo.

    A dedup antes da IA economiza cota mas zera a frequência (tudo vira 1).
    Aqui recontamos contra todos os anúncios: o veículo relevante ao OEM aparece
    muitas vezes; a poluição de busca (outro carro) aparece poucas. Isso alimenta
    o score e separa o que importa do lixo, mesmo sem código no título.
    """
    titulos = [a.get("titulo", "").lower() for a in anuncios]
    for g in grupos:
        modelo = (g.get("modelo") or "").lower().strip()
        if not modelo:
            continue
        # conta anúncios cujo título contém o modelo como palavra
        pat = r'\b' + re.escape(modelo) + r'\b'
        g["count"] = sum(1 for t in titulos if re.search(pat, t)) or g.get("count", 1)
    return sorted(grupos, key=lambda x: x["count"], reverse=True)


def _confianca_anos(grupos: list, anuncios: list) -> list:
    """Define os anos de cada modelo SOMENTE por evidência real nos anúncios.

    REGRA (definida pelo usuário): nunca inferir ano por nome/geração/IA.
    O ano só existe se estiver ESCRITO nos títulos. A confiança do ano vem de
    quantos anúncios concordam: 1 anúncio = baixa; vários = alta. Sem evidência
    = 'Não confirmados' (ano_inicial/ano_final = None, anos_confirmados=False).
    """
    from collections import Counter
    titulos = [a.get("titulo", "") for a in anuncios]
    for g in grupos:
        modelo = (g.get("modelo") or "").lower().strip()
        if not modelo:
            g["ano_inicial"] = g["ano_final"] = None
            g["anos_confirmados"] = False
            g["evidencia_ano"] = 0
            continue
        pat = r'\b' + re.escape(modelo) + r'\b'
        titulos_modelo = [t for t in titulos if re.search(pat, t.lower())]
        contagem_ano = Counter()
        anuncios_com_ano = 0
        for t in titulos_modelo:
            anos = {int(x) for x in re.findall(r'\b(20\d{2})\b', t) if 2000 <= int(x) <= 2035}
            if anos:
                anuncios_com_ano += 1
                for ano in anos:
                    contagem_ano[ano] += 1
        if not contagem_ano:
            # nenhum anúncio do modelo trazia ano → não confirmado
            g["ano_inicial"] = g["ano_final"] = None
            g["anos_confirmados"] = False
            g["evidencia_ano"] = 0
            print(f"[ANO NÃO CONFIRMADO] {g.get('marca')} {g.get('modelo')} — nenhum anúncio com ano")
            continue
        anos_vistos = sorted(contagem_ano)
        g["ano_inicial"] = anos_vistos[0]
        g["ano_final"] = anos_vistos[-1]
        g["evidencia_ano"] = anuncios_com_ano
        # vários anúncios com ano = confirmado; 1 só = baixa confiança
        g["anos_confirmados"] = anuncios_com_ano >= 2
        sit = "confirmado" if g["anos_confirmados"] else "baixa confiança (1 anúncio)"
        print(f"[ANO] {g.get('marca')} {g.get('modelo')} -> {g['ano_inicial']}-{g['ano_final']} "
              f"({anuncios_com_ano} anúncios com ano, {sit})")
    return grupos


# ─── PASSO 5: relevância ao OEM (anti-contaminação) ──────────────────────────

def _filtrar_por_relevancia_oem(anuncios: list, oem: str) -> list:
    """Mantém só anúncios realmente relacionados ao OEM buscado.

    A busca do ML por OEM às vezes mistura anúncios de OUTRAS peças (ex: buscar
    farol do Kicks trouxe faróis de Celta/Gol antigos). Sinal de relevância:
    o título cita o OEM buscado OU outro código de peça longo (OEM-irmão).
    Anúncios sem nenhum código de peça no título são poluição de busca e saem.
    """
    oem_norm = re.sub(r'[^0-9a-z]', '', oem.lower())
    relevantes, descartados = [], []
    for a in anuncios:
        titulo = a.get("titulo", "")
        t_norm = re.sub(r'[^0-9a-z]', '', titulo.lower())
        # códigos de peça no título: sequências de 7+ dígitos/alfanuméricas
        tem_codigo = bool(re.search(r'[0-9]{7,}|[0-9]{4,}[a-z]{1,3}[0-9]{2,}', titulo.lower()))
        if oem_norm and oem_norm in t_norm:
            relevantes.append(a)
        elif tem_codigo:
            relevantes.append(a)
        else:
            descartados.append(titulo)
    print(f"[OEM CONFIRMADO] {len(relevantes)}/{len(anuncios)} anúncios com código de peça no título")
    if descartados:
        print(f"[ML] Descartados sem código (poluição de busca): {len(descartados)}")
    # Se o filtro zerou tudo (nenhum título traz código), não filtra — evita perder peça boa
    return relevantes if relevantes else anuncios


# ─── PASSO 5: rejeição rigorosa ──────────────────────────────────────────────

def _eh_none(v):
    """True se o valor é vazio, None, ou a string literal 'None'/'null'."""
    if v is None:
        return True
    s = str(v).strip().lower()
    return s in ("", "none", "null", "nan")

def _rejeitar_invalidos(grupos: list) -> tuple:
    """Separa grupos aprovados dos rejeitados. PASSO 5.
    Rejeita: marca vazia, modelo vazio, qualquer None nesses campos.
    Ano vazio NÃO rejeita (será inferido depois) — peças sem ano no título são
    comuns e descartá-las apagava compatibilidades reais.
    Retorna (aprovados, rejeitados)."""
    aprovados, rejeitados = [], []
    for g in grupos:
        motivos = []
        if _eh_none(g.get("marca")):
            motivos.append("marca vazia")
        if _eh_none(g.get("modelo")):
            motivos.append("modelo vazio")
        rotulo = f"{g.get('marca') or '?'} {g.get('modelo') or '?'} {g.get('motor') or ''}".strip()
        if motivos:
            print(f"[COMPATIBILIDADE REJEITADA] {rotulo} — {', '.join(motivos)}")
            rejeitados.append({**g, "motivo_rejeicao": ", ".join(motivos)})
        else:
            print(f"[COMPATIBILIDADE APROVADA] {rotulo}")
            aprovados.append(g)
    return aprovados, rejeitados


# ─── PASSO 4/7: score por linha ──────────────────────────────────────────────

def _calcular_scores(grupos: list) -> list:
    """Score por linha = frequência do modelo + confiança do ano (evidência real).
    Mexe nos grupos in-place adicionando 'score' e retorna ordenado por score."""
    if not grupos:
        return []
    top_count = max(g.get("count", 0) for g in grupos) or 1
    for g in grupos:
        freq_rel = g.get("count", 0) / top_count
        # confiança do ano por evidência: 0 anúncios=0, 1 anúncio=0.5, 2+=1.0
        ev = g.get("evidencia_ano", 0)
        conf_ano = 1.0 if ev >= 2 else (0.5 if ev == 1 else 0.0)
        # peso: relevância do modelo (60%) + confiança do ano (40%)
        score = round((0.60 * freq_rel + 0.40 * conf_ano) * 100)
        g["score"] = max(0, min(100, score))
        anos_txt = (f"{g.get('ano_inicial')}-{g.get('ano_final')}"
                    if g.get("anos_confirmados") or ev >= 1 else "Não confirmados")
        print(f"[SCORE] {g.get('marca')} {g.get('modelo')} {g.get('motor') or ''} "
              f"{anos_txt} = {g['score']}% (freq {g.get('count')}/{top_count}, ano_ev {ev})")
    return sorted(grupos, key=lambda x: x["score"], reverse=True)


# ─── Filtro: descarta anúncios isolados ──────────────────────────────────────

def _filtrar_anuncios_isolados(anuncios: list) -> list:
    """Filtra anúncios fora da marca dominante quando ela representa >60% do total."""
    from collections import Counter

    def _detectar_marca(titulo):
        tl = titulo.lower()
        for pat in _MARCAS_RE:
            m = re.search(pat, tl, re.IGNORECASE)
            if m:
                return _MARCAS_NORM.get(m.group(0).lower().strip(), m.group(0).lower().strip())
        return None

    print(f"[ML] Antes filtro: {len(anuncios)} anúncios")

    marcas = [_detectar_marca(a["titulo"]) for a in anuncios]
    classificados = [m for m in marcas if m]
    contagem_marcas = Counter(classificados)

    if not classificados:
        print("[ML] Nenhuma marca detectada — sem filtro aplicado")
        return anuncios

    marca_dominante, count_dominante = contagem_marcas.most_common(1)[0]
    pct_dominante = count_dominante / len(classificados)

    print(f"[ML] Marca dominante: {marca_dominante} ({count_dominante}/{len(classificados)} anúncios classificados)")
    print(f"[ML] Percentual dominante: {pct_dominante:.0%}")

    _AGRICOLA = re.compile(r'\b(trator|massey|colheitadeira|implemento)\b', re.IGNORECASE)

    if pct_dominante > 0.60:
        por_marca = [(a, m) for a, m in zip(anuncios, marcas) if m is None or m == marca_dominante]
        removidos_marca = len(anuncios) - len(por_marca)
        print(f"[ML] Removidos por marca divergente: {removidos_marca}")

        anuncios_filtrados = [a for a, _ in por_marca if not _AGRICOLA.search(a["titulo"])]
        removidos_agricola = len(por_marca) - len(anuncios_filtrados)
        print(f"[ML] Removidos por categoria agrícola: {removidos_agricola}")
    else:
        # sem marca dominante clara — mantém quem aparece >= 2 vezes
        from collections import Counter as _C
        chaves = [f"{_detectar_marca(a['titulo'])}" for a in anuncios]
        cont = _C(k for k in chaves if k != "None")
        por_freq = [a for a, k in zip(anuncios, chaves) if k == "None" or cont[k] > 1]
        print(f"[ML] Removidos por marca divergente: 0 (sem dominante, filtro por frequência)")
        anuncios_filtrados = [a for a in por_freq if not _AGRICOLA.search(a["titulo"])]
        removidos_agricola = len(por_freq) - len(anuncios_filtrados)
        print(f"[ML] Removidos por categoria agrícola: {removidos_agricola}")

    print(f"[ML] Após filtro: {len(anuncios_filtrados)} anúncios")
    return anuncios_filtrados


# ─── Supabase: salvar compatibilidades ───────────────────────────────────────

def _salvar_supabase(oem: str, grupos: list, confianca: int) -> int:
    """Salva grupo de compatibilidades no Supabase via UPSERT. Retorna quantos salvos.

    Usa on_conflict nas colunas-chave + Prefer: resolution=ignore-duplicates: a mesma
    compatibilidade (oem+marca+modelo+motor+anos) NÃO duplica a cada busca — o conflito
    é ignorado silenciosamente. Exige a constraint única no banco (ver migração em
    sql/migracao_oem_compat_unico.sql). Usa ignore (não merge) porque a chave anon do CRM
    só tem permissão de INSERT, não de UPDATE/DELETE (RLS)."""
    hdrs = {**_crm_headers(), "Prefer": "resolution=ignore-duplicates,return=minimal"}
    on_conflict = "oem,marca,modelo,motor,ano_inicial,ano_final"

    total = 0
    for g in grupos:
        marca = (g.get("marca") or "").strip()
        if not marca:
            continue
        payload = {
            "oem": oem,
            "marca": marca,
            "modelo": (g.get("modelo") or "").strip() or None,
            "motor": (g.get("motor") or "").strip() or None,
            "cambio": (g.get("cambio") or "").strip() or None,
            "ano_inicial": g.get("ano_inicial") or None,
            "ano_final": g.get("ano_final") or None,
            "fonte": "mercadolivre_playwright",
            "confianca": confianca,
            "data_validacao": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        }
        try:
            r = requests.post(
                f"https://{_CRM_HOST}/rest/v1/oem_compatibilidades",
                headers=hdrs, params={"on_conflict": on_conflict}, json=payload, timeout=10
            )
            if r.status_code in (200, 201, 204):
                total += 1
            elif "no unique" in r.text.lower() or "42P10" in r.text:
                # Constraint ainda não criada no banco: cai pro insert simples (sem dedup).
                # Quando a migração sql/migracao_oem_compat_unico.sql for aplicada, o upsert
                # acima passa a funcionar e este fallback deixa de ser usado.
                r2 = requests.post(
                    f"https://{_CRM_HOST}/rest/v1/oem_compatibilidades",
                    headers=_crm_headers(), json=payload, timeout=10
                )
                if r2.status_code in (200, 201):
                    total += 1
                else:
                    print(f"[Supabase] Erro fallback {r2.status_code}: {r2.text[:200]}")
            else:
                print(f"[Supabase] Erro {r.status_code}: {r.text[:200]}")
        except Exception as e:
            print(f"[Supabase] Exceção: {e}")

    print(f"[Supabase] {total} compatibilidades upsert para OEM {oem}")
    return total


# ─── Rota principal ───────────────────────────────────────────────────────────

def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


@app.after_request
def add_cors(resp):
    return _cors(resp)


@app.route("/buscar-compat", methods=["POST", "GET", "OPTIONS"])
def buscar_compat():
    if request.method == "OPTIONS":
        return Response("", status=204)

    data = request.get_json(force=True, silent=True) or {}
    oem = (data.get("oem") or request.args.get("oem", "")).strip().upper()
    if not oem or len(oem) < 4:
        return jsonify({"ok": False, "erro": "OEM inválido (mínimo 4 caracteres)"}), 400

    print(f"\n{'='*50}")
    print(f"[LOCAL] Buscando OEM: {oem}")
    t0 = time.time()

    try:
        # 1. Raspar ML com Playwright
        anuncios = _raspar_ml(oem)
        if not anuncios:
            return jsonify({
                "ok": True,
                "oem": oem,
                "fonte": "ml_local",
                "compatibilidades_confirmadas": [],
                "grau_de_confianca": 0,
                "mensagem": "Nenhum anúncio encontrado no ML para este OEM",
                "tempo_s": round(time.time() - t0, 1)
            })

        # Guarda a lista bruta (antes do filtro de código): a maioria dos anúncios
        # da peça NÃO repete o OEM no título e seria cortada — para o PREÇO queremos
        # a amostra ampla de turbinas, filtrada só por nome da peça (não por código).
        anuncios_brutos = list(anuncios)

        # PASSO 5: descarta poluição de busca (anúncios de outras peças)
        anuncios = _filtrar_por_relevancia_oem(anuncios, oem)

        # 2. Filtra anúncios isolados (aparecem apenas 1 vez)
        anuncios = _filtrar_anuncios_isolados(anuncios)

        # 3. IA extrai compatibilidade dos títulos
        compat_raw = _extrair_compat_ia(oem, anuncios)

        grupos = _agrupar_compat(compat_raw)
        print(f"[DEBUG] Após agrupamento (marca|modelo): {len(grupos)} grupos")

        # Reconta frequência real contra todos os anúncios (a dedup zera o count)
        grupos = _recontar_frequencia_real(grupos, anuncios)

        # Anos por EVIDÊNCIA real nos anúncios (nunca inferidos por IA/geração)
        grupos = _confianca_anos(grupos, anuncios)

        # PASSO 5: rejeita marca/modelo vazios e None (ano vazio NÃO rejeita)
        aprovados, rejeitados = _rejeitar_invalidos(grupos)

        # PASSO 4/7: score por linha, ordenado
        aprovados = _calcular_scores(aprovados)

        total_confirmados = len(compat_raw)
        confianca = aprovados[0]["score"] if aprovados else 0

        # PASSO 6: salva no Supabase apenas os aprovados
        salvos = 0
        if aprovados:
            salvos = _salvar_supabase(oem, aprovados, confianca)
        print(f"[DEBUG] Salvos Supabase: {salvos}")

        # PASSO 7: tabela Marca | Modelo | Motor | Ano Inicial | Ano Final | Score
        tabela = [
            {
                "marca": g.get("marca"),
                "modelo": g.get("modelo"),
                "motor": g.get("motor") or "",
                "ano_inicial": g.get("ano_inicial"),
                "ano_final": g.get("ano_final"),
                "anos": (f"{g.get('ano_inicial')}-{g.get('ano_final')}"
                         if g.get("evidencia_ano", 0) >= 1 else "Não confirmados"),
                "anos_confirmados": g.get("anos_confirmados", False),
                "score": g.get("score", 0),
            }
            for g in aprovados
        ]

        # Formato compatível com o frontend existente (mesmo shape do Railway)
        compat_confirmadas = [
            {
                "veiculo": f"{g.get('marca','')} {g.get('modelo','')}".strip(),
                "anos": f"{g.get('ano_inicial','')} - {g.get('ano_final','')}" if g.get("ano_inicial") else "",
                "detalhes": g.get("motor", ""),
                "marca": g.get("marca"),
                "modelo": g.get("modelo"),
                "motor": g.get("motor"),
                "cambio": g.get("cambio"),
                "ano_inicial": g.get("ano_inicial"),
                "ano_final": g.get("ano_final"),
                "score": g.get("score", 0),
            }
            for g in aprovados
        ]

        # Nome da peça e títulos dos anúncios (pro painel exibir)
        nome_peca = _nome_peca_do_titulo(anuncios, oem)
        titulos_anuncios = [a.get("titulo", "") for a in anuncios if a.get("titulo")][:10]

        # Preço de mercado da peça NOVA: usa os preços que a própria busca já raspou
        # (1 navegador, não 3), descarta partes/acessórios e usados pelo título e
        # corta outliers (IQR). O ML não separa novo/usado por OEM de forma confiável.
        # dedup por título: o ML repete o mesmo anúncio patrocinado várias vezes;
        # contar a repetição enviesaria a mediana para o preço desse anúncio.
        itens_preco, vistos_preco = [], set()
        for a in anuncios_brutos:
            chave = re.sub(r'\s+', ' ', (a.get("titulo") or "").strip().lower())
            praw = re.sub(r'\D', '', a.get("preco") or "")
            if praw and chave and chave not in vistos_preco:
                vistos_preco.add(chave)
                itens_preco.append({"preco": int(praw), "titulo": a.get("titulo", "")})
        preco = _faixa_preco(_filtrar_precos_da_peca(itens_preco, nome_peca))

        print(f"[LOCAL] Concluído em {round(time.time()-t0,1)}s — "
              f"{len(aprovados)} aprovadas, {len(rejeitados)} rejeitadas")
        return jsonify({
            "ok": True,
            "oem": oem,
            "fonte": "ml_local",
            "compatibilidades_confirmadas": compat_confirmadas,
            "tabela": tabela,
            "preco": preco,
            "nome_peca": nome_peca,
            "titulos": titulos_anuncios,
            "grau_de_confianca": confianca,
            "total_anuncios": len(anuncios),
            "total_confirmados": total_confirmados,
            "total_aprovadas": len(aprovados),
            "total_rejeitadas": len(rejeitados),
            "salvos_supabase": salvos,
            "tempo_s": round(time.time() - t0, 1)
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "erro": str(e), "tempo_s": round(time.time() - t0, 1)}), 500


@app.route("/status", methods=["GET"])
def status():
    return jsonify({"ok": True, "msg": "Servidor local de compatibilidade OEM rodando"})


if __name__ == "__main__":
    print("=" * 50)
    print("Servidor local de compatibilidade OEM")
    print("Porta: 5001")
    print("Endpoint: POST http://localhost:5001/buscar-compat")
    print("=" * 50)
    app.run(host="127.0.0.1", port=5001, debug=False)
