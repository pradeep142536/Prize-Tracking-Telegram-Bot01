"""Per-check log lines, startup banner and running statistics for the price monitor.

Pure formatting and bookkeeping: no network, no database, no Telegram.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .alerts import format_price
from .config import Settings

LATE_THRESHOLD = 1.0  # a check that starts more than this many seconds after its schedule is "late"

_KNOWN_STORES = {
    "amazon": "Amazon", "amzn": "Amazon", "flipkart": "Flipkart", "myntra": "Myntra", "ajio": "Ajio",
    "snapdeal": "Snapdeal", "meesho": "Meesho", "croma": "Croma", "nykaa": "Nykaa",
    "tatacliq": "Tata CLiQ", "reliancedigital": "Reliance Digital",
}
_SECOND_LEVEL = {"co", "com", "org", "net", "gov", "ac"}


def store_name(host: str) -> str:
    """'www.amazon.in' -> 'Amazon', 'shop.example.co.uk' -> 'Example'."""
    labels = [p for p in (host or "").lower().removeprefix("www.").split(".") if p]
    if not labels:
        return "Unknown"
    for label in labels:
        if label in _KNOWN_STORES:
            return _KNOWN_STORES[label]
    if len(labels) >= 3 and labels[-2] in _SECOND_LEVEL:
        return labels[-3].capitalize()
    return (labels[-2] if len(labels) >= 2 else labels[0]).capitalize()


def safe_url(url: str, max_path: int = 60) -> str:
    """Scheme + host + path only. Query strings, fragments and credentials can carry secrets."""
    parts = urlsplit(url or "")
    path = parts.path or "/"
    if len(path) > max_path:
        path = path[:max_path] + "…"
    return f"{parts.scheme}://{parts.hostname or ''}{path}"


def describe_change(prev: float | None, new: float, currency: str) -> tuple[str, str]:
    """Returns (outcome label, short change text), e.g. ('PRICE DROP ₹500 (-0.5%)', '-₹500')."""
    if prev is None:
        return "SUCCESS", "n/a"
    delta = new - prev
    if abs(delta) < 0.005:
        return "SUCCESS", f"±{format_price(0, currency)}"
    pct = abs(delta) / prev * 100 if prev else 0.0
    amount = format_price(abs(delta), currency)
    if delta < 0:
        return f"PRICE DROP {amount} (-{pct:.1f}%)", f"-{amount}"
    return f"PRICE RISE {amount} (+{pct:.1f}%)", f"+{amount}"


@dataclass
class CheckRecord:
    """Everything we know about one real check attempt."""

    product_id: int
    store: str
    url: str  # already passed through safe_url()
    started_at: float
    finished_at: float
    scheduled_for: float | None
    interval: int | None
    since_prev: float | None  # start-to-start time since this product's previous attempt
    queue_wait: float  # time spent waiting for the per-website gap / concurrency slot
    fetch_seconds: float
    http_status: int | None
    success: bool
    outcome: str  # SUCCESS | PRICE DROP .. | PRICE RISE .. | FAILED | BLOCKED
    price: float | None = None
    prev_price: float | None = None
    currency: str = "INR"
    change: str = "n/a"
    error: str | None = None
    trackers: int = 0
    retry_in: float | None = None  # seconds until the next attempt after a failure

    @property
    def lateness(self) -> float | None:
        return None if self.scheduled_for is None else self.started_at - self.scheduled_for

    def db_row(self) -> dict:
        return {
            "product_id": self.product_id,
            "scheduled_for": self.scheduled_for,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "interval_s": self.interval,
            "since_prev_s": self.since_prev,
            "fetch_ms": round(self.fetch_seconds * 1000),
            "http_status": self.http_status,
            "success": 1 if self.success else 0,
            "outcome": self.outcome,
            "price": self.price,
            "prev_price": self.prev_price,
            "error": self.error,
        }


def format_check(rec: CheckRecord) -> str:
    """One line per attempt, e.g.

    [16:00:15] Amazon | ₹92,990 | SUCCESS | #12 interval=15s actual=15.1s late=+0.1s wait=0.0s fetch=0.84s http=200 ...
    [16:00:30] Amazon | ₹92,490 | PRICE DROP ₹500 (-0.5%) | #12 ...
    [16:00:45] Amazon | BLOCKED | HTTP 403 blocked by the store | #12 ... retry_in=300s
    """
    clock = time.strftime("%H:%M:%S", time.localtime(rec.started_at))
    if rec.success:
        head = f"{rec.store} | {format_price(rec.price or 0, rec.currency)} | {rec.outcome}"
    else:
        head = f"{rec.store} | {rec.outcome} | {rec.error}"

    late = rec.lateness
    fields = [
        f"#{rec.product_id}",
        f"interval={rec.interval}s" if rec.interval is not None else "interval=?",
        f"actual={rec.since_prev:.1f}s" if rec.since_prev is not None else "actual=first",
        f"late={late:+.1f}s" if late is not None else "late=?",
        f"wait={rec.queue_wait:.1f}s",
        f"fetch={rec.fetch_seconds:.2f}s",
        f"http={rec.http_status if rec.http_status is not None else '-'}",
    ]
    if rec.success:
        prev = format_price(rec.prev_price, rec.currency) if rec.prev_price is not None else "n/a"
        fields += [f"prev={prev}", f"change={rec.change}"]
    elif rec.retry_in is not None:
        fields.append(f"retry_in={rec.retry_in:.0f}s")
    fields += [f"trackers={rec.trackers}", f"url={rec.url}"]
    return f"[{clock}] {head} | " + " ".join(fields)


def startup_banner(settings: Settings) -> list[str]:
    return [
        "Price Monitor Started",
        f"Owner interval: {settings.owner_interval} seconds",
        f"Premium interval: {settings.premium_interval} seconds",
        f"Free interval: {settings.free_interval} seconds",
        f"Max concurrent fetches: {settings.max_concurrency} | "
        f"Minimum gap per website: {settings.host_min_gap:g}s | Request timeout: {settings.request_timeout}s",
        f"Detailed check log: {settings.log_dir}/price_checks.log "
        f"(attempts kept in the database for {settings.check_log_hours}h)",
    ]


@dataclass
class ProductStat:
    store: str
    url: str
    successes: int = 0
    failures: int = 0
    last_success_at: float | None = None
    last_price: float | None = None
    currency: str = "INR"
    last_error: str | None = None


@dataclass
class MonitorStats:
    started_at: float
    success: int = 0
    failed: int = 0
    blocked: int = 0
    skipped: int = 0
    late: int = 0
    last_success: tuple[float, str, int] | None = None  # (finished_at, store, product_id)
    products: dict[int, ProductStat] = field(default_factory=dict)
    _fetch_sum: float = 0.0
    _fetch_n: int = 0
    _recent: deque = field(default_factory=lambda: deque(maxlen=100))

    def record(self, rec: CheckRecord) -> None:
        if rec.success:
            self.success += 1
            self.last_success = (rec.finished_at, rec.store, rec.product_id)
        else:
            self.failed += 1
            if rec.outcome == "BLOCKED":
                self.blocked += 1
        if rec.lateness is not None and rec.lateness > LATE_THRESHOLD:
            self.late += 1
        self._fetch_sum += rec.fetch_seconds
        self._fetch_n += 1
        self._recent.append(rec.fetch_seconds)

        stat = self.products.setdefault(rec.product_id, ProductStat(store=rec.store, url=rec.url))
        if rec.success:
            stat.successes += 1
            stat.last_success_at = rec.finished_at
            stat.last_price = rec.price
            stat.currency = rec.currency
            stat.last_error = None
        else:
            stat.failures += 1
            stat.last_error = rec.error

    @property
    def total(self) -> int:
        return self.success + self.failed

    @property
    def avg_fetch(self) -> float | None:
        return self._fetch_sum / self._fetch_n if self._fetch_n else None

    @property
    def avg_fetch_recent(self) -> float | None:
        return sum(self._recent) / len(self._recent) if self._recent else None


def _clock_text(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def _uptime(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else (f"{m}m {s:02d}s" if m else f"{s}s")


def format_summary(stats: MonitorStats, now: float, next_due: dict | None) -> list[str]:
    rate = f"{stats.success / stats.total * 100:.1f}%" if stats.total else "n/a"
    if stats.last_success:
        ts, store, pid = stats.last_success
        last = f"{_clock_text(ts)} ({store} #{pid}, {_uptime(now - ts)} ago)"
    else:
        last = "none yet"
    if next_due:
        wait = next_due["next_check"] - now
        when = f"in {wait:.0f}s" if wait >= 0 else f"{-wait:.0f}s overdue"
        nxt = f"{_clock_text(next_due['next_check'])} ({when}) | {store_name(next_due['domain'])} #{next_due['id']}"
    else:
        nxt = "none (no active products)"
    avg = f"{stats.avg_fetch:.2f}s" if stats.avg_fetch is not None else "n/a"
    recent = f" (last {len(stats._recent)}: {stats.avg_fetch_recent:.2f}s)" if stats._recent else ""
    return [
        f"Price Monitor summary | uptime {_uptime(now - stats.started_at)}",
        f"  Successful checks: {stats.success}",
        f"  Failed checks: {stats.failed} (blocked by store: {stats.blocked}, skipped while cooling down: {stats.skipped})",
        f"  Success rate: {rate}",
        f"  Last successful check: {last}",
        f"  Next scheduled check: {nxt}",
        f"  Average fetch duration: {avg}{recent}",
        f"  Checks that started >{LATE_THRESHOLD:g}s late: {stats.late}",
    ]


def format_products(stats: MonitorStats, schedule: dict[int, float], inflight: set[int], now: float, limit: int = 10) -> list[str]:
    lines = []
    for pid, st in list(stats.products.items())[:limit]:
        if pid in inflight:
            nxt = "checking now"
        elif pid in schedule:
            wait = schedule[pid] - now
            nxt = f"next in {wait:.0f}s" if wait >= 0 else f"{-wait:.0f}s overdue"
        else:
            nxt = "removed"
        last_ok = _clock_text(st.last_success_at) if st.last_success_at else "never"
        price = format_price(st.last_price, st.currency) if st.last_price is not None else "n/a"
        err = f" | last error: {st.last_error}" if st.last_error else ""
        lines.append(f"  {st.store} #{pid} | ok {st.successes} / failed {st.failures} | last ok {last_ok} | {nxt} | {price}{err}")
    return lines
