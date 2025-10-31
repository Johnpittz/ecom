# main.py — API de busca (HTML) + SEO, com proxy opcional (ZenRows ou outro)
from fastapi import FastAPI, Query
from fastapi.responses import RedirectResponse
import httpx, os, json, asyncio, re
from dotenv import load_dotenv
import google.generativeai as genai
from urllib.parse import quote_plus, urlencode
from bs4 import BeautifulSoup

load_dotenv()

app = FastAPI(title="E-commerce SEO API")

# 🔑 Env vars (configure no Render → Environment)
GOOGLE_KEY  = os.getenv("GOOGLE_API_KEY", "").strip()
PROXY_BASE  = os.getenv("ML_PROXY_URL", "").strip()          # ex: https://api.zenrows.com/v1/?apikey=KEY&url=
PROXY_EXTRA = os.getenv("ML_PROXY_EXTRA", "").strip()        # ex: &js_render=false (ou true), etc.
USE_PROXY   = bool(PROXY_BASE)

if GOOGLE_KEY:
    genai.configure(api_key=GOOGLE_KEY)

# ========= helpers =========

def build_target(raw_url: str, force_params: dict | None = None) -> str:
    """
    Se houver proxy, encapsula raw_url no proxy. Permite forçar parâmetros extras (ex.: js_render=true)
    """
    if not USE_PROXY:
        return raw_url
    # base já termina com ...&url=
    target = f"{PROXY_BASE}{quote_plus(raw_url)}{PROXY_EXTRA}"
    if force_params:
        # acrescenta parâmetros extras mesmo que não estejam no ML_PROXY_EXTRA
        extra = "&" + urlencode(force_params)
        target = target + extra
    return target

async def get_text(url: str, headers: dict | None = None, timeout: int = 60) -> tuple[int, str, dict]:
    async with httpx.AsyncClient(timeout=timeout, headers=headers or {}) as client:
        resp = await client.get(url)
        return resp.status_code, resp.text, dict(resp.headers)

def price_to_number(txt: str | None) -> int | None:
    if not txt:
        return None
    # pega apenas dígitos (ignora separadores)
    digits = re.sub(r"[^\d]", "", txt)
    if not digits:
        return None
    try:
        return int(digits)
    except Exception:
        return None

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

# ========= rotas utilitárias =========

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
    return {
        "use_proxy": USE_PROXY,
        "proxy_base_preview": _mask(PROXY_BASE),
        "proxy_extra": PROXY_EXTRA,
    }

# ========= Plano A (API JSON oficial) — continua aqui, mas pode falhar =========
async def fetch_meli_json(query: str):
    base = "https://api.mercadolibre.com/sites/MLB/search"
    raw  = f"{base}?q={quote_plus(query)}&limit=5"
    target = build_target(raw)
    headers = {
        "User-Agent": "ecom-seo/1.0",
        "Accept": "application/json",
    }
    try:
        status, text, hdrs = await get_text(target, headers)
        data = None
        try:
            data = json.loads(text)
        except Exception:
            data = None
        return {
            "status": status,
            "headers": hdrs,
            "target": target,
            "raw_preview": text[:800],
            "json": data,
        }
    except Exception as e:
        return {"status": 0, "target": target, "error": str(e), "headers": {}, "raw_preview": ""}

@app.get("/meli/search")
async def meli_search(q: str = Query(..., description="Produto a buscar")):
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

    seo_text = "(IA desativada: defina GOOGLE_API_KEY no .env)"
    if GOOGLE_KEY:
        try:
            model = genai.GenerativeModel("gemini-1.5-flash")
            seo_text = model.generate_content(make_prompt(q, results)).text
        except Exception as e:
            seo_text = f"(Falha ao gerar SEO: {e})"

    return {"query": q, "results": results, "seo_text": seo_text}

# ========= Plano B (HTML público) — NOVO =========

def parse_ml_list_html(html: str) -> list[dict]:
    """
    Faz o parsing dos cards de resultado da busca pública do Mercado Livre.
    Usa seletores tolerantes a mudanças de classe.
    """
    soup = BeautifulSoup(html, "lxml")

    # Os cards geralmente possuem data-testid="product" OU classes 'ui-search-result__wrapper' etc.
    cards = soup.select('[data-testid="product"], .ui-search-result__wrapper, .ui-search-layout__item')
    items: list[dict] = []

    for c in cards:
        # Link (Âncora principal)
        a = c.select_one('a[href*="mercadolivre.com"], a.ui-search-item__group__element.ui-search-link, a.ui-search-result__content')
        href = a.get("href") if a else None

        # Título
        title_el = c.select_one('[data-testid="product-title"], h2.ui-search-item__title, .ui-search-item__title')
        title = title_el.get_text(strip=True) if title_el else None

        # Imagem
        img_el = c.select_one('img[data-src], img[src]')
        thumb = (img_el.get("data-src") or img_el.get("src")) if img_el else None

        # Preço (fração)
        price_el = c.select_one('.andes-money-amount__fraction, [data-testid="price"]')
        price_txt = price_el.get_text(strip=True) if price_el else None
        price = price_to_number(price_txt)

        if href and title:
            items.append({
                "title": title,
                "price": price,
                "link": href,
                "thumbnail": thumb,
            })

        if len(items) >= 10:
            break

    return items

async def fetch_meli_html(query: str):
    """
    Busca a lista pública (HTML) do Mercado Livre. Tenta sem JS; se falhar, força js_render=true.
    """
    # URL pública de busca (HTML)
    # Exemplos que funcionam: https://lista.mercadolivre.com.br/iphone
    raw = f"https://lista.mercadolivre.com.br/{quote_plus(query)}"

    ua_hdrs = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    # 1) tentativa com o que vier do ML_PROXY_EXTRA (ou sem proxy)
    target1 = build_target(raw)
    st1, tx1, hd1 = await get_text(target1, ua_hdrs)

    # Se falhou no proxy com RESP/403/etc ou HTML vazio, força render JS (quando houver proxy)
    if (USE_PROXY and (st1 >= 400 or len(tx1) < 1000)):
        target2 = build_target(raw, force_params={"js_render": "true"})
        st2, tx2, hd2 = await get_text(target2, ua_hdrs)
        return [
            {"status": st1, "target": target1, "headers": hd1, "raw_preview": tx1[:400]},
            {"status": st2, "target": target2, "headers": hd2, "raw_preview": tx2[:400], "html": tx2},
        ]

    return [
        {"status": st1, "target": target1, "headers": hd1, "raw_preview": tx1[:400], "html": tx1},
    ]

@app.get("/meli/search_html")
async def meli_search_html(q: str = Query(..., description="Produto a buscar (HTML)")):
    traces = await fetch_meli_html(q)

    # escolhe o melhor html válido dentre as tentativas
    html = None
    for t in reversed(traces):  # prioriza a última tentativa (com js_render=true)
        if t.get("html") and "<html" in t["html"].lower():
            html = t["html"]
            break

    if not html:
        return {"message": "Não foi possível obter HTML da busca.", "tries": traces}

    results = parse_ml_list_html(html)
    if not results:
        return {"message": "HTML obtido, mas nenhum item foi parseado.", "tries": traces[:1], "html_preview": html[:500]}

    # IA (opcional)
    seo_text = "(IA desativada: defina GOOGLE_API_KEY no .env)"
    if GOOGLE_KEY:
        try:
            model = genai.GenerativeModel("gemini-1.5-flash")
            seo_text = model.generate_content(make_prompt(q, results)).text
        except Exception as e:
            seo_text = f"(Falha ao gerar SEO: {e})"

    return {"query": q, "results": results, "seo_text": seo_text}
