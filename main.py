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
async def fetch_meli_html(query: str):
    raw = f"https://lista.mercadolivre.com.br/{quote_plus(query)}"

    ua_hdrs = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    }

    # 1ª tentativa já com js_render=true (quando tiver proxy)
    if USE_PROXY:
        target = build_target(raw, force_params={"js_render": "true"})
    else:
        target = raw

    st1, tx1, hd1 = await get_text(target, ua_hdrs)
    tries = [{"status": st1, "target": target, "headers": hd1, "raw_preview": tx1[:600], "html": tx1}]

    # Se veio muito curto ou erro, tenta sem js_render (inverte)
    if (st1 >= 400 or len(tx1) < 1000) and USE_PROXY:
        target2 = build_target(raw, force_params={"js_render": "false"})
        st2, tx2, hd2 = await get_text(target2, ua_hdrs)
        tries.append({"status": st2, "target": target2, "headers": hd2, "raw_preview": tx2[:600], "html": tx2})

    return tries


# ========= Plano B (HTML público) — NOVO =========

def parse_ml_list_html(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    items: list[dict] = []

    # --- A) JSON-LD (ItemList) ---
    for s in soup.find_all("script", {"type": "application/ld+json"}):
        txt = (s.string or s.text or "").strip()
        if not txt:
            continue
        try:
            data = json.loads(txt)
        except Exception:
            # às vezes vem múltiplos JSONs concatenados; tenta por regex de blocos {...}
            for m in re.finditer(r"\{.*?\}", txt, flags=re.S):
                try:
                    data = json.loads(m.group(0))
                except Exception:
                    continue
                else:
                    # processa bloco encontrado
                    if isinstance(data, dict) and ("itemListElement" in data or data.get("@type") == "ItemList"):
                        for el in data.get("itemListElement", []):
                            node = el.get("item") if isinstance(el, dict) else None
                            if not isinstance(node, dict):
                                continue
                            title = node.get("name")
                            link  = node.get("url")
                            offer = node.get("offers") or {}
                            price = offer.get("price")
                            thumb = None
                            if title and link:
                                items.append({
                                    "title": title,
                                    "price": price_to_number(str(price)) if price else None,
                                    "link": link,
                                    "thumbnail": thumb,
                                })
                    continue
            continue  # não era JSON útil
        # JSON válido direto
        if isinstance(data, dict) and ("itemListElement" in data or data.get("@type") == "ItemList"):
            for el in data.get("itemListElement", []):
                node = el.get("item") if isinstance(el, dict) else None
                if not isinstance(node, dict):
                    continue
                title = node.get("name")
                link  = node.get("url")
                offer = node.get("offers") or {}
                price = offer.get("price")
                thumb = None
                if title and link:
                    items.append({
                        "title": title,
                        "price": price_to_number(str(price)) if price else None,
                        "link": link,
                        "thumbnail": thumb,
                    })

    # se já coletou itens pelo JSON-LD, retorna (limita 12)
    if items:
        return items[:12]

    # --- B) Seletores de cartão (HTML) ---
    cards = soup.select('[data-testid="product"], .ui-search-result__wrapper, .ui-search-layout__item, li.ui-search-layout__item')
    for c in cards:
        a = c.select_one('a[href*="mercadolivre.com"]') or c.find("a", href=True)
        href = a["href"] if a and a.has_attr("href") else None

        title_el = c.select_one('[data-testid="product-title"], h2.ui-search-item__title, .ui-search-item__title')
        title = title_el.get_text(strip=True) if title_el else None

        img_el = c.select_one('img[data-src], img[src]')
        thumb = (img_el.get("data-src") or img_el.get("src")) if img_el else None

        price_el = c.select_one('.andes-money-amount__fraction, [data-testid="price"], span.ui-search-price__part')
        price_txt = price_el.get_text(strip=True) if price_el else None
        price = price_to_number(price_txt)

        if href and title:
            items.append({"title": title, "price": price, "link": href, "thumbnail": thumb})
        if len(items) >= 12:
            break

    if items:
        return items

    # --- C) Regex bruto como último recurso ---
    # Busca pares "permalink","title","price" dentro do HTML
    # (não é perfeito, mas salva quando o layout muda muito)
    # permalink
    links = re.findall(r'"permalink"\s*:\s*"([^"]+)"', html)
    titles = re.findall(r'"title"\s*:\s*"([^"]+)"', html)
    prices = re.findall(r'"price"\s*:\s*([0-9]+)', html)

    for i, link in enumerate(links[:12]):
        title = titles[i] if i < len(titles) else None
        price = int(prices[i]) if i < len(prices) else None
        if title and link:
            items.append({"title": title, "price": price, "link": link, "thumbnail": None})

    return items
