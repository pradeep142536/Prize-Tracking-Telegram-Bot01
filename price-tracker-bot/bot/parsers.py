"""Extract product title + price from raw HTML. Pure functions, no network."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup


@dataclass(frozen=True)
class ProductInfo:
    title: str
    price: float
    currency: str = "INR"
    http_status: int | None = None  # filled in by the fetcher, only used for logging
    # A best-effort product image.  It is optional because many stores omit one
    # or serve it only after JavaScript has run.
    image_url: str | None = None


_SYMBOLS = {"₹": "INR", "rs": "INR", "rs.": "INR", "inr": "INR", "$": "USD", "usd": "USD",
            "€": "EUR", "eur": "EUR", "£": "GBP", "gbp": "GBP"}
_PRICE_WITH_SYMBOL = re.compile(r"(₹|Rs\.?|INR|\$|€|£)\s*([\d][\d.,]*)", re.IGNORECASE)
_STALE_HINTS = ("mrp", "strike", "old", "was", "list", "original", "save", "discount", "emi", "off")
_AMAZON_SELECTORS = (
    "#corePrice_feature_div span.a-price span.a-offscreen",
    "#corePriceDisplay_desktop_feature_div span.a-price span.a-offscreen",
    "span.priceToPay span.a-offscreen",
    "#priceblock_dealprice",
    "#priceblock_ourprice",
    "span.a-price span.a-offscreen",
)


def parse_price(value: str | float | int | None) -> float | None:
    """Turn '₹1,29,999.00', '1.299,50', '$12.99' or 499 into a positive float."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None

    s = re.sub(r"[^\d.,]", "", str(value)).strip(".,")
    if not s:
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):  # 1.299,50
            s = s.replace(".", "").replace(",", ".")
        else:  # 1,299.50 / 1,29,999.00
            s = s.replace(",", "")
    elif "," in s:
        if re.fullmatch(r"\d{1,3}(,\d{3})+", s) or re.fullmatch(r"\d{1,2}(,\d{2})+,\d{3}", s):
            s = s.replace(",", "")  # thousands separators (western or Indian)
        elif re.fullmatch(r"\d+,\d{1,2}", s):
            s = s.replace(",", ".")  # decimal comma
        else:
            s = s.replace(",", "")
    elif s.count(".") > 1:
        s = s.replace(".", "")
    try:
        number = float(s)
    except ValueError:
        return None
    return number if number > 0 else None


def _clean(text: str | None, limit: int = 200) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:limit]


def _guess_currency(url: str, symbol: str | None = None) -> str:
    if symbol and symbol.lower() in _SYMBOLS:
        return _SYMBOLS[symbol.lower()]
    host = urlsplit(url).hostname or ""
    if host.endswith(".in") or "flipkart" in host or "myntra" in host or "ajio" in host:
        return "INR"
    if host.endswith((".co.uk", ".uk")):
        return "GBP"
    if host.endswith((".de", ".fr", ".it", ".es")):
        return "EUR"
    if host.endswith(".com") and "amazon" in host:
        return "USD"
    return "INR"


