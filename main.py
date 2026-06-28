import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from playwright.async_api import BrowserContext, async_playwright
from playwright_stealth import Stealth
from pydantic import BaseModel

from scrapers import scrapers
from scrapers.base import BottleResult
from store_generator import GeneratorError, add_store

log = logging.getLogger("spiritfinder")

_context: BrowserContext | None = None

_MANUAL_FILE = Path("manual_entries.json")
_SAVED_FILE  = Path("saved_list.json")

def _load_json_file(path: Path) -> list[dict]:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return []
    return []

def _save_json_file(path: Path, data: list[dict]) -> None:
    path.write_text(json.dumps(data, indent=2))

_manual_entries: list[dict] = _load_json_file(_MANUAL_FILE)
_saved_list:     list[dict] = _load_json_file(_SAVED_FILE)

def _load_manual_entries() -> list[dict]:
    return _load_json_file(_MANUAL_FILE)

def _save_manual_entries(entries: list[dict]) -> None:
    _save_json_file(_MANUAL_FILE, entries)

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

_CACHE_TTL = 1800  # 30 minutes

# (store_name, normalized_query) -> (monotonic_timestamp, results)
_cache: dict[tuple[str, str], tuple[float, list]] = {}

_scrape_sem = asyncio.Semaphore(3)

