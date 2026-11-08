"""Business rules: plans, limits and the "user pastes a link" flow.

Knows nothing about Telegram or HTTP, so it can be tested with a fake fetcher.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Awaitable, Callable
from urllib.parse import urlsplit

from .config import Settings
from .database import Database
from .errors import FetchError, UnsafeURL
from .parsers import ProductInfo
from .urlsafety import normalize_url

log = logging.getLogger(__name__)

FetchProduct = Callable[[str], Awaitable[ProductInfo]]

PLAN_OWNER = "owner"
PLAN_PREMIUM = "premium"
PLAN_FREE = "free"
PLAN_PRO = "pro"
PLAN_ULTRA = "ultra"

ATTEMPTS_PER_MINUTE = 6  # how many links a non-owner may submit per minute


@dataclass
class AddResult:
    status: str  # added | resumed | exists | limit | invalid | fetch_failed | rate_limited
    url: str | None = None
    title: str | None = None
    price: float | None = None
    currency: str = "INR"
    used: int = 0
    limit: int | None = None
    plan: str = PLAN_FREE
    error: str | None = None
    image_url: str | None = None
    domain: str | None = None
    track_id: int | None = None


class TrackerService:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        fetch: FetchProduct,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db = db
        self.settings = settings
        self._fetch = fetch
        self._clock = clock
        self._attempts: dict[int, deque[float]] = {}

    # ---- plans -----------------------------------------------------------
    def now(self) -> int:
        return int(self._clock())

    def is_owner(self, user_id: int) -> bool:
        return user_id in self.settings.owner_ids

    def plan_of(self, user_id: int, premium_until: int = 0, subscription_plan: str = PLAN_FREE) -> str:
        if self.is_owner(user_id):
            return PLAN_OWNER
        if premium_until <= self.now():
            return PLAN_FREE
        return subscription_plan if subscription_plan in {PLAN_PRO, PLAN_ULTRA, PLAN_PREMIUM} else PLAN_PREMIUM

    def limit_for(self, plan: str) -> int | None:
        if plan == PLAN_OWNER:
            return None
        if plan == PLAN_PRO:
            return self.settings.pro_link_limit
        if plan == PLAN_ULTRA:
            return self.settings.ultra_link_limit
        return self.settings.premium_link_limit if plan == PLAN_PREMIUM else self.settings.free_link_limit

    def interval_for(self, plan: str) -> int:
        return {
            PLAN_OWNER: self.settings.owner_interval,
            PLAN_PREMIUM: self.settings.premium_interval,
            PLAN_PRO: self.settings.pro_interval,
            PLAN_ULTRA: self.settings.ultra_interval,
        }.get(plan, self.settings.free_interval)

    def fastest_interval(self, trackers: list[dict]) -> int:
        """A product is checked as often as its most demanding tracker is entitled to."""
        intervals = [self.interval_for(self.plan_of(t["user_id"], t["premium_until"], t.get("subscription_plan", PLAN_FREE))) for t in trackers]
        return min(intervals, default=self.settings.free_interval)

    async def plan_info(self, user_id: int) -> dict:
        user = await self.db.get_user(user_id)
        until = user["premium_until"] if user else 0
        plan = self.plan_of(user_id, until, user.get("subscription_plan", PLAN_FREE) if user else PLAN_FREE)
        return {
            "plan": plan,
            "premium_until": until,
            "used": await self.db.count_tracks(user_id),
            "limit": self.limit_for(plan),
            "interval": self.interval_for(plan),
            "trial_used": bool(user.get("trial_used", 0)) if user else False,
            "founding_used": bool(user.get("founding_used", 0)) if user else False,
        }

    # ---- user actions ----------------------------------------------------
    async def register(self, user_id: int, username: str | None) -> None:
        await self.db.upsert_user(user_id, username, self.now())

    def _rate_limited(self, user_id: int, *, consume: bool = True) -> bool:
        if self.is_owner(user_id):
            return False
        now = self._clock()
        window = self._attempts.setdefault(user_id, deque())
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= ATTEMPTS_PER_MINUTE:
            return True
        if consume:
            window.append(now)
        return False

    async def add_link(self, user_id: int, username: str | None, raw_url: str) -> AddResult:
        await self.register(user_id, username)
        if self._rate_limited(user_id):
            return AddResult("rate_limited")
        try:
            url = normalize_url(raw_url)
        except UnsafeURL as exc:
            return AddResult("invalid", error=str(exc))

        info = await self.plan_info(user_id)
        plan, limit = info["plan"], info["limit"]
        domain = urlsplit(url).hostname or ""
        base = dict(url=url, domain=domain, limit=limit, plan=plan)
        now = self.now()
        interval = self.interval_for(plan)

        product = await self.db.get_product_by_url(url)
        resumed = False

        if product is None:
            if limit is not None and info["used"] >= limit:
                return AddResult("limit", used=info["used"], **base)
            fetched, status, error = await self._try_fetch(url)
            if fetched is None:
                return AddResult(status, error=error, used=info["used"], **base)
            product_id = await self.db.create_product(
                url,
                domain,
                fetched.title,
                fetched.price,
                fetched.currency,
                now,
                now + interval,
                fetched.image_url,
            )
            title, price, currency, image_url = fetched.title, fetched.price, fetched.currency, fetched.image_url
        else:
            product_id = product["id"]
            title, price, currency, image_url = (
                product["title"],
                product["price"],
                product["currency"],
                product.get("image_url"),
            )
            if not product["active"]:  # paused after repeated failures: try to bring it back
                fetched, status, error = await self._try_fetch(url)
                if fetched is None:
                    return AddResult(status, error=error, used=info["used"], **base)
                await self.db.reactivate_product(
                    product_id, fetched.title, fetched.price, fetched.currency, now, now + interval, fetched.image_url
                )
                title, price, currency, image_url = fetched.title, fetched.price, fetched.currency, fetched.image_url
                resumed = True

        outcome = await self.db.add_track(user_id, product_id, limit, now)
        used = await self.db.count_tracks(user_id)
        track = await self.db.get_track_by_product(user_id, product_id)
        common = dict(
            title=title,
            price=price,
            currency=currency,
            image_url=image_url,
            track_id=track["track_id"] if track else None,
            used=used,
            **base,
        )
        if outcome == "limit":
            return AddResult("limit", **common)
        if outcome == "exists":
            return AddResult("resumed" if resumed else "exists", **common)

        # this tracker may deserve faster checks than the product currently gets (e.g. owner/premium)
        trackers = await self.db.get_trackers(product_id)
        await self.db.set_next_check(product_id, now + self.fastest_interval(trackers))
        return AddResult("resumed" if resumed else "added", **common)

    async def _try_fetch(self, url: str) -> tuple[ProductInfo | None, str, str | None]:
        """Fetch a product page. Returns (info, '', None) or (None, failure status, message)."""
        try:
            return await self._fetch(url), "", None
        except UnsafeURL as exc:
            return None, "invalid", str(exc)
        except FetchError as exc:
            log.warning("Could not read %s: %s", url, exc)
            return None, "fetch_failed", str(exc)

    async def preview_link(self, user_id: int, username: str | None, raw_url: str) -> AddResult:
        """Validate and fetch a product for a confirmation screen without adding it.

        The final ``add_link`` call remains the sole write path, so plan limits
        and duplicate checks are still enforced atomically when a user taps
        "Yes, track it".  A preview is deliberately user-scoped: an existing
        track belongs only to the requesting user even when the product record
        itself is shared by several users.
        """
        await self.register(user_id, username)
        if self._rate_limited(user_id, consume=False):
            return AddResult("rate_limited")
        try:
            url = normalize_url(raw_url)
        except UnsafeURL as exc:
            return AddResult("invalid", error=str(exc))

        info = await self.plan_info(user_id)
        plan, limit = info["plan"], info["limit"]
        domain = urlsplit(url).hostname or ""
        base = dict(url=url, domain=domain, limit=limit, plan=plan, used=info["used"])
        product = await self.db.get_product_by_url(url)

        if product is not None:
            existing = await self.db.get_track_by_product(user_id, int(product["id"]))
            product_result = dict(
                title=product["title"],
                price=product["price"],
                currency=product["currency"],
                image_url=product.get("image_url"),
                track_id=existing["track_id"] if existing else None,
                **base,
            )
            if existing is not None:
                return AddResult("exists", **product_result)
            if limit is not None and info["used"] >= limit:
                return AddResult("limit", **product_result)
            # An inactive product must be fetched again before it can be
            # restored by the final add operation.  For an active shared
            # product, the cached, recently checked record is a safe preview.
            if product["active"]:
                return AddResult("ready", **product_result)
        elif limit is not None and info["used"] >= limit:
            return AddResult("limit", **base)

        fetched, status, error = await self._try_fetch(url)
        if fetched is None:
            return AddResult(status, error=error, **base)
        return AddResult(
            "ready",
            title=fetched.title,
            price=fetched.price,
            currency=fetched.currency,
            image_url=fetched.image_url,
            **base,
        )

    async def list_tracks(self, user_id: int) -> list[dict]:
        return await self.db.list_tracks(user_id)

    async def get_track(self, user_id: int, track_id: int) -> dict | None:
        """Fetch one saved product only if the requesting user owns its track."""
        return await self.db.get_track(user_id, track_id)

    async def refresh_track(self, user_id: int, track_id: int) -> tuple[dict | None, str | None]:
        """Refresh an owned product on demand without creating scheduler jobs."""
        track = await self.get_track(user_id, track_id)
        if track is None:
            return None, "not_found"
        fetched, status, _error = await self._try_fetch(str(track["url"]))
        if fetched is None:
            return None, status
        now = self.now()
        trackers = await self.db.get_trackers(int(track["product_id"]))
        await self.db.record_check(
            int(track["product_id"]), fetched.title, fetched.price, fetched.currency,
            now, now + self.fastest_interval(trackers)
        )
        return await self.track_details(user_id, track_id), None

    async def track_details(self, user_id: int, track_id: int, history_limit: int = 8) -> dict | None:
        """Return a safe product detail payload including only owned history."""
        track = await self.get_track(user_id, track_id)
        if track is None:
            return None
        history = await self.db.price_history(int(track["product_id"]), max(1, history_limit))
        track["history"] = history
        track["previous_price"] = history[1]["price"] if len(history) > 1 else None
        return track

    async def remove_track(self, user_id: int, track_id: int) -> bool:
        return await self.db.remove_track(user_id, track_id)

    async def set_target(self, user_id: int, track_id: int, target: float | None) -> bool:
        return await self.db.set_target(user_id, track_id, target)

    async def claim_target_alert(self, user_id: int, track_id: int) -> bool:
        return await self.db.claim_target_alert(user_id, track_id)

    async def remove_track_number(self, user_id: int, number: int) -> bool:
        return await self.db.remove_track_number(user_id, number)

    async def set_target_number(self, user_id: int, number: int, target: float | None) -> bool:
        return await self.db.set_target_number(user_id, number, target)

    async def grant_premium(self, user_id: int, days: int) -> int:
        return await self.db.grant_premium(user_id, days, self.now())

    async def activate_plan(self, user_id: int, plan: str, days: int, kind: str = "purchase") -> int:
        if plan not in {PLAN_PRO, PLAN_ULTRA}:
            raise ValueError("Unknown subscription plan")
        return await self.db.activate_subscription(user_id, plan, days, self.now(), kind)

    async def offer_info(self, user_id: int) -> dict:
        info = await self.db.offer_info(user_id)
        info["founding_sold"] = await self.db.founding_sales_count()
        info["founding_available"] = (
            self.settings.founding_enabled
            and not bool(info["founding_used"])
            and int(info["founding_sold"]) < self.settings.founding_buyer_limit
        )
        return info

    async def activate_trial(self, user_id: int) -> int | None:
        info = await self.offer_info(user_id)
        if info["trial_used"]:
            return None
        return await self.activate_plan(user_id, PLAN_PRO, self.settings.trial_days, "trial")

    def purchase_spec(self, kind: str) -> tuple[str, int, str] | None:
        if kind == PLAN_PRO:
            return PLAN_PRO, self.settings.pro_stars, "purchase"
        if kind == PLAN_ULTRA:
            return PLAN_ULTRA, self.settings.ultra_stars, "purchase"
        if kind == "founding" and self.settings.founding_enabled:
            return PLAN_PRO, self.settings.founding_pro_stars, "founding"
        return None

    async def record_payment(
        self, user_id: int, charge_id: str, stars: int, plan: str | None = None, kind: str | None = None
    ) -> bool:
        return await self.db.record_payment(user_id, charge_id, stars, self.now(), plan, kind)

    async def stats(self) -> dict[str, int]:
        return await self.db.stats()
