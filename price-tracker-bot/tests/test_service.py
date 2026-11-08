import unittest

from bot.errors import FetchError
from tests.helpers import OWNER, make_world

A = "https://shop.example.com/p/aaa"
B = "https://shop.example.com/p/bbb"
C = "https://shop.example.com/p/ccc"
D = "https://shop.example.com/p/ddd"


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.settings, self.clock, self.fetch, self.db, self.service = make_world()
        for url in (A, B, C, D):
            self.fetch.prices[url] = 1000.0

    async def asyncTearDown(self):
        self.db.close()

    async def test_free_user_limited_to_two_links(self):
        self.assertEqual((await self.service.add_link(2, "u", A)).status, "added")
        self.assertEqual((await self.service.add_link(2, "u", B)).status, "added")
        third = await self.service.add_link(2, "u", C)
        self.assertEqual(third.status, "limit")
        self.assertEqual((third.used, third.limit), (2, 2))
        self.assertEqual(await self.db.count_tracks(2), 2)
        self.assertNotIn(C, self.fetch.calls)  # no wasted fetch for an over-limit user

    async def test_owner_is_unlimited(self):
        for url in (A, B, C, D):
            self.assertEqual((await self.service.add_link(OWNER, "boss", url)).status, "added")
        self.assertEqual(await self.db.count_tracks(OWNER), 4)

    async def test_duplicate_and_shared_product(self):
        await self.service.add_link(2, "u", A)
        self.assertEqual((await self.service.add_link(2, "u", A)).status, "exists")
        self.assertEqual((await self.service.add_link(3, "v", A)).status, "added")
        self.assertEqual(self.fetch.calls.count(A), 1)  # second user reuses the stored product

    async def test_existing_track_reported_even_when_at_limit(self):
        await self.service.add_link(2, "u", A)
        await self.service.add_link(2, "u", B)
        self.assertEqual((await self.service.add_link(2, "u", A)).status, "exists")

    async def test_fetch_failure_does_not_use_a_slot(self):
        self.fetch.errors[A] = FetchError("blocked")
        result = await self.service.add_link(2, "u", A)
        self.assertEqual((result.status, result.error), ("fetch_failed", "blocked"))
        self.assertEqual(await self.db.count_tracks(2), 0)

    async def test_invalid_and_private_links_are_rejected(self):
        for url in ("http://localhost/x", "not a url at all", "http://10.1.1.1/a"):
            with self.subTest(url=url):
                self.assertEqual((await self.service.add_link(2, "u", url)).status, "invalid")
        self.assertEqual(self.fetch.calls, [])

    async def test_premium_raises_limit_and_expires(self):
        await self.service.add_link(2, "u", A)
        await self.service.add_link(2, "u", B)
        await self.service.grant_premium(2, days=30)
        self.assertEqual((await self.service.add_link(2, "u", C)).status, "added")
        info = await self.service.plan_info(2)
        self.assertEqual((info["plan"], info["limit"], info["interval"]), ("premium", 25, 300))

        self.clock.advance(31 * 86400)  # premium lapses; existing links stay, new ones blocked
        self.assertEqual((await self.service.plan_info(2))["plan"], "free")
        self.assertEqual((await self.service.add_link(2, "u", D)).status, "limit")

    async def test_premium_stacks_on_remaining_time(self):
        first = await self.service.grant_premium(2, 30)
        second = await self.service.grant_premium(2, 30)
        self.assertEqual(second - first, 30 * 86400)

    async def test_owner_link_makes_product_check_fast(self):
        await self.service.add_link(2, "u", A)  # free: 1800s
        product = await self.db.get_product_by_url(A)
        self.assertEqual(product["next_check"], self.clock.now + 1800)
        await self.service.add_link(OWNER, "boss", A)  # owner: 15s
        product = await self.db.get_product_by_url(A)
        self.assertEqual(product["next_check"], self.clock.now + 15)

    async def test_product_numbers_are_private_to_each_user(self):
        await self.service.add_link(OWNER, "boss", A)
        await self.service.add_link(OWNER, "boss", B)
        await self.service.add_link(42, "new-user", C)
        owner_tracks = await self.service.list_tracks(OWNER)
        user_tracks = await self.service.list_tracks(42)
        self.assertEqual([t["track_id"] for t in owner_tracks], [1, 2])
        self.assertEqual([t["track_id"] for t in user_tracks], [3])
        # The first item in each user's list is addressed as #1, regardless of its DB ID.
        self.assertTrue(await self.service.set_target_number(42, 1, 250.0))
        self.assertEqual((await self.service.list_tracks(42))[0]["target_price"], 250.0)
        self.assertTrue(await self.service.set_target_number(OWNER, 1, 500.0))
        self.assertEqual((await self.service.list_tracks(OWNER))[0]["target_price"], 500.0)
        self.assertTrue(await self.service.remove_track_number(42, 1))
        self.assertEqual(await self.db.count_tracks(42), 0)
        self.assertEqual(await self.db.count_tracks(OWNER), 2)

    async def test_user_product_numbers_reindex_after_removal(self):
        for url in (A, B):
            await self.service.add_link(77, "u", url)
        self.assertTrue(await self.service.remove_track_number(77, 1))
        tracks = await self.service.list_tracks(77)
        self.assertEqual([t["url"] for t in tracks], [B])
        self.assertTrue(await self.service.set_target_number(77, 1, 123.0))
        self.assertEqual((await self.service.list_tracks(77))[0]["target_price"], 123.0)

    async def test_removing_last_tracker_deletes_product(self):
        await self.service.add_link(2, "u", A)
        await self.service.add_link(3, "v", A)
        track_u = (await self.service.list_tracks(2))[0]["track_id"]
        track_v = (await self.service.list_tracks(3))[0]["track_id"]
        self.assertFalse(await self.service.remove_track(3, track_u))  # not your track
        self.assertTrue(await self.service.remove_track(2, track_u))
        self.assertIsNotNone(await self.db.get_product_by_url(A))
        self.assertTrue(await self.service.remove_track(3, track_v))
        self.assertIsNone(await self.db.get_product_by_url(A))

    async def test_rate_limit_for_non_owners(self):
        statuses = [(await self.service.add_link(2, "u", "http://localhost/x")).status for _ in range(8)]
        self.assertEqual(statuses[:6], ["invalid"] * 6)
        self.assertEqual(statuses[6:], ["rate_limited"] * 2)
        self.assertEqual((await self.service.add_link(OWNER, "boss", "http://localhost/x")).status, "invalid")
        self.clock.advance(61)
        self.assertEqual((await self.service.add_link(2, "u", "http://localhost/x")).status, "invalid")

    async def test_payment_is_idempotent(self):
        self.assertTrue(await self.service.record_payment(2, "charge-1", 150))
        self.assertFalse(await self.service.record_payment(2, "charge-1", 150))
        self.assertEqual((await self.service.stats())["stars"], 150)


if __name__ == "__main__":
    unittest.main()
