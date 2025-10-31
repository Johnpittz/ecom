# main.py — E-commerce SEO API (Mercado Livre)
from fastapi import FastAPI, Query
from fastapi.responses import RedirectResponse
import os, json, re, html as html_unescape
from urllib.parse import quote_plus, urlencode, urlparse

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import google.generativeai as genai

load_dotenv()

app = FastAPI(title="E-commerce SEO API")

# ---- ENV ----
GOOGLE_KEY  = os.getenv("GOOGLE_API_KEY", "").strip()
PROXY_BASE  = os.getenv("ML_PROXY_URL", "").strip()          # ex: https://api.zenrows.com/v1/?apikey=KEY&url=
PROXY_EXTRA = os.getenv("ML_PROXY_EXTRA", "").strip()        # ex: &js_render=true&premium_proxy=true
USE_PROXY   = bool(PROXY_BASE)

if GOOGLE_KEY:
    genai.configure(api_key=GOOGLE_KEY)

# ---- Utils / Helpers ----
def build_target(raw_url: str, force_params: dict | None = None) -> str:
    """Encapsula a URL real no proxy, adicionando PROXY_EXTRA e params forçados."""
    if not USE_PROXY:
        return raw_url
    target = f"{PROXY_BASE}{quote_plus(raw_url)}{PROXY_EXTRA}"
    if force_params:
        target += "&" + urlencode(force_params)
    return target

async def get_text(url: str, headers: dict | None = None, timeout: int = 60) -> tuple[int, str, dict]:
    async with httpx.AsyncClient(timeout=timeout, headers=headers or {}) as client:
        resp = await client.get(url)
        return resp.status_code, resp.text, dict(resp.headers)

def price_to_number(txt: str | None) -> int | None:
    if not txt:
        return None
    digits = re.sub(r"[^\d]", "", txt)
    if not digits:
        return None
    try:
        return int(digits)
    except Exception:
        return None

BAD_TITLES = {"Publicidade", "Buscas relacionadas", "Perguntas relacionadas", "Tudo sobre", "Resumo dos Prós e Contras do iPhone"}

def normalize_link(u: str | None) -> str | None:
    if not u:
        return None
    u = u.replace("\\u002F", "/").replace("$query_NoIndex_True", "").replace("$query", "")
    return html_unescape.unescape(u)

def is_product_link(u: str | None) -> bool:
    if not u:
        return False
    try:
        p = urlparse(u)
        host = (p.netloc or "").lower()
        path = (p.path or "").lower()
        # produto típico: produto.mercadolivre.com.br/MLB-... ou caminho com "mlb-"
        return ("mercadolivre.com.br" in host) and ("mlb-" in path or "produto.mercadolivre.com.br" in host)
    except Exception:
        return False

def make_prompt(q: str, items: list) -> str:
    return f"""
Gere uma descrição persuasiva e otimizada para SEO em português
para "{q}", com base nesses itens (título, preço, link):

{json.dumps(items, ensure_ascii=False, indent=2)}

Entregue:
- Título SEO (≤60 caracteres)
- Meta description (≤155 caracteres)
- 3 bullets de benefícios
- 3 FAQs curtas
"""

# ---- Rotas básicas ----
@app.get("/")
def root():
    return RedirectResponse("/docs")

@app.get("/health")
async def health():
    return {"ok": True}

@app.get("/debug/env")
def debug_env():
    def _mask(s: str, keep: int = 12):
        if not s: return ""
        return s[:keep] + "..." if len(s) > keep else s
    return {"use_proxy": USE_PROXY, "proxy_base_preview": _mask(PROXY_BASE), "proxy_extra": PROXY_EXTRA}

