"""HTTP fetching with SSRF protection, size limits, UA rotation and optional proxies."""
from __future__ import annotations

import asyncio
import random
from dataclasses import replace
from urllib.parse import urljoin, urlsplit

import aiohttp

from .config import Settings
from .errors import FetchError
from .parsers import ProductInfo, extract_product, looks_blocked
from .urlsafety import assert_public_host, normalize_url

MAX_BYTES = 2_000_000
MAX_REDIRECTS = 4

_USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36",
)


class Fetcher:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self._settings.request_timeout)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def get_html(self, url: str) -> tuple[int, str]:
        """Download a page. Returns (http_status, html)."""
        session = await self._get_session()
        proxy = random.choice(self._settings.proxy_urls) if self._settings.proxy_urls else None
        headers = {
            "User-Agent": random.choice(_USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-IN,en;q=0.9",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Cache-Control": "max-age=0",
        }
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            host = urlsplit(current).hostname or ""
            await assert_public_host(host)  # re-checked on every redirect hop
            try:
                async with session.get(current, headers=headers, proxy=proxy, allow_redirects=False) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        location = resp.headers.get("Location")
                        if not location:
                            raise FetchError("redirect without location")
                        current = normalize_url(urljoin(current, location))
                        continue
                    status = resp.status
                    if status in (404, 410):
                        raise FetchError(f"HTTP {status} page not found", permanent=True, status=status)
                    if status in (403, 429):
                        # The store said no. We report it and back off; we never retry with other
                        # identities or try to get past the protection.
                        raise FetchError(
                            f"HTTP {status} blocked by the store",
                            status=status,
                            blocked=True,
                            retry_after=_retry_after(resp.headers.get("Retry-After")),
                        )
                    if status != 200:
                        raise FetchError(
                            f"HTTP {status}", status=status, retry_after=_retry_after(resp.headers.get("Retry-After"))
                        )
                    body = bytearray()
                    async for chunk in resp.content.iter_chunked(65536):
                        body.extend(chunk)
                        if len(body) > MAX_BYTES:
                            break
                    return status, bytes(body).decode(resp.charset or "utf-8", errors="replace")
            except asyncio.TimeoutError:
                raise FetchError(f"timeout after {self._settings.request_timeout}s") from None
            except aiohttp.ClientError as exc:
                raise FetchError(f"network error: {type(exc).__name__}") from None
        raise FetchError("too many redirects")

    async def fetch_product(self, url: str) -> ProductInfo:
        status, html = await self.get_html(url)
        info = await asyncio.to_thread(extract_product, html, url)
        if info is None:
            if looks_blocked(html):
                raise FetchError("the store showed a bot-check (captcha) page", status=status, blocked=True)
            raise FetchError("no price found on that page", status=status)
        return replace(info, http_status=status)


def _retry_after(value: str | None) -> float | None:
    """Parse a numeric Retry-After header (seconds); ignore HTTP-date forms."""
    if value and value.strip().isdigit():
        return min(float(value), 6 * 3600)
    return None
