"""
Auto-detects a liquor store's search URL and product card structure,
generates a StoreScraper subclass, saves it, and hot-loads it.
No external API calls — pure heuristic HTML analysis.
"""

import importlib.util
import inspect
import re
import sys
from pathlib import Path
from urllib.parse import urlparse, urljoin

from bs4 import BeautifulSoup, NavigableString, Tag
from playwright.async_api import BrowserContext

import scrapers as scrapers_pkg
from scrapers.base import StoreScraper

SCRAPERS_DIR = Path(__file__).parent / "scrapers"
SENTINEL = "SPIRITFINDERTEST"
TEST_QUERIES = ["whiskey", "bourbon", "vodka"]
PRICE_RE = re.compile(r"\$[\d,]+\.\d{2}")
SIZE_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:ml|mL|ML|[lL](?:\b|itre|iter))")
SKIP_ATTRS = frozenset(["class", "id", "href", "name", "type", "action", "placeholder"])
# Link text that indicates a navigation/label link rather than a product name
PRICE_LABEL_WORDS = {"price", "case", "cart", "wish", "list", "compare", "add", "buy", "shop"}
GET_FALLBACKS = [
    ("kw",       "/websearch_results.html?kw={query}"),
    ("ch-query", "/shop?ch-query={query}"),
    ("q",        "/search?q={query}"),
    ("q",        "/?q={query}"),
    ("search",   "/search?search={query}"),
]

# Known e-commerce platform templates.  Each entry is tried in order; the
# first whose detect() passes AND whose selectors resolve against the live page
# (with ≥1 parseable result) wins.  Falls back to the generic heuristic.
KNOWN_TEMPLATES: list[dict] = [
    {
        "id": "corksy",
        "detect": lambda html, url: "ch-product-top-wrapper" in html or "ch-query" in url,
        "container_candidates": ["div.ch-product-wrapper", "div.item-wrapper", "div.wrapper"],
        "name_candidates": ["div.expanded-name", "div.ch-product-name", "a.ch-product-name"],
        "price_candidates": ["span.ch-single-product-price", "div.price-range", "span.price"],
        "url_sel": "a.ch-product-top-wrapper",
    },
    {
        "id": "woocommerce",
        "detect": lambda html, url: "woocommerce-loop-product__title" in html,
        "container_candidates": ["li.product"],
        "name_candidates": [
            "h2.woocommerce-loop-product__title",
            "h3.woocommerce-loop-product__title",
        ],
        # screen-reader-text is verified working (Norfolk); woocommerce-Price-amount
        # is the canonical class but splits currency symbol across nodes on some themes.
        "price_candidates": [
            "span.screen-reader-text",
            "span.woocommerce-Price-amount",
            "p.price",
        ],
        "url_sel": "a.woocommerce-LoopProduct-link",
    },
    {
        "id": "rebl",
        "detect": lambda html, url: "rebl15" in html or "websearch_results" in url,
        "container_candidates": ["tr"],
        "name_candidates": ["a.rebl15"],
        "price_candidates": ["span.rd14 b", "span.rd14"],
        "url_sel": None,  # name element is the product link
    },
]


# ── helpers ──────────────────────────────────────────────────────────────────

def _strip_html(soup: BeautifulSoup) -> BeautifulSoup:
    for tag in soup.find_all(["script", "style", "svg", "noscript", "iframe"]):
        tag.decompose()
    for tag in soup.find_all(True):
        for attr in list(tag.attrs):
            if attr not in SKIP_ATTRS:
                del tag[attr]
    return soup


def _css_selector(el: Tag) -> str | None:
    if not isinstance(el, Tag):
        return None
    classes = el.get("class") or []
    cls = classes[0] if classes else None
    return f"{el.name}.{cls}" if cls else el.name