# ---- Plano A: API JSON oficial (pode sofrer bloqueios) ----
async def fetch_meli_json(query: str):
    base = "https://api.mercadolibre.com/sites/MLB/search"
    raw  = f"{base}?q={quote_plus(query)}&limit=5"
    target = build_target(raw)
    headers = {"User-Agent": "ecom-seo/1.0", "Accept": "application/json"}
    try:
        status, text, hdrs = await get_text(target, headers)
        data = None
        try:
            data = json.loads(text)
        except Exception:
            data = None
        return {"status": status, "headers": hdrs, "target": target, "raw_preview": text[:800], "json": data}
    except Exception as e:
        return {"status": 0, "target": target, "error": str(e), "headers": {}, "raw_preview": ""}

    # 1) Proxy com render (se houver proxy)
    if USE_PROXY:
        target1 = build_target(raw, force_params={"js_render": "true"})
        st1, tx1, hd1 = await get_text(target1, ua_hdrs)
        tries.append({"status": st1, "target": target1, "headers": hd1, "raw_preview": tx1[:600], "html": tx1})

        # 2) Proxy sem render (fallback)
        if st1 >= 400 or len(tx1) < 1000:
            target2 = build_target(raw, force_params={"js_render": "false"})
            st2, tx2, hd2 = await get_text(target2, ua_hdrs)
            tries.append({"status": st2, "target": target2, "headers": hd2, "raw_preview": tx2[:600], "html": tx2})

    # 3) Tentar DIRETO (sem proxy) — MUITO ÚTIL quando proxy é marcado como bot
    st3, tx3, hd3 = await get_text(raw, ua_hdrs)
    tries.append({"status": st3, "target": raw, "headers": hd3, "raw_preview": tx3[:600], "html": tx3})

    return tries

@app.get("/meli/search")
async def meli_search(q: str = Query(..., description="Produto a buscar (API JSON ML)")):
    meli = await fetch_meli_json(q)
    if meli["status"] != 200 or not isinstance(meli.get("json"), dict):
        return {"message": "Mercado Livre não retornou JSON válido.", "diagnostic": meli}

    results = []
    for item in meli["json"].get("results", []):
        results.append({
            "title": item.get("title"),
            "price": item.get("price"),
            "link": item.get("permalink"),
            "thumbnail": item.get("thumbnail"),
        })
    if not results:
        return {"message": "Sem resultados ou bloqueado.", "meli_status": meli["status"], "meli_preview": meli["raw_preview"]}

    seo_text = "(IA desativada: defina GOOGLE_API_KEY)"
    if GOOGLE_KEY:
        try:
            model = genai.GenerativeModel("models/gemini-1.5-flash-latest")
            seo_text = model.generate_content(make_prompt(q, results)).text
        except Exception as e:
            seo_text = f"(Falha ao gerar SEO: {e})"

    return {"query": q, "results": results, "seo_text": seo_text}

# ---- Plano B: HTML público (com extractor de JSON inline) ----
def extract_from_inline_json(html: str) -> list[dict]:
    """
    Procura blobs JSON comuns em páginas do ML (pré-carregados ou em bundles).
    """
    items: list[dict] = []

    # A) window.__PRELOADED_STATE__ = {...}
    m = re.search(r"__PRELOADED_STATE__\s*=\s*({.*?});\s*</script>", html, flags=re.S)
    # B) __NEXT_DATA__
    if not m:
        m = re.search(r"__NEXT_DATA__\"\s*:\s*({.*?})\s*</script>", html, flags=re.S)
    # C) Qualquer blob que contenha results/permalink/title (bem permissivo)
    if not m:
        m = re.search(r"(\{[^<>{}]*\"results\"\s*:\s*\[.*?\}\])", html, flags=re.S)
    if not m:
        m = re.search(r"(\{[^<>{}]*\"permalink\"\s*:\s*\"https?://[^\"}]+\".*?\})", html, flags=re.S)

    if not m:
        return items

    blob = m.group(1).replace("\\u002F", "/")

    # Captura chaves usuais
    links   = re.findall(r'"permalink"\s*:\s*"([^"]+)"', blob)
    titles  = re.findall(r'"title"\s*:\s*"([^"]+)"', blob)
    prices  = re.findall(r'"price"\s*:\s*([0-9]+)', blob)
    thumbs  = re.findall(r'"thumbnail"\s*:\s*"([^"]+)"', blob)

    n = max(len(links), len(titles), len(prices), len(thumbs), 0)
    for i in range(n):
        link  = normalize_link(links[i]) if i < len(links)  else None
        title = (titles[i] if i < len(titles) else "").strip()
        price = int(prices[i]) if i < len(prices) else None
        thumb = normalize_link(thumbs[i]) if i < len(thumbs) else None

        if not title or title in BAD_TITLES:
            continue
        if not is_product_link(link):
            continue

        items.append({"title": title, "price": price, "link": link, "thumbnail": thumb})
        if len(items) >= 12:
            break

    return items

