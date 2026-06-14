"""
Quick diagnostic for a single store scraper.
Usage (server does NOT need to be running):

    python debug_scraper.py "el dorado 12"

It opens a visible browser window so you can see what the page actually shows,
then prints what the Burlington scraper finds (or doesn't find).
"""
import asyncio
import re
import sys

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

SEARCH_URL = "https://www.burlingtonwineandspirits.com/websearch_results.html?kw={query}"
BASE = "https://www.burlingtonwineandspirits.com"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


async def main(query: str):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)  # visible window
        ctx = await browser.new_context(
            user_agent=UA,
            viewport={"width": 1280, "height": 800},
            extra_http_headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        page = await ctx.new_page()
        url = SEARCH_URL.format(query=query.replace(" ", "+"))
        print(f"\nNavigating to:\n  {url}\n")
        await page.goto(url)
        await page.wait_for_load_state("networkidle")

        # Wait up to 5 s for results to appear
        try:
            await page.wait_for_selector("a.rebl15", timeout=5000)
            print("✓  Found a.rebl15 — results rendered in DOM")
        except Exception:
            print("✗  a.rebl15 never appeared after 5 s")

        html = await page.content()
        soup = BeautifulSoup(html, "html.parser")

        print(f"\nPage title: {soup.title.get_text(strip=True) if soup.title else '(none)'}")

        # Report what selectors actually exist
        for sel in ["a.rebl15", "table.prow", "tr", ".product", ".item", "h2", "h3"]:
            count = len(soup.select(sel))
            if count:
                print(f"  {sel:25s}  {count} element(s)")

        # Try the current scraper logic
        results = []
        for card in soup.select("tr"):
            name_el = card.select_one("a.rebl15")
            if not name_el:
                continue
            name = name_el.get_text(strip=True)
            url_href = name_el.get("href", "")
            if url_href and not url_href.startswith("http"):
                url_href = BASE + url_href
            price_el = card.select_one("b")
            price_text = price_el.get_text(strip=True) if price_el else ""
            m = re.search(r"[\d,]+\.?\d*", price_text)
            price = float(m.group().replace(",", "")) if m else None
            results.append({"name": name, "price": price, "url": url_href})

        print(f"\nScraper found {len(results)} result(s) for '{query}':")
        for r in results[:10]:
            print(f"  ${r['price']:.2f}  {r['name']}")

        if not results:
            # Show a snippet of the raw HTML to spot bot-detection pages
            body_text = soup.get_text(separator=" ", strip=True)
            print("\nFirst 500 chars of page text:")
            print(" ", body_text[:500])

        input("\nPress Enter to close the browser...")
        await browser.close()


if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "el dorado 12"
    asyncio.run(main(q))
