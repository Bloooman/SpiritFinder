import re

from bs4 import BeautifulSoup
from playwright.async_api import Page

from .base import BottleResult, StoreScraper


class EmpirewineComScraper(StoreScraper):
    name = 'Empire Wine & Liquor, NY'
    _base = 'https://www.empirewine.com'
    _search_url = 'https://www.empirewine.com/search/?q={query}'

    async def search(self, page: Page, query: str) -> list[BottleResult]:
        await page.goto(self._search_url.format(query=query.replace(" ", "+")))
        await page.wait_for_load_state("networkidle")
        soup = BeautifulSoup(await page.content(), "html.parser")
        return self._parse(soup)

    def _parse(self, soup: BeautifulSoup) -> list[BottleResult]:
        results = []
        seen_urls: set[str] = set()
        for card in soup.select('div.border-b-gray-200'):
            prev = card.find_previous_sibling()
            if not prev:
                continue
            name_el = prev.find("a", href=True)
            if not name_el:
                continue
            name = name_el.get_text(strip=True)
            if not name:
                continue
            url = name_el.get("href", "")
            if url and not url.startswith("http"):
                url = self._base + url
            if url in seen_urls:
                continue
            seen_urls.add(url)
            price_el = None
            for _s in card.find_all(string=re.compile(r'\$[\d,]+\.\d{2}')):
                _p = _s.parent
                if _p and 'line-through' not in ' '.join(_p.get('class') or []):
                    price_el = _p
                    break
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