def parse_ml_list_html(html: str) -> list[dict]:
    # 0) Tenta primeiro os blobs JSON inline
    items = extract_from_inline_json(html)
    if items:
        return items

    # 1) JSON-LD
    soup = BeautifulSoup(html, "lxml")
    items = []
    for s in soup.find_all("script", {"type": "application/ld+json"}):
        txt = (s.string or s.text or "").strip()
        if not txt:
            continue
        try:
            data = json.loads(txt)
        except Exception:
            continue
        if isinstance(data, dict) and ("itemListElement" in data or data.get("@type") == "ItemList"):
            for el in data.get("itemListElement", []):
                node = el.get("item") if isinstance(el, dict) else None
                if not isinstance(node, dict):
                    continue
                title = (node.get("name") or "").strip()
                link  = normalize_link(node.get("url"))
                if not title or title in BAD_TITLES or not is_product_link(link):
                    continue
                price = None
                offer = node.get("offers") or {}
                if isinstance(offer, dict) and offer.get("price") is not None:
                    price = price_to_number(str(offer.get("price")))
                items.append({"title": title, "price": price, "link": link, "thumbnail": None})
    if items:
        return items[:12]

    # 2) Seletores de cartão (fallback)
    cards = soup.select('[data-testid="product"], .ui-search-result__wrapper, .ui-search-layout__item, li.ui-search-layout__item')
    for c in cards:
        a = c.select_one('a[href*="mercadolivre.com"]') or c.find("a", href=True)
        href = normalize_link(a.get("href") if a else None)

        title_el = c.select_one('[data-testid="product-title"], h2.ui-search-item__title, .ui-search-item__title')
        title = (title_el.get_text(strip=True) if title_el else "").strip()

        if not title or title in BAD_TITLES or not is_product_link(href):
            continue

        img_el = c.select_one('img[data-src], img[src]')
        thumb = normalize_link((img_el.get("data-src") or img_el.get("src")) if img_el else None)

        price_el = c.select_one('.andes-money-amount__fraction, [data-testid="price"], span.ui-search-price__part')
        price_txt = price_el.get_text(strip=True) if price_el else None
        price = price_to_number(price_txt)

        items.append({"title": title, "price": price, "link": href, "thumbnail": thumb})
        if len(items) >= 12:
            break

    return items

async def fetch_meli_html(query: str):
    raw = f"https://lista.mercadolivre.com.br/{quote_plus(query)}"
    ua_hdrs = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    }
    # 1ª tentativa com js_render=true (quando houver proxy)
    target = build_target(raw, force_params={"js_render": "true"}) if USE_PROXY else raw
    st1, tx1, hd1 = await get_text(target, ua_hdrs)
    tries = [{"status": st1, "target": target, "headers": hd1, "raw_preview": tx1[:600], "html": tx1}]
    # Se falhar/curto, tenta sem render
    if (st1 >= 400 or len(tx1) < 1000) and USE_PROXY:
        target2 = build_target(raw, force_params={"js_render": "false"})
        st2, tx2, hd2 = await get_text(target2, ua_hdrs)
        tries.append({"status": st2, "target": target2, "headers": hd2, "raw_preview": tx2[:600], "html": tx2})
    return tries

@app.get("/meli/search_html")
async def meli_search_html(q: str = Query(..., description="Produto a buscar (HTML público)")):
    traces = await fetch_meli_html(q)

    html = None
    for t in reversed(traces):
        if t.get("html") and "<html" in t["html"].lower():
            html = t["html"]
            break
    if not html:
        return {"message": "Não foi possível obter HTML da busca.", "tries": traces}

    results = parse_ml_list_html(html)
    if not results:
        return {"message": "HTML obtido, mas nenhum item foi parseado.", "tries": traces[:1], "html_preview": html[:500]}

    # IA opcional
    seo_text = "(IA desativada: defina GOOGLE_API_KEY)"
    if GOOGLE_KEY:
        try:
            model = genai.GenerativeModel("models/gemini-1.5-flash-latest")
            seo_text = model.generate_content(make_prompt(q, results)).text
        except Exception as e:
            seo_text = f"(Falha ao gerar SEO: {e})"

    return {"query": q, "results": results, "seo_text": seo_text}

# (Opcional) rota para depurar HTML retornado
@app.get("/meli/search_html_debug")
async def meli_search_html_debug(q: str = Query(...)):
    traces = await fetch_meli_html(q)
    best = traces[-1] if traces else {}
    return {
        "status": best.get("status"),
        "target": best.get("target"),
        "headers": best.get("headers"),
        "html_preview": (best.get("html") or "")[:2000]
    }
