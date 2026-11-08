import logging
import os
import tempfile
import unittest
from dataclasses import replace

from bot.checklog import (
    CheckRecord,
    MonitorStats,
    describe_change,
    format_check,
    format_summary,
    safe_url,
    store_name,
)
from bot.logging_setup import RedactingFormatter, redact, setup_logging
from tests.helpers import make_settings

FAKE_TOKEN = "1234567890:AAEexampleExampleExampleExample_12345"


def record(**kw) -> CheckRecord:
    base = dict(
        product_id=7, store="Amazon", url="https://www.amazon.in/dp/B09B8V1LZ3", started_at=1_000_000.0,
        finished_at=1_000_000.9, scheduled_for=1_000_000.0, interval=15, since_prev=15.1, queue_wait=0.0,
        fetch_seconds=0.9, http_status=200, success=True, outcome="SUCCESS", price=92990.0, prev_price=92990.0,
        change="±₹0", trackers=1,
    )
    base.update(kw)
    return CheckRecord(**base)


class NamingTests(unittest.TestCase):
    def test_store_names(self):
        cases = {
            "www.amazon.in": "Amazon", "amazon.com": "Amazon", "www.flipkart.com": "Flipkart",
            "shop.example.co.uk": "Example", "shop.example.com": "Example", "": "Unknown",
        }
        for host, expected in cases.items():
            with self.subTest(host=host):
                self.assertEqual(store_name(host), expected)

    def test_safe_url_drops_query_fragment_and_credentials(self):
        self.assertEqual(
            safe_url("https://user:pw@shop.com/p/1?token=abc&session=xyz#frag"), "https://shop.com/p/1"
        )
        long = safe_url("https://shop.com/" + "a" * 200)
        self.assertTrue(long.endswith("…"))
        self.assertLess(len(long), 100)


class DescribeChangeTests(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(describe_change(92990, 92490, "INR"), ("PRICE DROP ₹500 (-0.5%)", "-₹500"))
        self.assertEqual(describe_change(1000, 1200, "INR"), ("PRICE RISE ₹200 (+20.0%)", "+₹200"))
        self.assertEqual(describe_change(1000, 1000, "INR"), ("SUCCESS", "±₹0"))
        self.assertEqual(describe_change(None, 1000, "INR"), ("SUCCESS", "n/a"))


class FormatTests(unittest.TestCase):
    def test_success_line_matches_requested_shape(self):
        line = format_check(record())
        self.assertRegex(line, r"^\[\d\d:\d\d:\d\d\] Amazon \| ₹92,990 \| SUCCESS \| #7 ")
        for part in ("interval=15s", "actual=15.1s", "late=+0.0s", "fetch=0.90s", "http=200", "prev=₹92,990"):
            self.assertIn(part, line)

    def test_drop_and_blocked_lines(self):
        drop = format_check(record(price=92490.0, outcome="PRICE DROP ₹500 (-0.5%)", change="-₹500"))
        self.assertIn("Amazon | ₹92,490 | PRICE DROP ₹500", drop)
        blocked = format_check(record(success=False, outcome="BLOCKED", error="HTTP 403 blocked by the store",
                                      http_status=403, price=None, retry_in=300.0))
        self.assertIn("Amazon | BLOCKED | HTTP 403 blocked by the store", blocked)
        self.assertIn("http=403", blocked)
        self.assertIn("retry_in=300s", blocked)
        self.assertNotIn("prev=", blocked)


class StatsTests(unittest.TestCase):
    def test_counts_average_and_summary(self):
        stats = MonitorStats(started_at=999_000.0)
        stats.record(record(fetch_seconds=1.0))
        stats.record(record(fetch_seconds=3.0, started_at=1_000_015.0, scheduled_for=1_000_012.0))  # 3 s late
        stats.record(record(success=False, outcome="BLOCKED", error="HTTP 403", fetch_seconds=2.0))
        self.assertEqual((stats.success, stats.failed, stats.blocked, stats.late), (2, 1, 1, 1))
        self.assertAlmostEqual(stats.avg_fetch, 2.0)
        text = "\n".join(format_summary(stats, 1_000_100.0, {"id": 7, "domain": "www.amazon.in", "next_check": 1_000_130.0}))
        for expected in ("Successful checks: 2", "Failed checks: 1 (blocked by store: 1", "Amazon #7",
                         "in 30s", "Average fetch duration: 2.00s"):
            self.assertIn(expected, text)

    def test_empty_summary_does_not_crash(self):
        text = "\n".join(format_summary(MonitorStats(started_at=1.0), 10.0, None))
        self.assertIn("none yet", text)
        self.assertIn("none (no active products)", text)


class RedactionTests(unittest.TestCase):
    def test_redact_masks_tokens_secrets_and_proxy_passwords(self):
        text = f"GET https://api.telegram.org/bot{FAKE_TOKEN}/getMe via http://user:hunter2@proxy.example:8080"
        out = redact(text, ["hunter2"])
        self.assertNotIn(FAKE_TOKEN, out)
        self.assertNotIn("hunter2", out)
        self.assertIn("http://***@proxy.example:8080", out)

    def test_formatter_masks_secrets_inside_tracebacks(self):
        fmt = RedactingFormatter("%(message)s", secrets=["SECRET-VALUE"])
        try:
            raise RuntimeError("boom SECRET-VALUE")
        except RuntimeError:
            import sys

            rec = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed", None, sys.exc_info())
        self.assertNotIn("SECRET-VALUE", fmt.format(rec))

    def test_setup_logging_writes_files_without_the_token(self):
        root = logging.getLogger()
        saved = (list(root.handlers), root.level)
        checks = logging.getLogger("pricecheck")
        saved_checks = (list(checks.handlers), checks.propagate)
        with tempfile.TemporaryDirectory() as tmp:
            try:
                settings = replace(make_settings(), bot_token=FAKE_TOKEN, log_dir=tmp)
                setup_logging(settings)
                logging.getLogger("bot.test").error("token is %s", FAKE_TOKEN)
                checks.info("[10:00:00] Amazon | ₹1,000 | SUCCESS | #1 token=%s", FAKE_TOKEN)
                for handler in root.handlers + checks.handlers:
                    handler.flush()
                for name in ("bot.log", "price_checks.log"):
                    with open(os.path.join(tmp, name), encoding="utf-8") as fh:
                        content = fh.read()
                    self.assertNotIn(FAKE_TOKEN, content)
                    self.assertIn("REDACTED", content)
                with open(os.path.join(tmp, "price_checks.log"), encoding="utf-8") as fh:
                    self.assertIn("₹1,000", fh.read())
            finally:
                for handler in root.handlers + checks.handlers:
                    handler.close()
                root.handlers[:] = saved[0]
                root.setLevel(saved[1])
                checks.handlers[:] = saved_checks[0]
                checks.propagate = saved_checks[1]


if __name__ == "__main__":
    unittest.main()
