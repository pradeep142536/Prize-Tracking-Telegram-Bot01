"""SQLite storage. Sync sqlite3 under a lock, exposed as async via worker threads."""
from __future__ import annotations

import asyncio
import functools
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id       INTEGER PRIMARY KEY,
    username      TEXT,
    premium_until INTEGER NOT NULL DEFAULT 0,
    subscription_plan TEXT NOT NULL DEFAULT 'free',
    trial_used     INTEGER NOT NULL DEFAULT 0,
    founding_used  INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS products (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    url          TEXT NOT NULL UNIQUE,
    domain       TEXT NOT NULL,
    title        TEXT,
    image_url    TEXT,
    price        REAL,
    currency     TEXT NOT NULL DEFAULT 'INR',
    last_checked INTEGER NOT NULL DEFAULT 0,
    next_check   INTEGER NOT NULL DEFAULT 0,
    fail_count   INTEGER NOT NULL DEFAULT 0,
    active       INTEGER NOT NULL DEFAULT 1,
    created_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_products_due ON products(active, next_check);
CREATE TABLE IF NOT EXISTS tracks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    product_id   INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    target_price REAL,
    target_alerted INTEGER NOT NULL DEFAULT 0,
    created_at   INTEGER NOT NULL,
    UNIQUE(user_id, product_id)
);
CREATE INDEX IF NOT EXISTS idx_tracks_user ON tracks(user_id);
CREATE INDEX IF NOT EXISTS idx_tracks_product ON tracks(product_id);
CREATE TABLE IF NOT EXISTS price_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    price      REAL NOT NULL,
    ts         INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_product ON price_history(product_id, ts);
CREATE TABLE IF NOT EXISTS payments (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id   INTEGER NOT NULL,
    charge_id TEXT NOT NULL UNIQUE,
    stars     INTEGER NOT NULL,
    plan      TEXT,
    purchase_kind TEXT,
    ts        INTEGER NOT NULL
);
-- One row per real price-check attempt (actual times, not just scheduled ones).
-- Added later than the tables above; CREATE IF NOT EXISTS leaves existing databases intact.
CREATE TABLE IF NOT EXISTS check_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id    INTEGER NOT NULL,
    scheduled_for REAL,
    started_at    REAL NOT NULL,
    finished_at   REAL NOT NULL,
    interval_s    INTEGER,
    since_prev_s  REAL,
    fetch_ms      INTEGER,
    http_status   INTEGER,
    success       INTEGER NOT NULL,
    outcome       TEXT NOT NULL,
    price         REAL,
    prev_price    REAL,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_check_log_product ON check_log(product_id, started_at);