def _price_nodes(soup: BeautifulSoup) -> list[Tag]:
    nodes = []
    for string in soup.find_all(string=PRICE_RE):
        parent = string.parent
        if not isinstance(parent, Tag):
            continue
        # Skip price-range filter links (e.g. "$25 - $50")
        if parent.name == "a":
            continue
        ancestor = parent
        while ancestor and ancestor.name == "a":
            ancestor = ancestor.parent
        if ancestor:
            nodes.append(ancestor)
    return nodes


def _is_product_link(link: Tag, base_domain: str) -> bool:
    """True if this link looks like a product page link (not a nav/price label)."""
    href = link.get("href", "")
    if not (href.startswith("/") or base_domain in href):
        return False
    text = link.get_text(strip=True)
    if len(text) < 8:
        return False
    text_lower = text.lower()
    return not any(word in text_lower for word in PRICE_LABEL_WORDS)


def _find_container(price_el: Tag, base_domain: str) -> tuple[Tag, int] | None:
    """
    Walk up from price_el up to depth 15.
    Return (ancestor, depth) for the first ancestor that contains BOTH
    a product-name link (substantial text, not a price label) AND a price.
    """
    el = price_el
    for depth in range(1, 16):
        el = el.parent
        if el is None or el.name in ("html", "body"):
            break
        has_product_link = any(_is_product_link(a, base_domain) for a in el.find_all("a", href=True))
        has_price = bool(PRICE_RE.search(el.get_text()))
        if has_product_link and has_price:
            return el, depth
    return None


def _best_container_selector(price_nodes: list[Tag], base_domain: str) -> str | None:
    """
    Return the CSS selector for the product card container.
    Must appear ≥ 3 times. Prefers elements with a CSS class over generic tags.
    """
    from collections import Counter
    candidates: list[tuple[str, bool]] = []  # (selector, has_class)
    for pn in price_nodes[:30]:
        result = _find_container(pn, base_domain)
        if result:
            container, _ = result
            sel = _css_selector(container)
            if sel:
                has_cls = bool(container.get("class"))
                candidates.append((sel, has_cls))

    counts = Counter(sel for sel, _ in candidates)
    # Must appear ≥ 3 times
    qualified = {sel: count for sel, count in counts.items() if count >= 3}
    if not qualified:
        return None

    has_class = {sel: any(hc for s, hc in candidates if s == sel) for sel in qualified}
    # Prefer: has a class > higher count
    return max(qualified, key=lambda s: (has_class[s], qualified[s]))


def _name_selector(card: Tag) -> str | None:
    """Find the best name element inside a product card."""
    # 1. Prefer elements whose class name strongly suggests a product name
    for el in card.find_all(True):
        classes = " ".join(el.get("class") or [])
        hook = el.get("data-hook", "")
        if re.search(r"product[-_]?name|item[-_]?name|product[-_]?title", classes + hook, re.I):
            text = el.get_text(strip=True)
            if len(text) >= 4:
                return _css_selector(el) or el.name

    # 2. Fall back to heading or link with substantial non-label text
    for tag in ["h1", "h2", "h3", "h4", "a"]:
        for el in card.find_all(tag):
            text = el.get_text(strip=True)
            if len(text) < 8:
                continue
            if any(word in text.lower() for word in PRICE_LABEL_WORDS):
                continue
            return _css_selector(el) or tag
    return None


def _price_selector(card: Tag) -> str | None:
    """Find the element containing the price string inside a card."""
    for string in card.find_all(string=PRICE_RE):
        parent = string.parent
        if not isinstance(parent, Tag) or parent.name == "a":
            continue
        direct_sel = _css_selector(parent)
        if direct_sel and "." in direct_sel:
            return direct_sel
        # Parent has no class — anchor to the nearest classed ancestor so we
        # don't emit a bare tag like 'b' that matches unrelated bold text.
        ancestor = parent.parent
        for _ in range(4):
            if ancestor is None or ancestor.name in ("body", "html", "[document]"):
                break
            anc_sel = _css_selector(ancestor)
            if anc_sel and "." in anc_sel:
                return f"{anc_sel} {parent.name}"
            ancestor = ancestor.parent
        return direct_sel or parent.name
    return None


