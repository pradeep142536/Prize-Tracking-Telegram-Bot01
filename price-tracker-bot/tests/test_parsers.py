import json
import unittest

from bot.parsers import extract_product, looks_blocked, parse_price


class ParsePriceTests(unittest.TestCase):
    def test_formats(self):
        cases = {
            "₹1,29,999.00": 129999.0,
            "₹1,299": 1299.0,
            "Rs. 499": 499.0,
            "$1,299.50": 1299.5,
            "1.299,50 €": 1299.5,
            "12,34": 12.34,
            "99,999": 99999.0,
            "499": 499.0,
            "1.299.000": 1299000.0,
            499: 499.0,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(parse_price(raw), expected)

    def test_rejects_junk(self):
        for raw in ("", "free", None, 0, -5, "₹0"):
            with self.subTest(raw=raw):
                self.assertIsNone(parse_price(raw))


class ExtractProductTests(unittest.TestCase):
    def test_json_ld_picks_cheapest_offer(self):
        data = {
            "@context": "https://schema.org",
            "@type": "Product",
            "name": "Noise Buds",
            "offers": [
                {"@type": "Offer", "price": "2499", "priceCurrency": "INR"},
                {"@type": "Offer", "price": "1999.00", "priceCurrency": "INR"},
            ],
        }
        html = f'<html><head><script type="application/ld+json">{json.dumps(data)}</script></head></html>'
        info = extract_product(html, "https://shop.example.in/p/1")
        self.assertEqual((info.title, info.price, info.currency), ("Noise Buds", 1999.0, "INR"))

    def test_json_ld_inside_graph_and_aggregate_offer(self):
        data = {
            "@graph": [
                {"@type": "WebPage"},
                {"@type": "Product", "name": "Lamp", "offers": {"@type": "AggregateOffer", "lowPrice": 15.5, "priceCurrency": "USD"}},
            ]
        }
        html = f'<script type="application/ld+json">{json.dumps(data)}</script>'
        info = extract_product(html, "https://x.com/l")
        self.assertEqual((info.title, info.price, info.currency), ("Lamp", 15.5, "USD"))

    def test_broken_json_ld_falls_through_to_meta(self):
        html = (
            '<html><head><title>Fallback Title</title>'
            '<script type="application/ld+json">{not json</script>'
            '<meta property="product:price:amount" content="799">'
            '<meta property="product:price:currency" content="INR"></head></html>'
        )
        info = extract_product(html, "https://store.in/x")
        self.assertEqual((info.title, info.price, info.currency), ("Fallback Title", 799.0, "INR"))

    def test_amazon_selector(self):
        html = (
            '<span id="productTitle"> Echo Dot  (5th Gen) </span>'
            '<div id="corePrice_feature_div"><span class="a-price"><span class="a-offscreen">₹4,499.00</span></span></div>'
        )
        info = extract_product(html, "https://www.amazon.in/dp/B09B8V1LZ3")
        self.assertEqual((info.title, info.price, info.currency), ("Echo Dot (5th Gen)", 4499.0, "INR"))

    def test_generic_skips_struck_through_mrp(self):
        html = (
            "<title>Shoes</title>"
            '<span class="mrp-price">₹2,999</span><span class="selling-price">₹1,799</span>'
        )
        info = extract_product(html, "https://shoes.example.in/a")
        self.assertEqual(info.price, 1799.0)

    def test_no_price_returns_none(self):
        self.assertIsNone(extract_product("<html><title>Hi</title><p>No price here</p></html>", "https://a.com"))
        self.assertIsNone(extract_product("", "https://a.com"))


class BlockedPageTests(unittest.TestCase):
    def test_detects_captcha_pages(self):
        amazon = "<html><title>Robot Check</title><form action='/errors/validateCaptcha'>Enter the characters you see below</form></html>"
        self.assertTrue(looks_blocked(amazon))
        self.assertFalse(looks_blocked("<html><title>Echo Dot</title><span class='a-price'>₹4,499</span></html>"))
        self.assertFalse(looks_blocked(""))


if __name__ == "__main__":
    unittest.main()
