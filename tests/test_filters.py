import unittest
from datetime import datetime, timedelta, timezone

from mm.api import Market
from mm.scanner import Filters, static_reject


def mk(q, days=200, game=None, rate=10.0):
    end = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    return Market("c", q, "s", "y", "n", end, rate, 20, 4.5, 0.01, 5, False, game)


class FilterTests(unittest.TestCase):
    def test_date_windows(self):
        f = Filters()
        for q in [
            "Will GPT Astra 6.1+ be released between November 9 and November 15, 2026?",
            "Will the Fed cut rates on December 10?",
            "Pete Hegseth out as Secretary of Defense by November 30?",
        ]:
            self.assertEqual(static_reject(mk(q), f), "datumvenster", q)

    def test_keeps_long_dated(self):
        self.assertIsNone(static_reject(mk("Will Anthropic raise between $85B and $95B in its IPO?"), Filters()))
        self.assertIsNone(static_reject(mk("Will the 2-year Treasury yield dip below 4.50% before 2027?"), Filters()))

    def test_ending_soon_and_sports(self):
        self.assertIn("eindigt", static_reject(mk("Will X happen?", days=10), Filters()))
        self.assertEqual(static_reject(mk("Lakers vs. Celtics"), Filters()), "sport/live-event")
        self.assertEqual(static_reject(mk("Who wins?", game="2026-11-05T00:00:00Z"), Filters()), "sport/live-event")
        self.assertIsNone(static_reject(mk("Lakers vs. Celtics"), Filters(exclude_sports_live=False)))


if __name__ == "__main__":
    unittest.main()
