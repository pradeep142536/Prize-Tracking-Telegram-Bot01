"""Focused coverage for the button-first UI's safe, pure building blocks."""
import unittest

from bot.handlers import products, target_screen
from tests.helpers import make_world


class ButtonUiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.settings, self.clock, self.fetch, self.db, self.service = make_world()
        self.url = "https://shop.example.com/p/ui-product"
        self.fetch.prices[self.url] = 1000.0

    async def asyncTearDown(self):
        self.db.close()

    async def test_preview_is_non_mutating_then_adds_on_confirmation(self):
        preview = await self.service.preview_link(22, "kid", self.url)
        self.assertEqual(preview.status, "ready")
        self.assertEqual(await self.db.count_tracks(22), 0)
        self.assertEqual((await self.service.add_link(22, "kid", preview.url)).status, "added")

    async def test_product_card_uses_internal_callback_id_and_safe_details(self):
        await self.service.add_link(22, "kid", self.url)
        track = (await self.service.list_tracks(22))[0]
        text, markup = products([track], 0, self.settings)
        callbacks = [button.callback_data for row in markup.inline_keyboard for button in row if button.callback_data]
        self.assertIn("Product 1 of 1", text)
        self.assertIn(f"details:{track['track_id']}:0", callbacks)
        self.assertEqual([len(row) for row in markup.inline_keyboard[:4]], [2, 2, 1, 1])
        self.assertEqual([button.text for button in markup.inline_keyboard[0]], ["🎯 Set Target", "🛒 Buy Now"])
        self.assertEqual([button.text for button in markup.inline_keyboard[1]], ["📊 Details", "🗑️ Remove"])
        self.assertEqual(markup.inline_keyboard[2][0].text, "🛍️ Add Product")
        self.assertEqual(markup.inline_keyboard[3][0].text, "🏠 Home")
        self.assertIsNone(await self.service.track_details(23, track["track_id"]))
        self.assertIsNotNone(await self.service.track_details(22, track["track_id"]))

    async def test_target_presets_are_dynamic(self):
        await self.service.add_link(22, "kid", self.url)
        track = await self.service.track_details(22, (await self.service.list_tracks(22))[0]["track_id"])
        text, markup = target_screen(track, 0)
        labels = [button.text for row in markup.inline_keyboard for button in row]
        self.assertIn("Current price", text)
        self.assertTrue(any("₹950" in label for label in labels))
        self.assertTrue(any("₹900" in label for label in labels))


if __name__ == "__main__":
    unittest.main()