def _match_template(
    html: str, search_url: str, soup: BeautifulSoup
) -> tuple[str, str, str, str | None] | None:
    """
    Try each known platform template in order.
    Returns (container_sel, name_sel, price_sel, url_sel) if a template matches
    and produces ≥1 fully-parseable result.  Returns None to signal the caller
    should fall back to the generic heuristic.
    """
    for t in KNOWN_TEMPLATES:
        if not t["detect"](html, search_url):
            continue
        for container_sel in t["container_candidates"]:
            cards = soup.select(container_sel)
            if not cards:
                continue
            name_sel = next(
                (n for n in t["name_candidates"] if cards[0].select_one(n)),
                None,
            )
            if not name_sel:
                continue
            price_sel = next(
                (p for p in t["price_candidates"] if any(c.select_one(p) for c in cards[:5])),
                None,
            )
            if not price_sel:
                continue
            # Require at least one card that yields a real parseable price.
            valid = any(
                (ne := c.select_one(name_sel))
                and ne.get_text(strip=True)
                and (pe := c.select_one(price_sel))
                and PRICE_RE.search(pe.get_text())
                for c in cards[:10]
            )
            if not valid:
                continue
            return container_sel, name_sel, price_sel, t["url_sel"]
    return None


# ── domain → class name ───────────────────────────────────────────────────────

def _domain_to_class(domain: str) -> str:
    parts = re.sub(r"[^a-zA-Z0-9 ]", " ", domain).split()
    return "".join(p.title() for p in parts) + "Scraper"