CREATE INDEX IF NOT EXISTS idx_check_log_time ON check_log(started_at);
"""

DAY = 86400


def _threaded(fn):
    """Run a sync Database method in a worker thread, serialised by the DB lock."""

    @functools.wraps(fn)
    async def wrapper(self, *args, **kwargs):
        def call():
            with self._lock:
                return fn(self, *args, **kwargs)

        return await asyncio.to_thread(call)

    return wrapper


def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class Database:
    def __init__(self, path: str) -> None:
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.RLock()
        self._conn.executescript(SCHEMA)
        # ``CREATE TABLE IF NOT EXISTS`` intentionally does not change an
        # existing installation.  Keep the small additive migration here so
        # deployments with an older tracker.db gain image-aware product cards
        # without losing any data.
        product_columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(products)")}
        if "image_url" not in product_columns:
            self._conn.execute("ALTER TABLE products ADD COLUMN image_url TEXT")
        track_columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(tracks)")}
        if "target_alerted" not in track_columns:
            self._conn.execute("ALTER TABLE tracks ADD COLUMN target_alerted INTEGER NOT NULL DEFAULT 0")
        user_columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(users)")}
        for name, definition in (
            ("subscription_plan", "TEXT NOT NULL DEFAULT 'free'"),
            ("trial_used", "INTEGER NOT NULL DEFAULT 0"),
            ("founding_used", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in user_columns:
                self._conn.execute(f"ALTER TABLE users ADD COLUMN {name} {definition}")
        payment_columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(payments)")}
        for name, definition in (("plan", "TEXT"), ("purchase_kind", "TEXT")):
            if name not in payment_columns:
                self._conn.execute(f"ALTER TABLE payments ADD COLUMN {name} {definition}")
        # Existing paid accounts retain their entitlement rather than being
        # silently downgraded when the richer plan model is introduced.
        self._conn.execute(
            "UPDATE users SET subscription_plan='premium' "
            "WHERE premium_until > 0 AND subscription_plan='free'"
        )

    @contextmanager
    def _tx(self):
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- users -----------------------------------------------------------
    @_threaded
    def upsert_user(self, user_id: int, username: str | None, now: int) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO users(user_id, username, created_at) VALUES(?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username",
                (user_id, username, now),
            )

    @_threaded
    def get_user(self, user_id: int) -> dict[str, Any] | None:
        return _dict(self._conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone())

    @_threaded
    def grant_premium(self, user_id: int, days: int, now: int) -> int:
        """Extend premium by ``days`` (stacking on remaining time). Returns the new expiry."""
        with self._tx() as c:
            c.execute("INSERT OR IGNORE INTO users(user_id, created_at) VALUES(?,?)", (user_id, now))
            current = c.execute("SELECT premium_until FROM users WHERE user_id=?", (user_id,)).fetchone()[0]
            until = max(now, current) + days * DAY
            c.execute("UPDATE users SET premium_until=?, subscription_plan='premium' WHERE user_id=?", (until, user_id))
            return until

    @_threaded
    def activate_subscription(
        self, user_id: int, plan: str, days: int, now: int, kind: str = "purchase"
    ) -> int:
        """Activate a named plan and mark one-time offer use atomically."""
        with self._tx() as c:
            c.execute("INSERT OR IGNORE INTO users(user_id, created_at) VALUES(?,?)", (user_id, now))
            current = c.execute("SELECT premium_until FROM users WHERE user_id=?", (user_id,)).fetchone()[0]
            until = max(now, current) + days * DAY
            c.execute("UPDATE users SET premium_until=?, subscription_plan=? WHERE user_id=?", (until, plan, user_id))
            if kind == "trial":
                c.execute("UPDATE users SET trial_used=1 WHERE user_id=?", (user_id,))
            elif kind == "founding":
                c.execute("UPDATE users SET founding_used=1 WHERE user_id=?", (user_id,))
            return until

    @_threaded
    def record_payment(
        self, user_id: int, charge_id: str, stars: int, now: int, plan: str | None = None, kind: str | None = None
    ) -> bool:
        """Store a payment. False if this charge was already recorded (duplicate delivery)."""
        with self._tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO payments(user_id, charge_id, stars, ts, plan, purchase_kind) VALUES(?,?,?,?,?,?)",
                (user_id, charge_id, stars, now, plan, kind),
            )
            return cur.rowcount == 1

    @_threaded
    def offer_info(self, user_id: int) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT trial_used, founding_used FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        return dict(row) if row else {"trial_used": 0, "founding_used": 0}

    @_threaded
    def founding_sales_count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM payments WHERE purchase_kind='founding'").fetchone()[0])

    # ---- products --------------------------------------------------------
    @_threaded
    def get_product_by_url(self, url: str) -> dict[str, Any] | None:
        return _dict(self._conn.execute("SELECT * FROM products WHERE url=?", (url,)).fetchone())

    @_threaded
    def get_product(self, product_id: int) -> dict[str, Any] | None:
        return _dict(self._conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone())

    @_threaded
    def create_product(
        self,
        url: str,
        domain: str,
        title: str,
        price: float,
        currency: str,
        now: int,
        next_check: int,
        image_url: str | None = None,
    ) -> int:
        with self._tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO products("
                "url, domain, title, image_url, price, currency, last_checked, next_check, created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?)",
                (url, domain, title, image_url, price, currency, now, next_check, now),
            )
            product_id = c.execute("SELECT id FROM products WHERE url=?", (url,)).fetchone()[0]
            if cur.rowcount:
                c.execute("INSERT INTO price_history(product_id, price, ts) VALUES(?,?,?)", (product_id, price, now))
            return product_id

    @_threaded
    def reactivate_product(
        self,
        product_id: int,
        title: str,
        price: float,
        currency: str,
        now: int,
        next_check: int,
        image_url: str | None = None,
    ) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE products SET active=1, fail_count=0, title=?, image_url=COALESCE(?, image_url), price=?, currency=?, "
                "last_checked=?, next_check=? WHERE id=?",
                (title, image_url, price, currency, now, next_check, product_id),
            )
            c.execute("INSERT INTO price_history(product_id, price, ts) VALUES(?,?,?)", (product_id, price, now))

    @_threaded
    def claim_due(self, now: float, limit: int, lease: int) -> list[dict[str, Any]]:
        """Return products due for a check and push their next_check out by ``lease`` seconds.

        The lease stops the same product being claimed twice while a check is still in
        flight; a finished check overwrites next_check with the real schedule.
        """
        with self._tx() as c:
            rows = c.execute(
                "SELECT * FROM products WHERE active=1 AND next_check<=? ORDER BY next_check LIMIT ?",
                (now, limit),
            ).fetchall()
            if rows:
                ids = [r["id"] for r in rows]
                marks = ",".join("?" * len(ids))
                c.execute(f"UPDATE products SET next_check=? WHERE id IN ({marks})", (now + lease, *ids))
            return [dict(r) for r in rows]

    @_threaded
    def record_check(
        self, product_id: int, title: str, price: float, currency: str, now: float, next_check: float
    ) -> None:
        with self._tx() as c:
            old = c.execute("SELECT price FROM products WHERE id=?", (product_id,)).fetchone()
            if old is None:
                return
            c.execute(
                "UPDATE products SET title=?, price=?, currency=?, last_checked=?, next_check=?, fail_count=0 "
                "WHERE id=?",
                (title, price, currency, now, next_check, product_id),
            )
            if old["price"] is None or abs(old["price"] - price) > 1e-9:
                c.execute("INSERT INTO price_history(product_id, price, ts) VALUES(?,?,?)", (product_id, price, now))

    @_threaded
    def set_next_check(self, product_id: int, next_check: int) -> None:
        """Bring a product's next check forward (never pushes it later)."""
        with self._tx() as c:
            c.execute("UPDATE products SET next_check=MIN(next_check, ?) WHERE id=?", (next_check, product_id))

    @_threaded
    def reschedule(self, product_id: int, next_check: float) -> None:
        """Set a product's next check exactly (used to defer it while its store is cooling down)."""
        with self._tx() as c:
            c.execute("UPDATE products SET next_check=? WHERE id=?", (next_check, product_id))

    @_threaded
    def earliest_due(self) -> dict[str, Any] | None:
        """The active product whose next check is soonest (None if nothing is being watched)."""
        return _dict(
            self._conn.execute(
                "SELECT id, domain, title, next_check FROM products WHERE active=1 ORDER BY next_check LIMIT 1"
            ).fetchone()
        )

    @_threaded
    def schedule_of(self, product_ids: list[int]) -> dict[int, float]:
        if not product_ids:
            return {}
        marks = ",".join("?" * len(product_ids))
        rows = self._conn.execute(
            f"SELECT id, next_check FROM products WHERE id IN ({marks})", product_ids
        ).fetchall()
        return {r["id"]: r["next_check"] for r in rows}

    @_threaded
    def log_check(self, row: dict[str, Any]) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO check_log(product_id, scheduled_for, started_at, finished_at, interval_s, "
                "since_prev_s, fetch_ms, http_status, success, outcome, price, prev_price, error) "
                "VALUES(:product_id, :scheduled_for, :started_at, :finished_at, :interval_s, "
                ":since_prev_s, :fetch_ms, :http_status, :success, :outcome, :price, :prev_price, :error)",
                row,
            )

    @_threaded
    def recent_checks(self, product_id: int, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM check_log WHERE product_id=? ORDER BY started_at DESC, id DESC LIMIT ?",
            (product_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    @_threaded
    def prune_check_log(self, older_than: float) -> int:
        """Delete attempt logs older than ``older_than`` (epoch seconds). Price history is never touched."""
        with self._tx() as c:
            return c.execute("DELETE FROM check_log WHERE started_at < ?", (older_than,)).rowcount

    @_threaded
    def record_failure(self, product_id: int, now: float, next_check: float, deactivate: bool) -> int:
        with self._tx() as c:
            c.execute(
                "UPDATE products SET fail_count=fail_count+1, last_checked=?, next_check=?, "
                "active=CASE WHEN ? THEN 0 ELSE active END WHERE id=?",
                (now, next_check, 1 if deactivate else 0, product_id),
            )
            row = c.execute("SELECT fail_count FROM products WHERE id=?", (product_id,)).fetchone()
            return row[0] if row else 0

    @_threaded
    def price_history(self, product_id: int, limit: int = 30) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT price, ts FROM price_history WHERE product_id=? ORDER BY ts DESC, id DESC LIMIT ?",
            (product_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- tracks ----------------------------------------------------------
    @_threaded
    def add_track(self, user_id: int, product_id: int, limit: int | None, now: int) -> str:
        """Atomically add a track. Returns 'ok', 'exists' or 'limit'."""
        with self._tx() as c:
            if c.execute(
                "SELECT 1 FROM tracks WHERE user_id=? AND product_id=?", (user_id, product_id)
            ).fetchone():
                return "exists"
            used = c.execute("SELECT COUNT(*) FROM tracks WHERE user_id=?", (user_id,)).fetchone()[0]
            if limit is not None and used >= limit:
                return "limit"
            c.execute(
                "INSERT INTO tracks(user_id, product_id, created_at) VALUES(?,?,?)", (user_id, product_id, now)
            )
            return "ok"

    @_threaded
    def count_tracks(self, user_id: int) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM tracks WHERE user_id=?", (user_id,)).fetchone()[0]

    @_threaded
    def list_tracks(self, user_id: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT t.id AS track_id, t.target_price, t.created_at AS track_created_at, "
            "p.id AS product_id, p.url, p.domain, p.title, p.image_url, p.price, p.currency, "
            "p.last_checked, p.active FROM tracks t JOIN products p ON p.id=t.product_id "
            "WHERE t.user_id=? ORDER BY t.id",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    @_threaded
    def get_track(self, user_id: int, track_id: int) -> dict[str, Any] | None:
        """Return one product card only when it belongs to ``user_id``.

        Keeping ownership in the SQL predicate is important: Telegram callback
        data is user-controlled and must never become a way to read or alter a
        different user's saved product.
        """
        return _dict(
            self._conn.execute(
                "SELECT t.id AS track_id, t.target_price, t.created_at AS track_created_at, "
                "p.id AS product_id, p.url, p.domain, p.title, p.image_url, p.price, p.currency, "
                "p.last_checked, p.next_check, p.fail_count, p.active "
                "FROM tracks t JOIN products p ON p.id=t.product_id "
                "WHERE t.id=? AND t.user_id=?",
                (track_id, user_id),
            ).fetchone()
        )

    @_threaded
    def get_track_by_product(self, user_id: int, product_id: int) -> dict[str, Any] | None:
        """Return a user's track for a product, if they already have one."""
        return _dict(
            self._conn.execute(
                "SELECT t.id AS track_id, t.target_price, t.created_at AS track_created_at, "
                "p.id AS product_id, p.url, p.domain, p.title, p.image_url, p.price, p.currency, "
                "p.last_checked, p.next_check, p.fail_count, p.active "
                "FROM tracks t JOIN products p ON p.id=t.product_id "
                "WHERE t.user_id=? AND t.product_id=?",
                (user_id, product_id),
            ).fetchone()
        )

    @_threaded
    def track_id_for_number(self, user_id: int, number: int) -> int | None:
        """Resolve a user's 1-based product number to its internal track ID."""
        if number < 1:
            return None
        row = self._conn.execute(
            "SELECT id FROM tracks WHERE user_id=? ORDER BY id LIMIT 1 OFFSET ?",
            (user_id, number - 1),
        ).fetchone()
        return int(row["id"]) if row else None

    @_threaded
    def remove_track_number(self, user_id: int, number: int) -> bool:
        """Remove a track using the user's own 1-based product number."""
        if number < 1:
            return False
        with self._tx() as c:
            row = c.execute(
                "SELECT id, product_id FROM tracks WHERE user_id=? ORDER BY id LIMIT 1 OFFSET ?",
                (user_id, number - 1),
            ).fetchone()
            if row is None:
                return False
            c.execute("DELETE FROM tracks WHERE id=? AND user_id=?", (row["id"], user_id))
            c.execute(
                "DELETE FROM products WHERE id=? AND NOT EXISTS (SELECT 1 FROM tracks WHERE product_id=?)",
                (row["product_id"], row["product_id"]),
            )
            return True

    @_threaded
    def set_target_number(self, user_id: int, number: int, target: float | None) -> bool:
        """Set target using the user's own 1-based product number."""
        if number < 1:
            return False
        with self._tx() as c:
            row = c.execute(
                "SELECT id FROM tracks WHERE user_id=? ORDER BY id LIMIT 1 OFFSET ?",
                (user_id, number - 1),
            ).fetchone()
            if row is None:
                return False
            cur = c.execute(
                "UPDATE tracks SET target_price=?, target_alerted=0 WHERE id=? AND user_id=?",
                (target, row["id"], user_id),
            )
            return cur.rowcount == 1

    @_threaded
    def remove_track(self, user_id: int, track_id: int) -> bool:
        with self._tx() as c:
            row = c.execute(
                "SELECT product_id FROM tracks WHERE id=? AND user_id=?", (track_id, user_id)
            ).fetchone()
            if row is None:
                return False
            c.execute("DELETE FROM tracks WHERE id=?", (track_id,))
            # nobody tracks this product any more: stop checking it and free the space
            c.execute(
                "DELETE FROM products WHERE id=? AND NOT EXISTS (SELECT 1 FROM tracks WHERE product_id=?)",
                (row["product_id"], row["product_id"]),
            )
            return True

    @_threaded
    def set_target(self, user_id: int, track_id: int, target: float | None) -> bool:
        with self._tx() as c:
            cur = c.execute(
                "UPDATE tracks SET target_price=?, target_alerted=0 WHERE id=? AND user_id=?", (target, track_id, user_id)
            )
            return cur.rowcount == 1

    @_threaded
    def claim_target_alert(self, user_id: int, track_id: int) -> bool:
        """Atomically claim a target-reached message for this user-owned track."""
        with self._tx() as c:
            cur = c.execute(
                "UPDATE tracks SET target_alerted=1 WHERE id=? AND user_id=? "
                "AND target_price IS NOT NULL AND target_alerted=0",
                (track_id, user_id),
            )
            return cur.rowcount == 1

    @_threaded
    def reset_target_alert(self, user_id: int, track_id: int) -> None:
        with self._tx() as c:
            c.execute("UPDATE tracks SET target_alerted=0 WHERE id=? AND user_id=?", (track_id, user_id))

    @_threaded
    def get_trackers(self, product_id: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT t.id AS track_id, t.user_id, t.target_price, t.target_alerted, COALESCE(u.premium_until, 0) AS premium_until, "
            "COALESCE(u.subscription_plan, 'free') AS subscription_plan "
            "FROM tracks t LEFT JOIN users u ON u.user_id=t.user_id WHERE t.product_id=?",
            (product_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- admin -----------------------------------------------------------
    @_threaded
    def stats(self) -> dict[str, int]:
        one = lambda sql: self._conn.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "users": one("SELECT COUNT(*) FROM users"),
            "tracks": one("SELECT COUNT(*) FROM tracks"),
            "products": one("SELECT COUNT(*) FROM products WHERE active=1"),
            "paused": one("SELECT COUNT(*) FROM products WHERE active=0"),
            "stars": one("SELECT COALESCE(SUM(stars),0) FROM payments"),
        }