def _walk(obj, depth: int = 0):
    if depth > 6:
        return
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            if isinstance(v, (dict, list)):
                yield from _walk(v, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk(item, depth + 1)


def _is_product(node: dict) -> bool:
    kind = node.get("@type")
    kinds = kind if isinstance(kind, list) else [kind]
    return any(str(k).lower() == "product" for k in kinds)


def _offer_prices(offers) -> list[tuple[float, str | None]]:
    found: list[tuple[float, str | None]] = []
    for node in _walk(offers):
        for key in ("price", "lowPrice"):
            price = parse_price(node.get(key))
            if price is None and isinstance(node.get("priceSpecification"), dict):
                price = parse_price(node["priceSpecification"].get(key))
            if price is not None:
                found.append((price, node.get("priceCurrency")))
                break
    return found


def _image_url(value, page_url: str) -> str | None:
    """Return a usable absolute HTTP(S) image URL from common structured-data shapes."""
    if isinstance(value, (list, tuple)):
        for item in value:
            found = _image_url(item, page_url)
            if found:
                return found
        return None
    if isinstance(value, dict):
        value = value.get("url") or value.get("contentUrl") or value.get("@id")
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = urljoin(page_url, value.strip())
    parts = urlsplit(candidate)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        return None
    return candidate


def _page_image(soup: BeautifulSoup, page_url: str) -> str | None:
    for attrs in (
        {"property": "og:image"},
        {"name": "og:image"},
        {"name": "twitter:image"},
        {"property": "twitter:image"},
    ):
        tag = soup.find("meta", attrs=attrs)
        if tag:
            image = _image_url(tag.get("content"), page_url)
            if image:
                return image
    return None


def _from_json_ld(soup: BeautifulSoup, url: str) -> ProductInfo | None:
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except (ValueError, TypeError):
            continue
        for node in _walk(data):
            if not _is_product(node) or "offers" not in node:
                continue
            prices = _offer_prices(node["offers"])
            if not prices:
                continue
            price, currency = min(prices, key=lambda p: p[0])  # cheapest seller
            title = _clean(node.get("name")) or _page_title(soup)
            return ProductInfo(
                title,
                price,
                (currency or _guess_currency(url)).upper(),
                image_url=_image_url(node.get("image") or node.get("thumbnailUrl"), url) or _page_image(soup, url),
            )
    return None


def _from_meta(soup: BeautifulSoup, url: str) -> ProductInfo | None:
    for prop in ("product:price:amount", "og:price:amount"):
        tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        price = parse_price(tag.get("content")) if tag else None
        if price:
            cur = soup.find("meta", attrs={"property": prop.replace("amount", "currency")})
            currency = (cur.get("content") if cur else None) or _guess_currency(url)
            return ProductInfo(_page_title(soup), price, currency.upper(), image_url=_page_image(soup, url))
    tag = soup.find(attrs={"itemprop": "price"})
    if tag:
        price = parse_price(tag.get("content") or tag.get_text())
        if price:
            cur = soup.find(attrs={"itemprop": "priceCurrency"})
            currency = (cur.get("content") or cur.get_text()) if cur else None
            return ProductInfo(
                _page_title(soup), price, (currency or _guess_currency(url)).upper(), image_url=_page_image(soup, url)
            )
    return None


def _from_amazon(soup: BeautifulSoup, url: str) -> ProductInfo | None:
    if "amazon." not in (urlsplit(url).hostname or ""):
        return None
    for selector in _AMAZON_SELECTORS:
        el = soup.select_one(selector)
        if not el:
            continue
        text = el.get_text(" ", strip=True)
        price = parse_price(text)
        if price:
            symbol = re.match(r"\s*(₹|\$|€|£)", text)
            return ProductInfo(
                _page_title(soup),
                price,
                _guess_currency(url, symbol.group(1) if symbol else None),
                image_url=_page_image(soup, url),
            )
    return None


def _from_generic(soup: BeautifulSoup, url: str) -> ProductInfo | None:
    for el in soup.select('[class*="price" i], [id*="price" i]')[:40]:
        hints = " ".join(el.get("class", [])) + " " + (el.get("id") or "")
        if any(h in hints.lower() for h in _STALE_HINTS):
            continue
        text = el.get_text(" ", strip=True)
        if not text or len(text) > 40:
            continue
        match = _PRICE_WITH_SYMBOL.search(text)
        price = parse_price(match.group(2)) if match else None
        if price:
            return ProductInfo(
                _page_title(soup), price, _guess_currency(url, match.group(1)), image_url=_page_image(soup, url)
            )
    return None


def _page_title(soup: BeautifulSoup) -> str:
    for finder in (
        lambda: soup.select_one("#productTitle"),
        lambda: soup.find("meta", attrs={"property": "og:title"}),
        lambda: soup.find("title"),
    ):
        el = finder()
        if el:
            text = _clean(el.get("content") if el.name == "meta" else el.get_text())
            if text:
                return text
    return "Unknown product"


_BLOCK_MARKERS = (
    "validatecaptcha",
    "enter the characters you see below",
    "type the characters you see in this image",
    "robot check",
    "to discuss automated access to amazon data",
    "access denied",
    "are you a human",
)


def looks_blocked(html: str) -> bool:
    """True when the page is a captcha / bot-check page instead of a product page."""
    head = (html or "")[:60000].lower()
    return any(marker in head for marker in _BLOCK_MARKERS)


def extract_product(html: str, url: str = "") -> ProductInfo | None:
    """Best-effort product extraction. Returns None when no price is found."""
    if not html:
        return None
    soup = BeautifulSoup(html, "lxml")
    for strategy in (_from_json_ld, _from_amazon, _from_meta, _from_generic):
        info = strategy(soup, url)
        if info:
            return info
    return None
