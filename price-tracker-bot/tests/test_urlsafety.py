import unittest

from bot.errors import UnsafeURL
from bot.urlsafety import extract_urls, normalize_url


class NormalizeTests(unittest.TestCase):
    def test_amazon_is_reduced_to_asin(self):
        url = "https://www.amazon.in/Some-Long-Title/dp/b09b8v1lz3/ref=sr_1_1?crid=XYZ&tag=other-21&th=1"
        self.assertEqual(normalize_url(url), "https://www.amazon.in/dp/B09B8V1LZ3")

    def test_flipkart_keeps_only_pid(self):
        url = "https://www.flipkart.com/phone/p/itm123?pid=MOBABC&lid=LST1&otracker=search"
        self.assertEqual(normalize_url(url), "https://www.flipkart.com/phone/p/itm123?pid=MOBABC")

    def test_tracking_params_removed_others_kept(self):
        self.assertEqual(
            normalize_url("shop.example.com/item?id=7&utm_source=x&fbclid=abc#frag"),
            "https://shop.example.com/item?id=7",
        )

    def test_trailing_punctuation_and_host_case(self):
        self.assertEqual(normalize_url("HTTPS://Shop.Example.com/a)."), "https://shop.example.com/a")

    def test_rejects_unsafe_or_invalid(self):
        bad = [
            "", "ftp://example.com/x", "http://localhost/admin", "http://127.0.0.1/", "https://10.0.0.5/x",
            "http://169.254.169.254/latest/meta-data", "https://[::1]/", "https://intranet.local/x",
            "https://user:pw@example.com/", "https://example.com:8080/", "https://nodot/", "https://" + "a" * 2100 + ".com",
        ]
        for url in bad:
            with self.subTest(url=url):
                with self.assertRaises(UnsafeURL):
                    normalize_url(url)


class ExtractUrlsTests(unittest.TestCase):
    def test_finds_links_in_text(self):
        text = "check https://a.com/x, and (https://b.com/y). also https://a.com/x"
        self.assertEqual(extract_urls(text), ["https://a.com/x", "https://b.com/y"])

    def test_no_links(self):
        self.assertEqual(extract_urls("hello there"), [])


if __name__ == "__main__":
    unittest.main()
