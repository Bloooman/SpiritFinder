# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

SpiritFinder is a local web app that searches multiple liquor store websites for a bottle by name or distillery, then returns all results sorted by price (lowest first).

## Running the app

```bash
pip install -r requirements.txt
playwright install chromium   # one-time setup

uvicorn main:app --reload     # dev server at http://localhost:8000
```

## Adding a store scraper

1. `cp scrapers/_template.py scrapers/<store_name>.py`
2. Uncomment and fill in the class — update `name`, `_search_url`, and the CSS selectors
3. Restart the server — the scraper is auto-discovered, no other changes needed

Two patterns exist (see `_template.py`):
- **Search-URL**: store has a search bar — navigate to `search?q={query}`, parse results
- **Catalog**: no search — navigate to a category page, parse all products, filter locally by query

## Architecture

- `scrapers/base.py` — `BottleResult` dataclass and abstract `StoreScraper` base class
- `scrapers/__init__.py` — auto-discovers all scraper classes at import time (skips `base` and `_template`)
- `main.py` — FastAPI app; `POST /search` runs all scrapers in parallel via `asyncio.gather`, one Playwright page per store
- `static/index.html` — single-file vanilla JS frontend; no build step

## Key behaviors

- All stores are searched concurrently; a single scraper failure shows an error row but does not abort the search
- Results are sorted by price ascending; the lowest-price row is highlighted green with a "Best" badge
- The shared Playwright browser instance is created at server startup (`lifespan`) and reused across requests