def _domain_to_slug(domain: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", domain.lower()).strip("_")


def _clean_store_name(name: str) -> str:
    """Strip city/state suffixes like ', Hadley, MA' that platforms append."""
    name = re.sub(r"(,\s+[^,]+,\s+[A-Z]{2})+$", "", name).strip()
    return name


def _extract_store_name(soup: BeautifulSoup, page_title: str) -> str:
    # 1. og:site_name — explicitly set by the site owner for this purpose
    og = soup.find("meta", attrs={"property": "og:site_name"})
    if og:
        name = _clean_store_name((og.get("content") or "").strip())
        if name:
            return name

    # 2. Logo image alt text — usually the brand name
    for img in soup.find_all("img", alt=True):
        src = img.get("src", "").lower()
        cls = " ".join(img.get("class") or []).lower()
        iid = (img.get("id") or "").lower()
        if "logo" in src + cls + iid:
            alt = _clean_store_name(img.get("alt", "").strip())
            if 2 < len(alt) < 80:
                return alt

    # 3. Page title — strip everything after the first |, -, –, —
    title = re.sub(r"\s*[\|\-–—].*$", "", page_title).strip()
    return _clean_store_name(title) or "Unknown Store"


# ── code generation ───────────────────────────────────────────────────────────

def _url_selector(card: Tag, base_domain: str) -> str | None:
    """Find the product-page link inside a card (may differ from the name element)."""
    for link in card.find_all("a", href=True):
        href = link.get("href", "")
        if href.startswith("/") or base_domain in href:
            return _css_selector(link) or "a"
    return None


def _generate_code(
    class_name: str,
    store_name: str,
    base_url: str,
    search_url_template: str,
    container_sel: str,
    name_sel: str,
    price_sel: str,
    url_sel: str | None,
) -> str:
    I = "            "  # 12-space indent for loop body

    url_lines: list[str] = []
    if url_sel:
        url_lines += [
            f"{I}link = card.select_one({url_sel!r})",
            f'{I}url = link.get("href", "") if link else ""',
        ]
    else:
        url_lines += [
            f'{I}url = name_el.get("href", "") if name_el.name == "a" else ""',
        ]
    url_lines += [
        f'{I}if url and not url.startswith("http"):',
        f"{I}    url = self._base + url",
    ]

    lines = [
        "import re",
        "",
        "from bs4 import BeautifulSoup",
        "from playwright.async_api import Page",
        "",
        "from .base import BottleResult, StoreScraper",
        "",
        "",
        f"class {class_name}(StoreScraper):",
        f"    name = {store_name!r}",
        f"    _base = {base_url!r}",
        f"    _search_url = {search_url_template!r}",
        "",
        "    async def search(self, page: Page, query: str) -> list[BottleResult]:",
        '        await page.goto(self._search_url.format(query=query.replace(" ", "+")))',
        '        await page.wait_for_load_state("networkidle")',
        '        soup = BeautifulSoup(await page.content(), "html.parser")',
        "        return self._parse(soup)",
        "",
        "    def _parse(self, soup: BeautifulSoup) -> list[BottleResult]:",
        "        results = []",
        f"        for card in soup.select({container_sel!r}):",
        f"            name_el = card.select_one({name_sel!r})",
        "            if not name_el:",
        "                continue",
        "            name = name_el.get_text(strip=True)",
        "            if not name:",
        "                continue",
        *url_lines,
        f"            price_el = card.select_one({price_sel!r})",
        "            if not price_el:",
        "                continue",
        r'            m = re.search(r"[\d,]+\.?\d*", price_el.get_text())',
        "            if not m:",
        "                continue",
        "            results.append(BottleResult(",
        "                store_name=self.name,",
        "                bottle_name=name,",
        '                price=float(m.group().replace(",", "")),',
        "                url=url,",
        "                in_stock=True,",
        "            ))",
        "        return results",
        "",
    ]
    return "\n".join(lines)


# ── hot-load ──────────────────────────────────────────────────────────────────

def _hot_load(file_path: Path) -> StoreScraper | None:
    module_name = file_path.stem
    full_name = f"scrapers.{module_name}"
    spec = importlib.util.spec_from_file_location(full_name, file_path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    for _, cls in inspect.getmembers(mod, inspect.isclass):
        if issubclass(cls, StoreScraper) and cls is not StoreScraper:
            return cls()
    return None


# ── main entry point ──────────────────────────────────────────────────────────

class GeneratorError(Exception):
    def __init__(self, message: str, stage: str):
        super().__init__(message)
        self.stage = stage


async def add_store(url: str, context: BrowserContext, custom_name: str = "") -> dict:
    """
    Full pipeline: discover → analyze → generate → save → hot-load.
    Returns {"store_name": ..., "results_found": ...} on success.
    Raises GeneratorError on failure.
    """
    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    base_domain = parsed.netloc.replace("www.", "")

    page = await context.new_page()
    try:
        # ── Stage 1: find search URL ──────────────────────────────────────
        await page.goto(url, timeout=30000)
        await page.wait_for_load_state("networkidle")
        page_title = await page.title()
        homepage_soup = BeautifulSoup(await page.content(), "html.parser")

        search_url_template = await _discover_search_url(page, base_url, base_domain)
        if not search_url_template:
            raise GeneratorError("Could not find a search form on this page.", "search_url")

        # ── Stage 2: fetch results sample ─────────────────────────────────
        raw_html, used_query = await _fetch_results(page, search_url_template)
        if not raw_html:
            raise GeneratorError(
                "Searched for whiskey/bourbon/vodka but found no prices on the results page.",
                "results"
            )

        # ── Stage 3: detect selectors ─────────────────────────────────────
        soup = BeautifulSoup(raw_html, "html.parser")

        # Try known platform templates first — they yield more reliable selectors.
        template_result = _match_template(raw_html, search_url_template, soup)
        if template_result:
            container_sel, name_sel, price_sel, url_sel = template_result
        else:
            # Generic heuristic fallback.
            _strip_html(soup)

            price_nodes = _price_nodes(soup)
            if not price_nodes:
                raise GeneratorError("No price strings ($X.XX) found on results page.", "selectors")

            container_sel = _best_container_selector(price_nodes, base_domain)
            if not container_sel:
                raise GeneratorError(
                    "Could not identify repeating product containers (need ≥ 3).", "selectors"
                )

            sample_cards = soup.select(container_sel)
            name_sel = _name_selector(sample_cards[0]) if sample_cards else None
            price_sel = _price_selector(sample_cards[0]) if sample_cards else None

            if not name_sel or not price_sel:
                raise GeneratorError(
                    f"Detected container ({container_sel}) but couldn't find name or price inside it.",
                    "selectors"
                )

            first_card = sample_cards[0]
            name_el_sample = first_card.select_one(name_sel)
            url_sel = None
            if name_el_sample and name_el_sample.name != "a":
                url_sel = _url_selector(first_card, base_domain)

        # ── Stage 4: generate + save ──────────────────────────────────────
        store_name = custom_name.strip() if custom_name.strip() else _extract_store_name(homepage_soup, page_title)
        class_name = _domain_to_class(base_domain)
        slug = _domain_to_slug(base_domain)

        code = _generate_code(
            class_name=class_name,
            store_name=store_name,
            base_url=base_url,
            search_url_template=search_url_template,
            container_sel=container_sel,
            name_sel=name_sel,
            price_sel=price_sel,
            url_sel=url_sel,
        )

        file_path = SCRAPERS_DIR / f"{slug}.py"
        file_path.write_text(code)

        # ── Stage 5: hot-load ─────────────────────────────────────────────
        scraper = _hot_load(file_path)
        if scraper is None:
            raise GeneratorError("Generated code did not produce a valid scraper class.", "load")

        # Verify it returns results
        test_page = await context.new_page()
        try:
            results = await scraper.search(test_page, used_query)
        finally:
            await test_page.close()

        # Register in the live scrapers list
        scrapers_pkg.scrapers.append(scraper)

        return {"store_name": scraper.name, "results_found": len(results)}

    finally:
        await page.close()


# ── internal helpers ──────────────────────────────────────────────────────────

async def _discover_search_url(page, base_url: str, base_domain: str) -> str | None:
    SEARCH_KEYWORDS = ("search", "q", "kw", "keyword", "find", "item")

    # Try submitting the search form
    inputs = await page.query_selector_all("input")
    for inp in inputs:
        type_ = (await inp.get_attribute("type") or "").lower()
        name = (await inp.get_attribute("name") or "").lower()
        placeholder = (await inp.get_attribute("placeholder") or "").lower()
        if type_ in ("hidden", "submit", "checkbox", "radio", "password"):
            continue
        if any(kw in name or kw in placeholder or type_ == "search"
               for kw in SEARCH_KEYWORDS):
            try:
                await inp.fill(SENTINEL)
                await inp.press("Enter")
                await page.wait_for_load_state("networkidle", timeout=10000)
                current_url = page.url
                if SENTINEL in current_url:
                    return current_url.replace(SENTINEL, "{query}")
            except Exception:
                pass
            # Reload and try next input
            await page.goto(page.url.split("?")[0], timeout=15000)
            await page.wait_for_load_state("networkidle")

    # Fallback: try common GET patterns
    for param, pattern in GET_FALLBACKS:
        test_url = base_url + pattern.format(query="whiskey")
        try:
            resp = await page.goto(test_url, timeout=15000)
            if resp and resp.status == 200:
                content = await page.content()
                if PRICE_RE.search(content):
                    return base_url + pattern
        except Exception:
            pass

    return None


async def _fetch_results(page, search_url_template: str) -> tuple[str | None, str]:
    for query in TEST_QUERIES:
        url = search_url_template.format(query=query.replace(" ", "+"))
        try:
            await page.goto(url, timeout=20000)
            await page.wait_for_load_state("networkidle")
            content = await page.content()
            if PRICE_RE.search(content):
                return content, query
        except Exception:
            pass
    return None, ""
