"""Turn a plain product link into an affiliate link (this is where the money is)."""
from __future__ import annotations

from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from .config import Settings


def _with_param(url: str, key: str, value: str) -> str:
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != key]
    query.append((key, value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def affiliate_url(url: str, settings: Settings) -> str:
    host = (urlsplit(url).hostname or "").lower()
    if "amazon." in host and settings.amazon_tag:
        return _with_param(url, "tag", settings.amazon_tag)
    if host.endswith("flipkart.com") and settings.flipkart_affid:
        return _with_param(url, "affid", settings.flipkart_affid)
    if settings.affiliate_template and "{url}" in settings.affiliate_template:
        return settings.affiliate_template.replace("{url}", quote(url, safe=""))
    return url
