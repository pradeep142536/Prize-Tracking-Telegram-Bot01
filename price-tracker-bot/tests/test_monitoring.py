"""Cadence, overlap, block handling, logging and summary of the price monitor."""
import asyncio
import os
import sqlite3
import tempfile
import unittest

from bot.database import SCHEMA, Database
from bot.errors import FetchError
from bot.scheduler import PriceMonitor
from tests.helpers import OWNER, START, make_world

URL = "https://shop.example.com/p/aaa"
URL_B = "https://shop.example.com/p/bbb"
AMAZON = "https://www.amazon.in/dp/B09B8V1LZ3"


async def wait_until(predicate, timeout: float = 3.0) -> None:
    for _ in range(int(timeout / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached in time")


class MonitorTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.settings, self.clock, self.fetch, self.db, self.service = make_world()
        self.sent: list[tuple[int, str]] = []

        async def notify(user_id: int, text: str, buy_url: str | None = None) -> None:
            self.sent.append((user_id, text))

        self.monitor = PriceMonitor(self.db, self.service, self.fetch, notify, self.settings, self.clock)

    async def asyncTearDown(self):
        self.db.close()

    async def at(self, when: float):
        """Move the clock to ``when``, run one scheduler round and wait for its checks."""
        self.clock.now = when
        await self.monitor.tick()
        await self.monitor.drain()

    async def product(self, url: str = URL) -> dict:
        return await self.db.get_product_by_url(url)


class CadenceTests(MonitorTestCase):
    async def test_owner_cadence_does_not_drift_with_fetch_time(self):
        self.fetch.prices[URL] = 1000.0
        self.fetch.duration = 0.8  # every fetch takes 0.8 s
        await self.service.add_link(OWNER, "boss", URL)
        self.assertEqual((await self.product())["next_check"], START + 15)

        await self.at(START + 15)  # exactly on schedule, finishes at +15.8
        self.assertEqual((await self.product())["next_check"], START + 30)  # not 30.8

        await self.at(START + 30.4)  # started a little late: next one is still on the grid
        product = await self.product()
        self.assertEqual(product["next_check"], START + 45)

        rows = await self.db.recent_checks(product["id"])
        self.assertEqual(len(rows), 2)
        newest = rows[0]
        self.assertAlmostEqual(newest["since_prev_s"], 15.4, places=3)  # actual start-to-start time
        self.assertAlmostEqual(newest["scheduled_for"], START + 30, places=3)
        self.assertEqual((newest["success"], newest["http_status"], newest["fetch_ms"]), (1, 200, 800))

    async def test_far_behind_schedule_reanchors_instead_of_bursting(self):
        self.fetch.prices[URL] = 1000.0
        await self.service.add_link(OWNER, "boss", URL)
        await self.at(START + 3600)  # the bot was off for an hour
        self.assertEqual((await self.product())["next_check"], START + 3615)
        await self.at(START + 3601)  # nothing owed: no catch-up burst
        self.assertEqual(len(self.fetch.calls), 2)  # add-time fetch + one check

    async def test_each_plan_gets_its_own_interval(self):
        urls = {OWNER: "https://a.example.com/p/o01", 5: "https://a.example.com/p/f01", 6: "https://a.example.com/p/p01"}
        for url in urls.values():
            self.fetch.prices[url] = 100.0
        await self.service.grant_premium(6, 30)
        for user, url in urls.items():
            await self.service.add_link(user, "u", url)
        nxt = {user: (await self.product(url))["next_check"] for user, url in urls.items()}
        self.assertEqual(nxt, {OWNER: START + 15, 6: START + 300, 5: START + 1800})

    async def test_failed_check_keeps_cadence_once_then_backs_off(self):
        self.fetch.prices[URL] = 1000.0
        await self.service.add_link(2, "u", URL)  # free: 1800 s
        self.fetch.errors[URL] = FetchError("HTTP 503", status=503)
        await self.at(START + 1800)
        product = await self.product()
        self.assertEqual((product["fail_count"], product["next_check"]), (1, START + 3600))
        await self.at(START + 3600)
        product = await self.product()
        self.assertEqual((product["fail_count"], product["next_check"]), (2, START + 3600 + 3600))


class OverlapAndTimeoutTests(MonitorTestCase):
    async def test_slow_site_is_never_fetched_twice_at_once(self):
        self.fetch.prices[URL] = 1000.0
        await self.service.add_link(2, "u", URL)
        self.fetch.block = True
        self.clock.now = START + 1800
        await self.monitor.tick()
        await wait_until(lambda: len(self.fetch.calls) == 2)  # first check is now hanging

        self.clock.now = START + 1800 + 700  # claim lease expired, product is due again
        await self.monitor.tick()
        await asyncio.sleep(0.1)
        self.assertEqual(len(self.fetch.calls), 2)  # no second request was started
        self.assertEqual(len(self.monitor._tasks), 1)

        self.fetch.gate.set()
        await self.monitor.drain()
        self.assertEqual(len(await self.db.recent_checks((await self.product())["id"])), 1)

    async def test_hanging_fetch_hits_the_hard_timeout(self):
        self.fetch.prices[URL] = 1000.0
        await self.service.add_link(2, "u", URL)
        self.fetch.block = True
        self.monitor._hard_timeout = 0.05
        await self.at(START + 1800)
        row = (await self.db.recent_checks((await self.product())["id"]))[0]
        self.assertEqual(row["success"], 0)
        self.assertIn("timeout", row["error"])
        self.assertEqual(self.monitor.stats.failed, 1)


class BlockedStoreTests(MonitorTestCase):
    async def test_403_is_a_block_and_the_whole_store_cools_down(self):
        self.fetch.prices[URL] = self.fetch.prices[URL_B] = 1000.0
        await self.service.add_link(OWNER, "boss", URL)  # due at START+15
        self.clock.now = START + 10
        await self.service.add_link(OWNER, "boss", URL_B)  # due at START+25
        a_id = (await self.product(URL))["id"]
        calls_before = len(self.fetch.calls)

        self.fetch.errors[URL] = FetchError("HTTP 403 blocked by the store", status=403, blocked=True)
        await self.at(START + 16)
        a = await self.product(URL)
        self.assertEqual((a["fail_count"], a["active"]), (1, 1))
        self.assertEqual(a["next_check"], START + 16 + 300)  # long cool-down, not a 15 s retry
        row = (await self.db.recent_checks(a_id))[0]
        self.assertEqual((row["outcome"], row["http_status"], row["success"]), ("BLOCKED", 403, 0))
        self.assertEqual(self.monitor.stats.blocked, 1)

        await self.at(START + 26)  # B is due, but its store just blocked us: no request at all
        self.assertEqual(len(self.fetch.calls), calls_before + 1)  # only A's blocked attempt
        self.assertEqual((await self.product(URL_B))["next_check"], START + 316)
        self.assertEqual(self.monitor.stats.skipped, 1)

        await self.at(START + 100)  # still cooling down: nothing happens
        self.assertEqual(len(self.fetch.calls), calls_before + 1)

        await self.at(START + 317)  # cool-down over; still blocked, so the pause doubles
        a = await self.product(URL)
        self.assertEqual((a["fail_count"], a["next_check"]), (2, START + 317 + 600))

    async def test_retry_after_header_is_respected(self):
        self.fetch.prices[URL] = 1000.0
        await self.service.add_link(OWNER, "boss", URL)
        self.fetch.errors[URL] = FetchError("HTTP 429", status=429, blocked=True, retry_after=900)
        await self.at(START + 15)
        self.assertEqual((await self.product())["next_check"], START + 15 + 900)

    async def test_captcha_page_counts_as_blocked(self):
        self.fetch.prices[URL] = 1000.0
        await self.service.add_link(2, "u", URL)
        self.fetch.errors[URL] = FetchError("bot-check (captcha) page", status=200, blocked=True)
        await self.at(START + 1800)
        self.assertEqual(self.monitor.stats.blocked, 1)


class LoggingAndSummaryTests(MonitorTestCase):
    async def test_every_attempt_is_logged_with_all_fields(self):
        self.fetch.prices[AMAZON] = 1000.0
        await self.service.add_link(2, "u", AMAZON)
        self.fetch.prices[AMAZON] = 900.0
        with self.assertLogs("pricecheck", "INFO") as logs:
            await self.at(START + 4000)
        line = "\n".join(logs.output)
        for expected in (
            "Amazon", "₹900", "PRICE DROP ₹100 (-10.0%)", "#1", "interval=1800s", "actual=first",
            "late=+2200.0s", "wait=0.0s", "fetch=0.00s", "http=200", "prev=₹1,000", "change=-₹100",
            "trackers=1", "url=https://www.amazon.in/dp/B09B8V1LZ3",
        ):
            self.assertIn(expected, line)

    async def test_failure_line_has_reason_and_retry(self):
        self.fetch.prices[URL] = 1000.0
        await self.service.add_link(2, "u", URL)
        self.fetch.errors[URL] = FetchError("HTTP 503", status=503)
        with self.assertLogs("pricecheck", "WARNING") as logs:
            await self.at(START + 1800)
        line = logs.output[0]
        self.assertIn("FAILED | HTTP 503", line)
        self.assertIn("http=503", line)
        self.assertIn("retry_in=1800s", line)

    async def test_summary_reports_counts_last_next_and_average(self):
        self.fetch.prices[URL] = 1000.0
        self.fetch.duration = 0.5
        await self.service.add_link(2, "u", URL)
        await self.at(START + 1800)
        self.fetch.errors[URL] = FetchError("HTTP 503", status=503)
        await self.at(START + 3600)

        text = await self.monitor.summary()
        for expected in (
            "Successful checks: 1", "Failed checks: 1", "Success rate: 50.0%", "Last successful check:",
            "Example #1", "Next scheduled check:", "Average fetch duration: 0.50s", "Products:",
        ):
            self.assertIn(expected, text)

    async def test_startup_banner_lists_the_three_intervals(self):
        from bot.checklog import startup_banner

        lines = startup_banner(self.settings)
        self.assertEqual(
            lines[:4],
            ["Price Monitor Started", "Owner interval: 15 seconds", "Premium interval: 300 seconds",
             "Free interval: 1800 seconds"],
        )

    async def test_fetch_failure_is_logged_without_query_string_secrets(self):
        url = "https://shop.example.com/p/aaa?token=SUPERSECRET"
        self.fetch.prices[url] = 500.0
        await self.service.add_link(2, "u", url)
        self.fetch.prices[url] = 400.0
        with self.assertLogs("pricecheck", "INFO") as logs:
            await self.at(START + 4000)
        text = "\n".join(logs.output)
        self.assertNotIn("SUPERSECRET", text)
        self.assertNotIn("token=", text)


class CheckLogStorageTests(MonitorTestCase):
    async def test_old_attempt_rows_are_pruned_but_price_history_is_kept(self):
        self.fetch.prices[URL] = 1000.0
        await self.service.add_link(2, "u", URL)
        pid = (await self.product())["id"]
        row = dict(product_id=pid, scheduled_for=None, started_at=START - 10_000, finished_at=START - 10_000,
                   interval_s=15, since_prev_s=None, fetch_ms=10, http_status=200, success=1, outcome="SUCCESS",
                   price=1.0, prev_price=None, error=None)
        await self.db.log_check(row)
        await self.db.log_check({**row, "started_at": START, "finished_at": START})
        self.assertEqual(await self.db.prune_check_log(START - 100), 1)
        self.assertEqual(len(await self.db.recent_checks(pid)), 1)
        self.assertEqual(len(await self.db.price_history(pid)), 1)

    async def test_existing_database_keeps_its_data_when_check_log_is_added(self):
        old_schema = SCHEMA.split("-- One row per real price-check attempt")[0]  # schema before this feature
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "old.db")
            conn = sqlite3.connect(path)
            conn.executescript(old_schema)
            conn.execute("INSERT INTO products(url, domain, title, price, currency, created_at) "
                         "VALUES('https://a.com/x','a.com','Old',99,'INR',1)")
            conn.execute("INSERT INTO price_history(product_id, price, ts) VALUES(1, 99, 1)")
            conn.commit()
            conn.close()

            db = Database(path)
            try:
                self.assertEqual((await db.get_product(1))["price"], 99)
                self.assertEqual([h["price"] for h in await db.price_history(1)], [99])
                self.assertEqual(await db.recent_checks(1), [])  # new table exists and is empty
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
