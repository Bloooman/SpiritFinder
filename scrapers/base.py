from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from playwright.async_api import Page


@dataclass
class BottleResult:
    store_name: str
    bottle_name: str
    price: float
    url: str
    in_stock: bool
    size: Optional[str] = None


class StoreScraper(ABC):
    name: str

    @abstractmethod
    async def search(self, page: Page, query: str) -> list[BottleResult]:
        ...
