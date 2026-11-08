"""Shared fakes for the tests."""
from __future__ import annotations

import asyncio
from dataclasses import replace

from bot.config import Settings
from bot.database import Database
from bot.errors import FetchError
from bot.parsers import ProductInfo
from bot.service import TrackerService

OWNER = 1
START = 1_000_000


def make_settings(**overrides) -> Settings:
    base = Settings(bot_token="x", owner_ids=frozenset({OWNER}), db_path=":memory:", host_min_gap=0)
    return replace(base, **overrides)


class FakeClock:
    def __init__(self, now: float = START) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeFetch:
    """Stands in for the real HTTP fetcher. Set ``prices[url]`` or ``errors[url]``.

    duration  seconds the fake clock moves forward during each fetch (simulates a slow page)
    block     when True every fetch waits until ``gate`` is set (simulates a hanging site)
    """

    def __init__(self, clock: FakeClock | None = None, duration: float = 0.0) -> None:
        self.prices: dict[str, float] = {}
        self.errors: dict[str, FetchError] = {}
        self.calls: list[str] = []
        self.clock = clock
        self.duration = duration
        self.block = False
        self.gate = asyncio.Event()

    async def __call__(self, url: str) -> ProductInfo:
        self.calls.append(url)
        if self.block:
            await self.gate.wait()
        if self.clock is not None and self.duration:
            self.clock.advance(self.duration)
        if url in self.errors:
            raise self.errors[url]
        if url not in self.prices:
            raise FetchError("no price found on that page")
        return ProductInfo(title=f"Item {url[-3:]}", price=self.prices[url], currency="INR", http_status=200)


def make_world(**overrides):
    settings = make_settings(**overrides)
    clock = FakeClock()
    fetch = FakeFetch(clock)
    db = Database(":memory:")
    service = TrackerService(db, settings, fetch, clock)
    return settings, clock, fetch, db, service
