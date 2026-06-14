"""
Template for adding a new store scraper.

Copy this file to scrapers/<store_name>.py and fill in the TODOs.
The scraper is auto-discovered on the next server start — no other changes needed.

Two common patterns:

PATTERN 1 — Store has a search bar (most common):
  Navigate to the search URL, wait for results, parse the HTML.

PATTERN 2 — Store only has category/catalog pages:
  Navigate to a category page, parse all products, filter by query locally.
"""

from bs4 import BeautifulSoup
from playwright.async_api import Page

from .base import BottleResult, StoreScraper


# class MyStoreScraper(StoreScraper):
#     name = "My Store Name"
#
#     # PATTERN 1: search URL — replace with the actual search URL
#     _search_url = "https://example.com/search?q={query}"
#
#     async def search(self, page: Page, query: str) -> list[BottleResult]:
#         await page.goto(self._search_url.format(query=query))
#         await page.wait_for_load_state("networkidle")
#         soup = BeautifulSoup(await page.content(), "html.parser")
#
#         results = []
#         for card in soup.select(".product-card"):  # TODO: update selector
#             name = card.select_one(".product-name").get_text(strip=True)
#             price_text = card.select_one(".price").get_text(strip=True)
#             price = float(price_text.replace("$", "").replace(",", ""))
#             url = card.select_one("a")["href"]
#             if not url.startswith("http"):
#                 url = "https://example.com" + url
#             in_stock = card.select_one(".out-of-stock") is None
#             results.append(BottleResult(
#                 store_name=self.name,
#                 bottle_name=name,
#                 price=price,
#                 url=url,
#                 in_stock=in_stock,
#             ))
#         return results
