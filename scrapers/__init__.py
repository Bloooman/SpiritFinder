import importlib
import inspect
import pkgutil
from pathlib import Path

from .base import StoreScraper

_SKIP = {"base", "_template"}

scrapers: list[StoreScraper] = []

for _info in pkgutil.iter_modules([str(Path(__file__).parent)]):
    if _info.name in _SKIP:
        continue
    _mod = importlib.import_module(f".{_info.name}", package=__name__)
    for _name, _cls in inspect.getmembers(_mod, inspect.isclass):
        if issubclass(_cls, StoreScraper) and _cls is not StoreScraper:
            scrapers.append(_cls())
