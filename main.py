# main.py — API de busca + SEO (com debug e proxy opcional)
from fastapi import FastAPI, Query
import httpx, os, json
from dotenv import load_dotenv
import google.generativeai as genai
from urllib.parse import quote_plus
from fastapi.responses import RedirectResponse

load_dotenv()
app = FastAPI(title="E-commerce SEO API")

@app.get("/")
def root():
    return RedirectResponse("/docs")

# 🔑 Variáveis de ambiente (NÃO colocar chaves direto no código!)
GOOGLE_KEY = os.getenv("GOOGLE_API_KEY", "").strip()
PROXY_BASE = os.getenv("ML_PROXY_URL", "").strip()          # ex: http://api.scraperapi.com?api_key=KEY&url=
PROXY_EXTRA = os.getenv("ML_PROXY_EXTRA", "").strip()        # ex: &country_code=br&device_type=desktop&render=false&keep_headers=true
USE_PROXY = bool(PROXY_BASE)

if GOOGLE_KEY:
    genai.configure(api_key=GOOGLE_KEY)


def build_target(url: str) -> str:
    """Se houver proxy, encapsula a URL real do ML na URL do proxy."""
    if USE_PROXY:
        return f"{PROXY_BASE}{quote_plus(url)}{PROXY_EXTRA}"
    return url


async def fetch_meli(query: str):
    base = "https://api.mercadolibre.com/sites/MLB/search"
    url = f"{base}?q={quote_plus(query)}&limit=5"
    target = build_target(url)

    headers = {
        "User-Agent": "ecom-seo/1.0 (+https://example.com/contact)",
        "Accept": "application/json",
    }

    async with httpx.AsyncClient(timeout=60, headers=headers) as client:
        last_exc = None
        for attempt in range(3):
            try:
                resp = await client.get(target)
                text = resp.text
                data = None
                try:
                    data = resp.json()
                except Exception:
                    pass
                return {
                    "status": resp.status_code,
                    "headers": dict(resp.headers),
                    "target": target,
                    "raw_preview": text[:800],  # primeiros 800 chars p/ debug
                    "json": data,
                    "attempts": attempt + 1,
                }
            except (httpx.ReadTimeout, httpx.ConnectTimeout) as e:
                last_exc = e
                await asyncio.sleep(1.5 * (attempt + 1))

        # se chegou aqui, estourou os retries
        return {
            "status": 0,
            "target": target,
            "error": f"Timeout após retries: {last_exc}",
            "json": None,
            "raw_preview": "",
            "attempts": 3,
        }


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


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/meli/search")
async def meli_search(q: str = Query(..., description="Produto a buscar")):
    meli = await fetch_meli(q)

    # Se não veio 200 ou não há JSON válido, mostre diagnóstico ao invés de quebrar
    if meli["status"] != 200 or not isinstance(meli["json"], dict):
        return {
            "message": "Mercado Livre não retornou JSON válido.",
            "diagnostic": {
                "status": meli["status"],
                "target": meli["target"],
                "headers": meli.get("headers", {}),
                "raw_preview": meli.get("raw_preview", ""),
                "attempts": meli.get("attempts"),
                "error": meli.get("error"),
            }
        }

    # Normaliza itens
    results = []
    for item in meli["json"].get("results", []):
        results.append({
            "title": item.get("title"),
            "price": item.get("price"),
            "link": item.get("permalink"),
            "thumbnail": item.get("thumbnail"),
        })

    if not results:
        return {
            "message": "Sem resultados ou bloqueado.",
            "meli_status": meli["status"],
            "meli_preview": meli["raw_preview"],
        }

    # IA (opcional)
    seo_text = "(IA desativada: defina GOOGLE_API_KEY no .env)"
    if GOOGLE_KEY:
        try:
            model = genai.GenerativeModel("gemini-1.5-flash")
            seo_text = model.generate_content(make_prompt(q, results)).text
        except Exception as e:
            seo_text = f"(Falha ao gerar SEO: {e})"

    return {"query": q, "results": results, "seo_text": seo_text}
