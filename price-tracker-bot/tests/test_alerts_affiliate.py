import unittest

from bot.affiliate import affiliate_url
from bot.alerts import format_drop_message, format_price, should_alert
from tests.helpers import make_settings


class FormatPriceTests(unittest.TestCase):
    def test_indian_grouping(self):
        self.assertEqual(format_price(129999), "₹1,29,999")
        self.assertEqual(format_price(999), "₹999")
        self.assertEqual(format_price(1499.5), "₹1,499.50")
        self.assertEqual(format_price(100000), "₹1,00,000")

    def test_other_currencies(self):
        self.assertEqual(format_price(1234.5, "USD"), "$1,234.50")
        self.assertEqual(format_price(10, "EUR"), "€10")


class ShouldAlertTests(unittest.TestCase):
    def test_plain_drop_rules(self):
        self.assertTrue(should_alert(1000, 900, None))
        self.assertFalse(should_alert(1000, 1000, None))
        self.assertFalse(should_alert(1000, 1100, None))
        self.assertFalse(should_alert(1000, 998, None))  # 0.2% noise
        self.assertFalse(should_alert(None, 900, None))  # nothing to compare with

    def test_target_rules(self):
        self.assertFalse(should_alert(1000, 950, 900))  # dropped but still above target
        self.assertTrue(should_alert(950, 880, 900))  # crossed the target
        self.assertTrue(should_alert(880, 850, 900))  # kept falling below target
        self.assertFalse(should_alert(880, 880, 900))  # unchanged
        self.assertFalse(should_alert(850, 880, 900))  # rose, still below target


class MessageTests(unittest.TestCase):
    def test_message_escapes_html_and_shows_percent(self):
        text = format_drop_message("A <b>&</b> B", 2000, 1500, "INR", target=1600)
        self.assertIn("A &lt;b&gt;&amp;&lt;/b&gt; B", text)
        self.assertNotIn("<a href=", text)
        self.assertNotIn("https://", text)
        self.assertIn("-25%", text)
        self.assertIn("Reached your target", text)


class AffiliateTests(unittest.TestCase):
    def test_amazon_and_flipkart_tags(self):
        s = make_settings(amazon_tag="me-21", flipkart_affid="meflip")
        self.assertEqual(affiliate_url("https://www.amazon.in/dp/B09B8V1LZ3", s), "https://www.amazon.in/dp/B09B8V1LZ3?tag=me-21")
        self.assertEqual(
            affiliate_url("https://www.flipkart.com/p/itm?pid=ABC", s),
            "https://www.flipkart.com/p/itm?pid=ABC&affid=meflip",
        )


    def test_amazon_buy_now_url_uses_configured_tracking_id(self):
        s = make_settings(amazon_tag="inventions0e-21")
        generated = affiliate_url("https://www.amazon.in/dp/B0GSFGSVZ4", s)
        self.assertIn("tag=inventions0e-21", generated)
        self.assertIn("/dp/B0GSFGSVZ4", generated)

    def test_existing_tag_is_replaced(self):
        s = make_settings(amazon_tag="me-21")
        self.assertEqual(affiliate_url("https://www.amazon.in/dp/B09B8V1LZ3?tag=old-21", s), "https://www.amazon.in/dp/B09B8V1LZ3?tag=me-21")

    def test_template_and_passthrough(self):
        s = make_settings(affiliate_template="https://go.example/?u={url}")
        self.assertEqual(affiliate_url("https://shop.com/a?b=1", s), "https://go.example/?u=https%3A%2F%2Fshop.com%2Fa%3Fb%3D1")
        self.assertEqual(affiliate_url("https://shop.com/a", make_settings()), "https://shop.com/a")


if __name__ == "__main__":
    unittest.main()