# Bottle names accumulated from all past searches for autocomplete
_name_index: set[str] = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _context
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--no-zygote",
            "--disable-gpu",
            "--disable-extensions",
            "--disable-software-rasterizer",
            "--single-process",
        ],
    )
    _context = await browser.new_context(
        user_agent=_BROWSER_UA,
        viewport={"width": 1280, "height": 800},
        extra_http_headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    await Stealth().apply_stealth_async(_context)
    yield
    await _context.close()
    await browser.close()
    await pw.stop()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def index():
    return FileResponse("static/index.html")


class SearchRequest(BaseModel):
    query: str


class ResultItem(BaseModel):
    store_name: str
    bottle_name: str
    price: float | None
    url: str
    in_stock: bool
    size: str | None
    error: bool = False
    error_message: str | None = None


def _query_tokens(query: str) -> list[str]:
    """Meaningful words from the query.
    Short tokens are kept when purely numeric (age statements: '10', '12', '21').
    Single/double-letter abbreviations like 'El', 'XO' are still skipped."""
    age_statement = {"vs", "vsop", "xo"}
    return [w for w in query.lower().split() if len(w) > 2 or w.isdigit() or w in age_statement]


def _relevant(name: str, tokens: list[str]) -> bool:
    """True if the bottle name contains at least one query token."""
    if not tokens:
        return True  # query has no long tokens — don't filter
    low = name.lower()
    return any(t in low for t in tokens)


def _relevance_score(name: str, tokens: list[str]) -> float:
    """Fraction of query tokens present in the bottle name (0.0 – 1.0).
    Used as the primary sort key so closer matches rank above cheap-but-wrong results."""
    if not tokens:
        return 1.0
    low = name.lower()
    return sum(1 for t in tokens if t in low) / len(tokens)


async def _run_scraper(scraper, query: str) -> list[ResultItem]:
    cache_key = (scraper.name, query.lower().strip())
    entry = _cache.get(cache_key)
    if entry:
        ts, cached_items = entry
        if time.monotonic() - ts < _CACHE_TTL:
            return cached_items

    async with _scrape_sem:
        return await _run_scraper_inner(scraper, query, cache_key)


async def _run_scraper_inner(scraper, query: str, cache_key: tuple) -> list[ResultItem]:
    assert _context is not None
    page = await _context.new_page()
    tokens = _query_tokens(query)
    try:
        raw: list[BottleResult] = await scraper.search(page, query)
        items = [
            ResultItem(
                store_name=r.store_name,
                bottle_name=r.bottle_name,
                price=r.price,
                url=r.url,
                in_stock=r.in_stock,
                size=r.size,
            )
            for r in raw
            if _relevant(r.bottle_name, tokens)
        ]
        dropped = len(raw) - len(items)
        log.info(
            "[%s] query=%r  raw=%d  kept=%d  dropped=%d",
            scraper.name, query, len(raw), len(items), dropped,
        )
        if dropped and not items:
            # All results were filtered — log a sample so we can see why
            log.info("[%s] filtered-out names: %s", scraper.name,
                     [r.bottle_name for r in raw[:5]])

        # Don't cache empty results — let the next search retry the live site
        if items:
            _cache[cache_key] = (time.monotonic(), items)
            for item in items:
                if item.bottle_name:
                    _name_index.add(item.bottle_name)
        return items
    except Exception as e:
        log.warning("[%s] scrape error: %s", scraper.name, e)
        # Don't cache errors — transient failures shouldn't persist
        return [
            ResultItem(
                store_name=scraper.name,
                bottle_name="",
                price=None,
                url="",
                in_stock=False,
                size=None,
                error=True,
                error_message=str(e),
            )
        ]
    finally:
        await page.close()


@app.post("/search")
async def search(req: SearchRequest) -> list[ResultItem]:
    if not scrapers:
        return []

    per_store = await asyncio.gather(*[_run_scraper(s, req.query) for s in scrapers])

    all_results: list[ResultItem] = [item for store in per_store for item in store]

    tokens = _query_tokens(req.query)

    # Inject matching manual entries
    for entry in _manual_entries:
        if _relevant(entry["bottle_name"], tokens):
            all_results.append(ResultItem(
                store_name=entry["store_name"],
                bottle_name=entry["bottle_name"],
                price=entry.get("price"),
                url=entry.get("url", ""),
                in_stock=entry.get("in_stock", True),
                size=entry.get("size"),
            ))

    priced = [r for r in all_results if not r.error and r.price is not None]
    errors = [r for r in all_results if r.error]

    priced.sort(key=lambda r: (-_relevance_score(r.bottle_name, tokens), r.price or 0.0))
    return priced + errors


class ManualEntryRequest(BaseModel):
    store_name: str
    bottle_name: str
    price: float
    size: str = ""
    in_stock: bool = True
    url: str = ""


@app.post("/manual-entry")
async def add_manual_entry(req: ManualEntryRequest):
    entry = {
        "store_name": req.store_name,
        "bottle_name": req.bottle_name,
        "price": req.price,
        "size": req.size or None,
        "in_stock": req.in_stock,
        "url": req.url or "",
    }
    _manual_entries.append(entry)
    _save_manual_entries(_manual_entries)
    return {"ok": True}


@app.get("/manual-entries")
async def list_manual_entries():
    return _manual_entries


@app.delete("/manual-entry/{index}")
async def delete_manual_entry(index: int):
    if index < 0 or index >= len(_manual_entries):
        return JSONResponse(status_code=404, content={"ok": False})
    _manual_entries.pop(index)
    _save_manual_entries(_manual_entries)
    return {"ok": True}


@app.put("/manual-entry/{index}")
async def update_manual_entry(index: int, req: ManualEntryRequest):
    if index < 0 or index >= len(_manual_entries):
        return JSONResponse(status_code=404, content={"ok": False})
    _manual_entries[index] = {
        "store_name": req.store_name,
        "bottle_name": req.bottle_name,
        "price": req.price,
        "size": req.size or None,
        "in_stock": req.in_stock,
        "url": req.url or "",
    }
    _save_manual_entries(_manual_entries)
    return {"ok": True}


@app.get("/suggest")
async def suggest(q: str = "") -> list[str]:
    q_low = q.strip().lower()
    if len(q_low) < 2:
        return []
    matches = [n for n in _name_index if q_low in n.lower()]
    # Prioritise names that start with the query, then sort alphabetically
    matches.sort(key=lambda n: (not n.lower().startswith(q_low), n.lower()))
    return matches[:12]


@app.get("/debug/scrape")
async def debug_scrape(store: str, q: str):
    """
    Bypass the cache and run one scraper directly.
    Visit: /debug/scrape?store=Burlington+Wine+%26+Spirits&q=el+dorado+12
    """
    target = next((s for s in scrapers if s.name == store), None)
    if not target:
        return JSONResponse(status_code=404, content={
            "error": "store not found",
            "available": [s.name for s in scrapers],
        })
    assert _context is not None
    page = await _context.new_page()
    try:
        raw: list[BottleResult] = await target.search(page, q)
        tokens = _query_tokens(q)
        kept = [r for r in raw if _relevant(r.bottle_name, tokens)]
        dropped = [r for r in raw if not _relevant(r.bottle_name, tokens)]
        # Capture page state so we can see block/CAPTCHA pages when raw=0
        page_title = await page.title()
        page_url = page.url
        page_text = (await page.inner_text("body"))[:400] if raw == [] else ""
        return {
            "store": store,
            "query": q,
            "tokens": tokens,
            "raw_count": len(raw),
            "kept_count": len(kept),
            "dropped_count": len(dropped),
            "kept": [{"name": r.bottle_name, "price": r.price} for r in kept],
            "dropped_names": [r.bottle_name for r in dropped[:20]],
            "page_title": page_title,
            "page_url": page_url,
            "page_text_snippet": page_text,
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
    finally:
        await page.close()


@app.get("/scrapers")
async def list_scrapers():
    return [{"name": s.name} for s in scrapers]


class RenameRequest(BaseModel):
    old_name: str
    new_name: str


@app.post("/rename-store")
async def rename_store(req: RenameRequest):
    for scraper in scrapers:
        if scraper.name == req.old_name:
            # Patch live instance
            scraper.name = req.new_name
            # Patch the file so the name survives a restart
            try:
                for p in Path("scrapers").glob("*.py"):
                    text = p.read_text()
                    needle = f"name = {req.old_name!r}"
                    if needle in text:
                        p.write_text(text.replace(needle, f"name = {req.new_name!r}", 1))
                        break
            except Exception:
                pass
            return {"ok": True}
    return JSONResponse(status_code=404, content={"ok": False, "error": "Store not found"})


class AddStoreRequest(BaseModel):
    url: str
    name: str = ""


class SavedItemRequest(BaseModel):
    store_name: str
    bottle_name: str
    price: float | None = None
    size: str | None = None
    url: str = ""
    in_stock: bool = True
    notes: str = ""


@app.get("/saved-list")
async def get_saved_list():
    return _saved_list


@app.post("/saved-list")
async def add_saved_item(req: SavedItemRequest):
    item = req.model_dump()
    _saved_list.append(item)
    _save_json_file(_SAVED_FILE, _saved_list)
    return {"ok": True, "index": len(_saved_list) - 1}


@app.delete("/saved-list/{index}")
async def delete_saved_item(index: int):
    if index < 0 or index >= len(_saved_list):
        return JSONResponse(status_code=404, content={"ok": False})
    _saved_list.pop(index)
    _save_json_file(_SAVED_FILE, _saved_list)
    return {"ok": True}


@app.patch("/saved-list/{index}")
async def update_saved_notes(index: int, body: dict):
    if index < 0 or index >= len(_saved_list):
        return JSONResponse(status_code=404, content={"ok": False})
    if "notes" in body:
        _saved_list[index]["notes"] = body["notes"]
    _save_json_file(_SAVED_FILE, _saved_list)
    return {"ok": True}


@app.post("/add-store")
async def add_store_endpoint(req: AddStoreRequest):
    assert _context is not None
    try:
        result = await add_store(req.url, _context, custom_name=req.name)
        return {"ok": True, **result}
    except GeneratorError as e:
        return JSONResponse(
            status_code=422,
            content={"ok": False, "error": str(e), "stage": e.stage},
        )
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": str(e), "stage": "unknown"},
        )
