"""Plan, trial, and founding-offer rules introduced by the button UI."""
import os
import sqlite3
import tempfile
import unittest

from bot.service import PLAN_PRO, PLAN_ULTRA
from bot.database import Database
from tests.helpers import make_world


class SubscriptionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.settings, self.clock, self.fetch, self.db, self.service = make_world(
            pro_link_limit=10, pro_interval=1200, pro_stars=50,
            ultra_link_limit=25, ultra_interval=600, ultra_stars=120,
            founding_buyer_limit=1, founding_pro_stars=25,
        )

    async def asyncTearDown(self):
        self.db.close()

    async def test_trial_is_one_time_pro_without_payment(self):
        until = await self.service.activate_trial(31)
        self.assertIsNotNone(until)
        info = await self.service.plan_info(31)
        self.assertEqual((info["plan"], info["limit"], info["interval"]), (PLAN_PRO, 10, 1200))
        self.assertIsNone(await self.service.activate_trial(31))

    async def test_ultra_purchase_spec_and_activation(self):
        plan, stars, kind = self.service.purchase_spec(PLAN_ULTRA)
        self.assertEqual((plan, stars, kind), (PLAN_ULTRA, 120, "purchase"))
        until = await self.service.activate_plan(32, plan, self.settings.subscription_days, kind)
        self.assertGreater(until, self.clock.now)
        info = await self.service.plan_info(32)
        self.assertEqual((info["plan"], info["limit"], info["interval"]), (PLAN_ULTRA, 25, 600))

    async def test_founding_offer_is_limited_and_single_use(self):
        self.assertTrue((await self.service.offer_info(33))["founding_available"])
        plan, stars, kind = self.service.purchase_spec("founding")
        self.assertEqual((plan, stars, kind), (PLAN_PRO, 25, "founding"))
        self.assertTrue(await self.service.record_payment(33, "founding-charge", stars, plan, kind))
        await self.service.activate_plan(33, plan, self.settings.subscription_days, kind)
        self.assertFalse((await self.service.offer_info(33))["founding_available"])
        self.assertFalse((await self.service.offer_info(34))["founding_available"])


class SubscriptionMigrationTests(unittest.TestCase):
    def test_existing_paid_user_is_preserved_as_legacy_plan(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE users (user_id INTEGER PRIMARY KEY, username TEXT, premium_until INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL)")
            conn.execute("INSERT INTO users VALUES(7, 'saved', 9999999999, 1)")
            conn.commit(); conn.close()
            db = Database(path)
            row = db._conn.execute("SELECT subscription_plan, trial_used, founding_used FROM users WHERE user_id=7").fetchone()
            self.assertEqual(tuple(row), ("premium", 0, 0))
            db.close()
        finally:
            if os.path.exists(path): os.unlink(path)


if __name__ == "__main__":
    unittest.main()
