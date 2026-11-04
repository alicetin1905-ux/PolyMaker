import unittest

from mm.api import Book, Level
from mm.paper import PaperExchange
from mm.rewards import order_score, our_share, q_min
from mm.strategy import StrategyConfig, make_quote


def book(bids, asks, tick=0.01):
    return Book("YES", [Level(p, s) for p, s in bids], [Level(p, s) for p, s in asks], tick, 5)


class RewardTests(unittest.TestCase):
    def test_order_score(self):
        self.assertAlmostEqual(order_score(0.50, 100, 0.50, 3.0, 20), 100)
        self.assertAlmostEqual(order_score(0.485, 100, 0.50, 3.0, 20), 25)  # half the band -> 1/4
        self.assertEqual(order_score(0.47, 100, 0.50, 3.0, 20), 0)  # at the edge
        self.assertEqual(order_score(0.49, 10, 0.50, 3.0, 20), 0)  # below min size

    def test_q_min(self):
        self.assertEqual(q_min(90, 0, 0.5), 30)  # single-sided allowed at /3 in the middle
        self.assertEqual(q_min(90, 0, 0.05), 0)  # extremes require two sides
        self.assertEqual(q_min(40, 60, 0.5), 40)

    def test_share(self):
        b = book([(0.49, 100)], [(0.51, 100)])
        s = our_share(0.49, 100, 0.51, 100, b, 0.50, 3.0, 20)
        self.assertAlmostEqual(s, 0.5)


class StrategyTests(unittest.TestCase):
    def test_symmetric_and_skew(self):
        b = book([(0.48, 100)], [(0.52, 100)])
        cfg = StrategyConfig(order_size=50, spread_frac=0.5, max_position=100)
        q = make_quote(b, 0, 4.0, 20, cfg)
        self.assertEqual((q.bid, q.ask), (0.48, 0.52))
        q_long = make_quote(b, 50, 4.0, 20, cfg)
        self.assertLess(q_long.bid, q.bid)
        self.assertLessEqual(q_long.ask, q.ask)
        q_max = make_quote(b, 100, 4.0, 20, cfg)
        self.assertIsNone(q_max.bid)
        self.assertIsNotNone(q_max.ask)

    def test_never_crosses(self):
        b = book([(0.49, 100)], [(0.50, 100)])
        q = make_quote(b, -100 + 1, 6.0, 20, StrategyConfig(spread_frac=0.1, max_position=100, skew=5))
        self.assertLess(q.bid, 0.50)
        self.assertGreater(q.ask, 0.49)

    def test_size_at_least_reward_min(self):
        b = book([(0.48, 100)], [(0.52, 100)])
        q = make_quote(b, 0, 4.0, 200, StrategyConfig(order_size=50))
        self.assertEqual(q.bid_size, 200)


def trade(px, size, ts, token="YES", tx="t"):
    return {"price": px, "size": size, "timestamp": ts, "token_id": token, "transaction_hash": tx}


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.ex = PaperExchange(1000)
        self.ex.on_trades("m", [], "YES", 0)  # initialize

    def test_queue_then_fill(self):
        self.ex.set_order("m", "bid", 0.40, 50, book_size_at_price=30, now=100)
        self.ex.on_trades("m", [trade(0.40, 20, 101, tx="a")], "YES", 101)
        self.assertEqual(self.ex.p("m").yes, 0)  # still behind 10 in queue
        self.ex.on_trades("m", [trade(0.40, 25, 102, tx="b")], "YES", 102)
        self.assertAlmostEqual(self.ex.p("m").yes, 15)
        self.assertAlmostEqual(self.ex.cash, 1000 - 15 * 0.40)

    def test_trade_through_and_no_token(self):
        self.ex.set_order("m", "ask", 0.60, 50, 0, now=100)
        # a NO trade at 0.35 == YES at 0.65 -> through our ask
        self.ex.on_trades("m", [trade(0.35, 80, 101, token="NO", tx="c")], "YES", 101)
        ps = self.ex.p("m")
        self.assertAlmostEqual(ps.no, 50)
        self.assertAlmostEqual(self.ex.cash, 1000 - 50 * 0.40)

    def test_round_trip_merges_and_profits(self):
        self.ex.set_order("m", "bid", 0.40, 50, 0, now=100)
        self.ex.set_order("m", "ask", 0.44, 50, 0, now=100)
        self.ex.on_trades("m", [trade(0.39, 50, 101, tx="d"), trade(0.45, 50, 102, tx="e")], "YES", 102)
        ps = self.ex.p("m")
        self.assertAlmostEqual(ps.yes, 0)
        self.assertAlmostEqual(ps.no, 0)
        self.assertAlmostEqual(self.ex.cash, 1000 + 50 * 0.04)

    def test_old_trades_ignored_and_dedup(self):
        self.ex.set_order("m", "bid", 0.40, 50, 0, now=100)
        self.ex.on_trades("m", [trade(0.39, 50, 99, tx="old")], "YES", 101)
        self.assertEqual(self.ex.p("m").yes, 0)
        t = trade(0.39, 10, 101, tx="x")
        self.ex.on_trades("m", [t, dict(t)], "YES", 101)
        self.ex.on_trades("m", [t], "YES", 102)
        self.assertAlmostEqual(self.ex.p("m").yes, 10)

    def test_book_cross_fill(self):
        self.ex.set_order("m", "bid", 0.40, 50, 0, now=100)
        self.ex.on_book("m", 0.38, 0.40, 101)
        self.assertAlmostEqual(self.ex.p("m").yes, 50)
        self.assertIsNone(self.ex.p("m").bid)

    def test_cash_limit(self):
        ex = PaperExchange(10)
        ex.set_order("m", "bid", 0.50, 50, 0, now=1)
        self.assertIsNone(ex.p("m").bid)


if __name__ == "__main__":
    unittest.main()
