import tempfile
import time
import unittest
from types import SimpleNamespace

from mm.api import Book, Level, Market
from mm.bot import BotConfig, PaperBot
from mm.paper import PaperExchange
from mm.risk import MarkoutTracker, UnrestTracker, markout_cents, plan_rotation
from mm.strategy import StrategyConfig, make_quote


def mk_market(slug, cid=None):
    return Market(cid or "c-" + slug, slug.replace("-", " "), slug, "Y-" + slug, "N-" + slug,
                  "2030-01-01T00:00:00Z", 10.0, 20, 4.0, 0.01, 5, False)


def mk_book(bid, ask, tick=0.01):
    return Book("Y", [Level(bid, 500)], [Level(ask, 500)], tick, 5)


class FakeAPI:
    def __init__(self, books):
        self._books = books

    def books(self, tokens):
        return {t: self._books[t] for t in tokens if t in self._books}

    def trades(self, cid, limit=100):
        return []


class MarkoutTests(unittest.TestCase):
    def test_sign(self):
        self.assertAlmostEqual(markout_cents("bid", 0.30, 0.28), -2.0)  # bought, price fell: bad
        self.assertAlmostEqual(markout_cents("ask", 0.54, 0.57), -3.0)  # sold, price rose: bad
        self.assertAlmostEqual(markout_cents("ask", 0.54, 0.52), 2.0)

    def test_tracker_horizons(self):
        t = MarkoutTracker()
        t.add("m", "bid", 0.30, 100, ts=1000)
        self.assertEqual(t.update("m", 0.29, 1030), [])
        got = t.update("m", 0.29, 1061)
        self.assertEqual([(h, round(c, 2)) for _, h, c in got], [(60, -1.0)])
        got = t.update("m", 0.32, 1301)
        self.assertEqual([(h, round(c, 2)) for _, h, c in got], [(300, 2.0)])
        self.assertEqual(t.pending, [])


class UnrestTests(unittest.TestCase):
    def test_range_window(self):
        u = UnrestTracker(window=600)
        u.add("m", 0.50, 0)
        u.add("m", 0.53, 100)
        self.assertAlmostEqual(u.range("m"), 0.03)
        u.add("m", 0.51, 300)
        self.assertAlmostEqual(u.add("m", 0.52, 800), 0.01)  # the 0.50 and 0.53 points dropped out

    def test_quote_widens_and_stops_joining(self):
        b = mk_book(0.49, 0.51)
        cfg = StrategyConfig(spread_frac=0.25, vol_factor=0.5, join_best=True, max_position=100)
        calm = make_quote(b, 0, 4.0, 20, cfg, recent_range=0.0)
        self.assertEqual((calm.bid, calm.ask), (0.50, 0.51))  # 2-tick spread: join/improve
        wild = make_quote(b, 0, 4.0, 20, cfg, recent_range=0.06)
        self.assertLessEqual(wild.bid, 0.47)  # 0.5 * 6c = 3c away from the mid
        self.assertGreaterEqual(wild.ask, 0.53)
        self.assertAlmostEqual(wild.dist, 0.03)
        huge = make_quote(b, 0, 4.0, 20, cfg, recent_range=0.40)
        self.assertAlmostEqual(huge.dist, 0.08)  # capped at twice the reward band


class RotationTests(unittest.TestCase):
    def test_plan(self):
        A, B, C, D, E = (SimpleNamespace(slug=x) for x in "ABCDE")
        keep, removed, added = plan_rotation(
            [A, B, C], [B, D, E], target=3,
            has_position=lambda m: m.slug == "A",  # A dropped out of the ranking but still has a position
            blocked=lambda m: m.slug == "D",
        )
        self.assertEqual([m.slug for m in keep], ["A", "B"])
        self.assertEqual([m.slug for m in removed], ["C"])
        self.assertEqual([m.slug for m in added], ["E"])  # D is blocked


class BotRiskTests(unittest.TestCase):
    def make_bot(self, books, markets, **cfg):
        d = tempfile.mkdtemp()
        ex = PaperExchange(1000)
        bot = PaperBot(FakeAPI(books), markets, ex, StrategyConfig(order_size=30, max_position=400, vol_factor=0),
                       BotConfig(log_dir=d, print_every=10**9, **cfg))
        return bot, ex

    def test_loss_limit_blocks_and_only_exits(self):
        m = mk_market("hegseth")
        bot, ex = self.make_bot({m.yes_token: mk_book(0.49, 0.51)}, [m], max_loss=3.0)
        ps = ex.p(m.condition_id)
        ps.yes, ps.avg = 200, 0.56  # bought at 56c, now 50c: -12 dollar open
        bot.tick()
        self.assertGreater(ps.blocked_until, time.time())
        self.assertEqual(ps.block_reason, "verlies")
        self.assertIsNone(ps.bid)  # no buying more
        self.assertIsNotNone(ps.ask)  # but still trying to sell out
        self.assertLessEqual(ps.ask.size, 200)
        self.assertEqual(bot.live_markets[m.slug]["status"], "geblokkeerd")

    def test_block_expires_and_counts_from_there(self):
        m = mk_market("x")
        bot, ex = self.make_bot({m.yes_token: mk_book(0.49, 0.51)}, [m], max_loss=3.0)
        ps = ex.p(m.condition_id)
        ps.realized, ps.blocked_until, ps.block_reason = -8.0, time.time() - 1, "verlies"
        bot.tick()
        self.assertEqual(ps.blocked_until, 0.0)  # old loss no longer counts
        self.assertAlmostEqual(ps.loss_base, -8.0)
        self.assertIsNotNone(ps.bid)

    def test_toxic_fills_block(self):
        m = mk_market("toxic")
        bot, ex = self.make_bot({m.yes_token: mk_book(0.49, 0.51)}, [m], toxic_cents=1.0, toxic_min_fills=5)
        ps = ex.p(m.condition_id)
        for _ in range(5):
            ps.add_markout(300, -2.0, 30)
        bot.tick()
        self.assertEqual(ps.block_reason, "giftige fills")
        self.assertIsNone(ps.bid)
        self.assertIsNone(ps.ask)  # flat: nothing to exit

    def test_apply_rotation_keeps_positions(self):
        a, b, c = mk_market("a"), mk_market("b"), mk_market("c")
        books = {x.yes_token: mk_book(0.49, 0.51) for x in (a, b, c)}
        bot, ex = self.make_bot(books, [a, b], target_markets=2)
        ex.p(a.condition_id).yes = 50  # a has a position: must stay
        bot.tick()
        removed, added = bot.apply_rotation([c], time.time())
        self.assertEqual([m.slug for m in bot.markets], ["a", "c"])
        self.assertEqual([m.slug for m in removed], ["b"])
        self.assertIsNone(ex.p(b.condition_id).bid)  # orders of b cancelled


if __name__ == "__main__":
    unittest.main()
