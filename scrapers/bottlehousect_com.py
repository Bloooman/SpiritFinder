import re

from bs4 import BeautifulSoup
from playwright.async_api import Page

from .base import BottleResult, StoreScraper


class BottlehousectComScraper(StoreScraper):
    name = 'Bottle House CT'
    _base = 'http://www.bottlehousect.com'
    _search_url = 'https://bottlehousect.com/shop?ch-query={query}'

    async def search(self, page: Page, query: str) -> list[BottleResult]:
        await page.goto(self._search_url.format(query=query.replace(" ", "+")))
        await page.wait_for_load_state("networkidle")
        soup = BeautifulSoup(await page.content(), "html.parser")
        return self._parse(soup)

    def _parse(self, soup: BeautifulSoup) -> list[BottleResult]:
        results = []
        for card in soup.select('div.ch-product-wrapper'):
            name_el = card.select_one('div.expanded-name')
            if not name_el:
                continue
            name = name_el.get_text(strip=True)
            if not name:
                continue
            link = card.select_one('a.ch-product-top-wrapper')
            url = link.get("href", "") if link else ""
            if url and not url.startswith("http"):
                url = self._base + url
            price_el = card.select_one('span.ch-single-product-price')
            if not price_el:
                continue
            m = re.search(r"[\d,]+\.?\d*", price_el.get_text())
            if not m:
                continue
            results.append(BottleResult(
                store_name=self.name,
                bottle_name=name,
                price=float(m.group().replace(",", "")),
                url=url,
                in_stock=True,
            ))
        return results
