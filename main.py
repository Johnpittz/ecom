# main.py — Baseline simples (JSON + HTML) com parser "solto"
from fastapi import FastAPI, Query
from fastapi.responses import RedirectResponse
import os, json, re, html as html_unescape
from urllib.parse import quote_plus, urlencode

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import google.generativeai as genai

load_dotenv()

app = FastAPI(title="E-commerce SEO API (baseline)")

# ---------------- ENV ----------------
GOOGLE_KEY  = os.getenv("GOOGLE_API_KEY", "").strip()
PROXY_BASE  = os.getenv("ML_PROXY_URL", "").strip()          # ex: https://api.zenrows.com/v1/?apikey=KEY&url=
PROXY_EXTRA = os.getenv("ML_PROXY_EXTRA", "").strip()        # ex: &js_render=true&premium_proxy=true
USE_PROXY   = bool(PROXY_BASE)

if GOOGLE_KEY:
    genai.configure(api_key=GOOGLE_KEY)

# ---------------- Utils ----------------
def build_target(raw_url: str, force_params: dict | None = None) -> str:
    """Encapsula a URL real no proxy (se houver)."""
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

def normalize_link(u: str | None) -> str | None:
    """Apenas normaliza escapes comuns (mantemos simples)."""
    if not u:
        return None
    u = u.replace("\\u002F", "/").replace("$query_NoIndex_True", "").replace("$query", "")
    return html_unescape.unescape(u)

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

# ---------------- Rotas básicas ----------------
@app.get("/")
def root():
    return RedirectResponse("/docs")

@app.get("/health")
async def health():
    return {"ok": True}

@app.get("/debug/env")
def debug_env():
    def _mask(s: str, keep: int = 10):
        if not s: return ""
        return s[:keep] + "..." if len(s) > keep else s
    return {"use_proxy": USE_PROXY, "proxy_base_preview": _mask(PROXY_BASE), "proxy_extra": PROXY_EXTRA}

# ---------------- Plano A: API JSON oficial (pode bloquear) ----------------
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

    # IA (com modelo válido)
    seo_text = "(IA desativada: defina GOOGLE_API_KEY)"
    if GOOGLE_KEY:
        try:
            model = genai.GenerativeModel("models/gemini-1.5-flash-latest")
            seo_text = model.generate_content(make_prompt(q, results)).text
        except Exception as e:
            seo_text = f"(Falha ao gerar SEO: {e})"

    return {"query": q, "results": results, "seo_text": seo_text}

# ---------------- Plano B: HTML público (parser simples) ----------------
def parse_ml_list_html_simple(html: str) -> list[dict]:
    """
    Parser simples — o mesmo estilo que te retornou itens como 'Publicidade', etc.
    (Sem filtros agressivos para garantir que apareçam resultados.)
    """
    soup = BeautifulSoup(html, "lxml")
    items: list[dict] = []

    # A) JSON-LD (se tiver)
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
                offer = node.get("offers") or {}
                price = None
                if isinstance(offer, dict) and offer.get("price") is not None:
                    price = price_to_number(str(offer.get("price")))
                if title and link:
                    items.append({"title": title, "price": price, "link": link, "thumbnail": None})
    if items:
        return items[:12]

    # B) Seletores "cartão" — bem solto
    cards = soup.select('[data-testid="product"], .ui-search-result__wrapper, .ui-search-layout__item, li.ui-search-layout__item')
    for c in cards:
        a = c.select_one('a[href*="mercadolivre.com"]') or c.find("a", href=True)
        href = normalize_link(a.get("href") if a else None)

        title_el = c.select_one('[data-testid="product-title"], h2.ui-search-item__title, .ui-search-item__title')
        title = (title_el.get_text(strip=True) if title_el else "").strip()

        img_el = c.select_one('img[data-src], img[src]')
        thumb = normalize_link((img_el.get("data-src") or img_el.get("src")) if img_el else None)

        price_el = c.select_one('.andes-money-amount__fraction, [data-testid="price"], span.ui-search-price__part')
        price_txt = price_el.get_text(strip=True) if price_el else None
        price = price_to_number(price_txt)

        if href and title:
            items.append({"title": title, "price": price, "link": href, "thumbnail": thumb})
        if len(items) >= 12:
            break
    if items:
        return items

    # C) Regex fallback (permalink/title/price) — também bem permissivo
    links  = re.findall(r'"permalink"\s*:\s*"([^"]+)"', html)
    titles = re.findall(r'"title"\s*:\s*"([^"]+)"', html)
    prices = re.findall(r'"price"\s*:\s*([0-9]+)', html)

    for i, link in enumerate(links[:12]):
        link = normalize_link(link)
        title = (titles[i] if i < len(titles) else "").strip()
        price = int(prices[i]) if i < len(prices) else None
        if title and link:
            items.append({"title": title, "price": price, "link": link, "thumbnail": None})

    return items

async def fetch_meli_html(query: str):
    raw = f"https://lista.mercadolivre.com.br/{quote_plus(query)}"
    ua_hdrs = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    }
    target = build_target(raw, force_params={"js_render": "true"}) if USE_PROXY else raw
    st1, tx1, hd1 = await get_text(target, ua_hdrs)
    tries = [{"status": st1, "target": target, "headers": hd1, "raw_preview": tx1[:600], "html": tx1}]
    # fallback sem render (a mesma lógica de quando testamos)
    if (st1 >= 400 or len(tx1) < 1000) and USE_PROXY:
        target2 = build_target(raw, force_params={"js_render": "false"})
        st2, tx2, hd2 = await get_text(target2, ua_hdrs)
        tries.append({"status": st2, "target": target2, "headers": hd2, "raw_preview": tx2[:600], "html": tx2})
    return tries

@app.get("/meli/search_html")
async def meli_search_html(q: str = Query(..., description="Produto a buscar (HTML público — baseline)")):
    traces = await fetch_meli_html(q)

    html = None
    for t in reversed(traces):
        if t.get("html") and "<html" in t["html"].lower():
            html = t["html"]
            break
    if not html:
        return {"message": "Não foi possível obter HTML da busca.", "tries": traces}

    results = parse_ml_list_html_simple(html)
    if not results:
        return {"message": "HTML obtido, mas nenhum item foi parseado.", "tries": traces[:1], "html_preview": html[:600]}

    seo_text = "(IA desativada: defina GOOGLE_API_KEY)"
    if GOOGLE_KEY:
        try:
            model = genai.GenerativeModel("models/gemini-1.5-flash-latest")
            seo_text = model.generate_content(make_prompt(q, results)).text
        except Exception as e:
            seo_text = f"(Falha ao gerar SEO: {e})"

    return {"query": q, "results": results, "seo_text": seo_text}

# (opcional) rota para inspecionar o HTML retornado
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
