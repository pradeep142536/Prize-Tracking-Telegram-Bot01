"""Background price monitor.

Scheduling model
----------------
* Every product has one ``next_check`` time. The loop claims the products that are due, so a
  product is fetched once per cycle no matter how many users track it.
* The interval of a product is the fastest one any of its trackers is entitled to
  (``TrackerService.fastest_interval``): owner 15s, premium 300s, free 1800s by default.
* The next check is anchored to the *scheduled* time, not to when the fetch finished, so a
  slow page does not push every later check back (no drift). If the monitor is far behind
  (restart, outage) it re-anchors instead of firing a burst of catch-up checks.
* A product is never fetched twice at once: the database claim has a lease and the monitor
  also keeps an in-memory set of products that are in flight.
* A store that answers 403/429 or shows a captcha is treated as BLOCKED. We do not retry with
  other identities; every product of that store is left alone for a cool-down that doubles on
  each repeat.

Every real attempt is logged (logger "pricecheck"), stored in ``check_log`` and counted in
``MonitorStats``.
"""
from __future__ import annotations

import asyncio
import html
import logging
import time
from typing import Awaitable, Callable
from urllib.parse import urlsplit

from .affiliate import affiliate_url
from .alerts import format_drop_message, format_target_message, should_alert
from .checklog import (
    CheckRecord,
    MonitorStats,
    describe_change,
    format_check,
    format_products,
    format_summary,
    safe_url,
    startup_banner,
    store_name,
)
from .config import Settings
from .database import Database
from .errors import FetchError, UnsafeURL
from .service import FetchProduct, TrackerService

log = logging.getLogger(__name__)
check_log = logging.getLogger("pricecheck")

Notify = Callable[[int, str, str | None], Awaitable[None]]

IDLE_TICK = 1.0  # longest the loop sleeps when nothing is due
MIN_TICK = 0.05  # shortest sleep (checks start within ~50 ms of their schedule)
MAX_INFLIGHT = 100
CLAIM_LEASE = 600  # seconds a claimed product is protected from being claimed again
MIN_PAUSE = 1.0  # never re-check a product sooner than this after its previous check finished
MAX_FAILURES = 8
MAX_BACKOFF = 6 * 3600
BLOCK_COOLDOWN = 300  # first cool-down after a block; doubles on each repeat
PRUNE_EVERY = 3600  # seconds between check_log clean-ups


