import unittest

from bot.errors import FetchError
from bot.scheduler import MAX_FAILURES, PriceMonitor
from tests.helpers import OWNER, make_world

URL = "https://shop.example.com/p/aaa"


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.settings, self.clock, self.fetch, self.db, self.service = make_world()
        self.sent: list[tuple[int, str]] = []

        async def notify(user_id: int, text: str, buy_url: str | None = None) -> None:
            self.sent.append((user_id, text))

        self.monitor = PriceMonitor(self.db, self.service, self.fetch, notify, self.settings, self.clock)
        self.fetch.prices[URL] = 1000.0
        await self.service.add_link(2, "u", URL)

    async def asyncTearDown(self):
        self.db.close()

    async def run_check(self, advance: float = 4000):
        self.clock.advance(advance)
        await self.monitor.tick()
        await self.monitor.drain()

    async def test_nothing_happens_before_it_is_due(self):
        await self.run_check(advance=10)
        self.assertEqual(self.fetch.calls, [URL])  # only the initial add-time fetch

    async def test_price_drop_notifies_and_records_history(self):
        self.fetch.prices[URL] = 800.0
        await self.run_check()
        self.assertEqual(len(self.sent), 1)
        user_id, text = self.sent[0]
        self.assertEqual(user_id, 2)
        self.assertIn("₹800", text)
        self.assertIn("-20%", text)
        product = await self.db.get_product_by_url(URL)
        self.assertEqual(product["price"], 800.0)
        self.assertEqual(product["next_check"], self.clock.now + 1800)
        history = await self.db.price_history(product["id"])
        self.assertEqual([h["price"] for h in history], [800.0, 1000.0])

    async def test_same_or_higher_price_is_silent(self):
        await self.run_check()
        self.fetch.prices[URL] = 1200.0
        await self.run_check()
        self.assertEqual(self.sent, [])
        self.assertEqual((await self.db.get_product_by_url(URL))["price"], 1200.0)

    async def test_target_price_only_alerts_when_reached(self):
        track = (await self.service.list_tracks(2))[0]["track_id"]
        await self.service.set_target(2, track, 900.0)
        self.fetch.prices[URL] = 950.0
        await self.run_check()
        self.assertEqual(self.sent, [])
        self.fetch.prices[URL] = 850.0
        await self.run_check()
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Reached your target", self.sent[0][1])

    async def test_target_already_below_notifies_once_then_reenters(self):
        track = (await self.service.list_tracks(2))[0]["track_id"]
        await self.service.set_target(2, track, 2000.0)  # current is already ₹1,000
        await self.run_check()
        self.assertEqual(len(self.sent), 1)
        self.assertIn("PRICE TARGET REACHED", self.sent[0][1])
        await self.run_check()
        self.assertEqual(len(self.sent), 1)  # same condition is not repeated
        self.fetch.prices[URL] = 2500.0
        await self.run_check()
        self.fetch.prices[URL] = 1500.0
        await self.run_check()
        self.assertEqual(len(self.sent), 2)

    async def test_target_equal_to_current_price_notifies(self):
        track = (await self.service.list_tracks(2))[0]["track_id"]
        await self.service.set_target(2, track, 1000.0)
        await self.run_check()
        self.assertEqual(len(self.sent), 1)

    async def test_target_above_condition_is_silent(self):
        track = (await self.service.list_tracks(2))[0]["track_id"]
        await self.service.set_target(2, track, 900.0)
        await self.run_check()
        self.assertEqual(self.sent, [])

    async def test_each_tracker_is_alerted_once_and_product_fetched_once(self):
        await self.service.add_link(3, "v", URL)
        before = len(self.fetch.calls)
        self.fetch.prices[URL] = 500.0
        await self.run_check()
        self.assertEqual(len(self.fetch.calls) - before, 1)
        self.assertEqual(sorted(u for u, _ in self.sent), [2, 3])

    async def test_owner_gets_fast_schedule(self):
        await self.service.add_link(OWNER, "boss", URL)
        await self.run_check(advance=20)  # 15s interval
        self.assertEqual(len(self.fetch.calls), 2)

    async def test_failures_back_off_then_pause_and_notify(self):
        self.fetch.errors[URL] = FetchError("HTTP 503")
        for attempt in range(1, MAX_FAILURES):
            await self.run_check(advance=10**6)
            product = await self.db.get_product_by_url(URL)
            self.assertEqual((product["fail_count"], product["active"]), (attempt, 1))
        self.assertEqual(self.sent, [])
        await self.run_check(advance=10**6)
        product = await self.db.get_product_by_url(URL)
        self.assertEqual(product["active"], 0)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("paused", self.sent[0][1])

    async def test_permanent_error_pauses_immediately_and_link_can_resume(self):
        self.fetch.errors[URL] = FetchError("page not found (404)", permanent=True)
        await self.run_check()
        self.assertEqual((await self.db.get_product_by_url(URL))["active"], 0)
        self.assertEqual(len(self.sent), 1)

        del self.fetch.errors[URL]  # page is back; user sends the link again
        result = await self.service.add_link(2, "u", URL)
        self.assertEqual(result.status, "resumed")
        self.assertEqual((await self.db.get_product_by_url(URL))["active"], 1)

    async def test_notification_failure_does_not_break_the_monitor(self):
        async def broken(user_id, text):
            raise RuntimeError("telegram down")

        monitor = PriceMonitor(self.db, self.service, self.fetch, broken, self.settings, self.clock)
        self.fetch.prices[URL] = 100.0
        self.clock.advance(4000)
        await monitor.tick()
        await monitor.drain()
        self.assertEqual((await self.db.get_product_by_url(URL))["price"], 100.0)


if __name__ == "__main__":
    unittest.main()
