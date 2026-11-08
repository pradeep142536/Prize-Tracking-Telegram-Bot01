"""URL validation, normalisation and SSRF protection.

Users give us arbitrary links and our server fetches them, so we must never let
someone point the bot at localhost, a private network or a cloud metadata IP.
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .errors import FetchError, UnsafeURL

MAX_URL_LENGTH = 2000

_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_AMAZON_HOST = re.compile(r"(^|\.)amazon\.[a-z]{2,3}(\.[a-z]{2})?$")
_ASIN_PATH = re.compile(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})(?:[/?]|$)", re.IGNORECASE)
_TRACKING_PARAMS = frozenset(
    {"fbclid", "gclid", "igshid", "mc_cid", "mc_eid", "ref", "ref_", "tag", "affid", "_encoding", "linkcode"}
)
_TRACKING_PREFIXES = ("utm_", "pd_rd_", "pf_rd_")
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home", ".corp", ".intranet")


def extract_urls(text: str) -> list[str]:
    """Return links found in free text, in order, without duplicates."""
    seen: list[str] = []
    for match in _URL_IN_TEXT.findall(text or ""):
        cleaned = match.rstrip(".,;)!?")
        if cleaned not in seen:
            seen.append(cleaned)
    return seen


def _check_literal_host(host: str) -> None:
    if not host or "." not in host or host == "localhost" or host.endswith(_BLOCKED_SUFFIXES):
        raise UnsafeURL("That doesn't look like a public website link.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return  # a normal hostname
    raise UnsafeURL("Please send a normal product link, not an IP address.")


def normalize_url(raw: str) -> str:
    """Validate ``raw`` and return a canonical https URL (tracking params removed)."""
    url = (raw or "").strip().strip("<>").rstrip(".,;)!?\"'")
    if not url:
        raise UnsafeURL("That link is empty.")
    if len(url) > MAX_URL_LENGTH:
        raise UnsafeURL("That link is too long.")
    if "://" not in url:
        url = "https://" + url

    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise UnsafeURL("That doesn't look like a valid link.") from None

    if parts.scheme not in ("http", "https"):
        raise UnsafeURL("Only http/https links are supported.")
    if parts.username or parts.password:
        raise UnsafeURL("Links with a username or password aren't supported.")
    if port not in (None, 80, 443):
        raise UnsafeURL("Links with a custom port aren't supported.")

    host = (parts.hostname or "").lower().rstrip(".")
    _check_literal_host(host)

    path = parts.path or "/"
    query = ""
    if _AMAZON_HOST.search(host):
        asin = _ASIN_PATH.search(parts.path)
        if asin:
            path = f"/dp/{asin.group(1).upper()}"
    elif host.endswith("flipkart.com"):
        pid = [(k, v) for k, v in parse_qsl(parts.query) if k == "pid"]
        query = urlencode(pid)
    else:
        kept = [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in _TRACKING_PARAMS and not k.lower().startswith(_TRACKING_PREFIXES)
        ]
        query = urlencode(kept)

    return urlunsplit(("https", host, path, query, ""))


async def assert_public_host(host: str, port: int = 443) -> None:
    """Resolve ``host`` and refuse if any address is not a public internet address."""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise FetchError("DNS lookup failed") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.version == 6 and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if not ip.is_global:
            raise UnsafeURL("That address points to a private network.")