class PriceMonitor:
    def __init__(
        self,
        db: Database,
        service: TrackerService,
        fetch: FetchProduct,
        notify: Notify,
        settings: Settings,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._db = db
        self._service = service
        self._fetch = fetch
        self._notify = notify
        self._settings = settings
        self._clock = clock
        self._sem = asyncio.Semaphore(settings.max_concurrency)
        self._stop = asyncio.Event()
        self._tasks: set[asyncio.Task] = set()
        self._inflight: set[int] = set()
        self._last_start: dict[int, float] = {}
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._host_last: dict[str, float] = {}
        self._host_cooldown: dict[str, float] = {}
        # Longest a single check may take (covers every redirect hop); a safety net on top of
        # the per-request timeout inside the fetcher.
        self._hard_timeout = settings.request_timeout * 6 + 5
        self.stats = MonitorStats(started_at=clock())

    # ---- lifecycle -------------------------------------------------------
    async def run(self) -> None:
        for line in startup_banner(self._settings):
            check_log.info(line)
        last_summary = last_prune = time.monotonic()
        while not self._stop.is_set():
            try:
                await self.tick()
                now_m = time.monotonic()
                if now_m - last_summary >= self._settings.summary_seconds:
                    last_summary = now_m
                    check_log.info("\n".join(await self._summary_lines()))
                if now_m - last_prune >= PRUNE_EVERY:
                    last_prune = now_m
                    await self._prune()
            except Exception:
                log.exception("Monitor tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=await self._sleep_seconds())
            except asyncio.TimeoutError:
                pass
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        check_log.info("Price Monitor stopped")

    def stop(self) -> None:
        self._stop.set()

    async def drain(self) -> None:
        """Wait for in-flight checks (used by tests)."""
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _sleep_seconds(self) -> float:
        """Sleep until the next product is due (at most IDLE_TICK) so checks start on time."""
        try:
            nxt = await self._db.earliest_due()
        except Exception:
            return IDLE_TICK
        if nxt is None:
            return IDLE_TICK
        return min(IDLE_TICK, max(MIN_TICK, nxt["next_check"] - self._clock()))

    # ---- summaries ---------------------------------------------------------
    async def _summary_lines(self) -> list[str]:
        return format_summary(self.stats, self._clock(), await self._db.earliest_due())

    async def summary(self) -> str:
        """Plain-text summary (used by the owner's /monitor command)."""
        lines = await self._summary_lines()
        ids = list(self.stats.products)[:10]
        schedule = await self._db.schedule_of(ids)
        products = format_products(self.stats, schedule, self._inflight, self._clock())
        if products:
            lines += ["", "Products:"] + products
        return "\n".join(lines)

    async def _prune(self) -> None:
        cutoff = self._clock() - self._settings.check_log_hours * 3600
        removed = await self._db.prune_check_log(cutoff)
        if removed:
            log.info("Pruned %d old check-log rows", removed)

    # ---- one scheduling round -------------------------------------------
    async def tick(self) -> None:
        capacity = MAX_INFLIGHT - len(self._tasks)
        if capacity <= 0:
            return
        due = await self._db.claim_due(self._clock(), capacity, CLAIM_LEASE)
        for product in due:
            if product["id"] in self._inflight:
                continue  # its lease ran out while a slow check is still running: never overlap
            self._inflight.add(product["id"])
            task = asyncio.create_task(self._check(product))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _host_gate(self, host: str) -> None:
        """Keep a polite minimum gap between requests to the same website."""
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        async with lock:
            wait = self._host_last.get(host, 0.0) + self._settings.host_min_gap - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._host_last[host] = time.monotonic()

    # ---- one check -------------------------------------------------------
    async def _check(self, product: dict) -> None:
        try:
            await self._run_check(product)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Check crashed for product %s", product["id"])
        finally:
            self._inflight.discard(product["id"])

    async def _run_check(self, product: dict) -> None:
        pid = product["id"]
        url = product["url"]
        host = product["domain"] or urlsplit(url).hostname or ""
        store = store_name(host)
        scheduled = float(product["next_check"])  # the row as it was before it was claimed

        cooldown_until = self._host_cooldown.get(host, 0.0)
        if cooldown_until > self._clock():
            await self._defer(product, store, cooldown_until)
            return

        queued_at = self._clock()
        await self._host_gate(host)
        async with self._sem:
            started = self._clock()
            error: FetchError | None = None
            info = None
            try:
                info = await asyncio.wait_for(self._fetch(url), timeout=self._hard_timeout)
            except asyncio.TimeoutError:
                error = FetchError(f"timeout (no answer within {self._hard_timeout:.0f}s)")
            except FetchError as exc:
                error = exc
            except UnsafeURL as exc:  # e.g. the host now resolves to a private address
                error = FetchError(str(exc), permanent=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Unexpected error checking %s", safe_url(url))
                error = FetchError("unexpected error")
            finished = self._clock()

        trackers = await self._db.get_trackers(pid)
        if not trackers:  # everyone stopped tracking while we were fetching
            self._last_start.pop(pid, None)
            return
        interval = self._service.fastest_interval(trackers)
        previous_start = self._last_start.get(pid)
        self._last_start[pid] = started

        rec = CheckRecord(
            product_id=pid,
            store=store,
            url=safe_url(url),
            started_at=started,
            finished_at=finished,
            scheduled_for=scheduled,
            interval=interval,
            since_prev=None if previous_start is None else started - previous_start,
            queue_wait=max(0.0, started - queued_at),
            fetch_seconds=max(0.0, finished - started),
            http_status=None,
            success=error is None,
            outcome="SUCCESS",
            trackers=len(trackers),
        )
        if error is None:
            await self._on_success(product, info, rec, trackers)
        else:
            await self._on_failure(product, error, rec, trackers, host)

    async def _on_success(self, product: dict, info, rec: CheckRecord, trackers: list[dict]) -> None:
        old = product["price"]
        rec.http_status = info.http_status
        rec.price = info.price
        rec.prev_price = old
        rec.currency = info.currency
        rec.outcome, rec.change = describe_change(old, info.price, info.currency)

        next_check = self._next_due(rec.scheduled_for, rec.started_at, rec.finished_at, rec.interval)
        await self._db.record_check(
            product["id"], info.title, info.price, info.currency, rec.finished_at, next_check
        )
        await self._emit(rec)

        link = affiliate_url(product["url"], self._settings)
        for tracker in trackers:
            target = tracker["target_price"]
            if target is not None:
                if info.price <= target:
                    # The row-level claim makes this safe across scheduler
                    # restarts and prevents every polling cycle from alerting.
                    if await self._db.claim_target_alert(tracker["user_id"], tracker["track_id"]):
                        text = format_target_message(info.title, info.price, target, info.currency)
                        await self._safe_notify(tracker["user_id"], text, buy_url=link, track_id=tracker.get("track_id"))
                else:
                    # A later re-entry below target is meaningful again.
                    await self._db.reset_target_alert(tracker["user_id"], tracker["track_id"])
            elif should_alert(old, info.price, None):
                text = format_drop_message(info.title, old, info.price, info.currency)
                await self._safe_notify(tracker["user_id"], text, buy_url=link, track_id=tracker.get("track_id"))

    async def _on_failure(
        self, product: dict, exc: FetchError, rec: CheckRecord, trackers: list[dict], host: str
    ) -> None:
        failures = product["fail_count"] + 1
        blocked = exc.blocked or exc.status in (403, 429)
        deactivate = exc.permanent or failures >= MAX_FAILURES
        interval = rec.interval or self._settings.free_interval

        if blocked:
            # The store said no. Back off for everything on that store; no retries, no workarounds.
            delay = min(max(BLOCK_COOLDOWN * 2 ** (failures - 1), interval, exc.retry_after or 0), MAX_BACKOFF)
            next_check = rec.finished_at + delay
            self._host_cooldown[host] = next_check
        elif failures == 1 and not exc.permanent:
            # One missed reading: keep the normal cadence instead of punishing the product.
            next_check = self._next_due(rec.scheduled_for, rec.started_at, rec.finished_at, interval)
        else:
            next_check = rec.finished_at + min(interval * 2 ** (failures - 1), MAX_BACKOFF)

        rec.http_status = exc.status
        rec.outcome = "BLOCKED" if blocked else "FAILED"
        rec.error = str(exc)
        rec.retry_in = next_check - rec.finished_at
        await self._db.record_failure(product["id"], rec.finished_at, next_check, deactivate)
        await self._emit(rec)

        if deactivate:
            title = product["title"] or rec.url
            text = (
                f"⚠️ I can't read the price for <b>{html.escape(title, quote=False)}</b> anymore, "
                "so I paused tracking it.\nSend the link again to resume."
            )
            for tracker in trackers:
                await self._safe_notify(tracker["user_id"], text)

    async def _defer(self, product: dict, store: str, until: float) -> None:
        """Skip a product whose store is cooling down after a block (no request is made)."""
        await self._db.reschedule(product["id"], until)
        self.stats.skipped += 1
        now = self._clock()
        check_log.info(
            "[%s] %s | SKIPPED | #%s store is cooling down after a block, next try in %.0fs",
            time.strftime("%H:%M:%S", time.localtime(now)), store, product["id"], until - now,
        )

    @staticmethod
    def _next_due(scheduled: float | None, started: float, finished: float, interval: int) -> float:
        """Next check time, anchored to the schedule so fetch time never accumulates as drift."""
        if scheduled is None or started - scheduled >= interval:
            anchor = started  # far behind (restart / outage): re-anchor instead of bursting
        else:
            anchor = scheduled
        return max(anchor + interval, finished + MIN_PAUSE)

    async def _emit(self, rec: CheckRecord) -> None:
        self.stats.record(rec)
        level = logging.INFO if rec.success else logging.WARNING
        check_log.log(level, format_check(rec))
        try:
            await self._db.log_check(rec.db_row())
        except Exception:
            log.exception("Could not write the check log row")

    async def _safe_notify(
        self, user_id: int, text: str, buy_url: str | None = None, track_id: int | None = None
    ) -> None:
        try:
            # Existing integrations and tests use the original three-argument
            # notifier. The production notifier accepts this optional keyword
            # and can offer a safe, recipient-owned View Product button.
            try:
                await self._notify(user_id, text, buy_url, track_id=track_id)
            except TypeError as exc:
                if "track_id" not in str(exc):
                    raise
                await self._notify(user_id, text, buy_url)
        except Exception:
            log.exception("Could not notify user %s", user_id)
