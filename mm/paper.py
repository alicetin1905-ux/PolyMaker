"""Paper exchange: simulated resting orders, fills from real trades, positions and P&L.

Fill model (deliberately conservative):
  * When we place an order we join the BACK of the queue at that price
    (queue_ahead = size already resting there).
  * A real trade at a price strictly better than ours fills us (we had priority).
  * A real trade at exactly our price first eats the queue ahead of us, then us.
  * If the book crosses our price (best ask <= our bid), we are filled.
Every order is expressed on the YES token. A YES ask is simulated as buying NO
at (1 - price); YES+NO pairs are merged back into $1 immediately.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

EPS = 1e-9


@dataclass
class Order:
    side: str  # "bid" (buy YES) or "ask" (sell YES == buy NO)
    price: float
    size: float
    queue_ahead: float
    placed_at: float
    filled: float = 0.0

    @property
    def remaining(self) -> float:
        return max(0.0, self.size - self.filled)


@dataclass
class Fill:
    ts: float
    market: str
    side: str
    price: float
    size: float
    reason: str
    realized: float = 0.0  # P&L locked in by this fill (closing part only)
    pos_after: float = 0.0  # net YES position after the fill (negative = NO)
    avg_after: float = 0.0  # average YES-equivalent entry price of the remaining position


def apply_fill(net: float, avg: float, side: str, price: float, qty: float) -> tuple[float, float, float]:
    """Average-cost bookkeeping in YES terms. Returns (new_net, new_avg, realized).

    A bid buys YES at `price`; an ask sells YES at `price` (economically: buys NO at 1-price).
    """
    d = qty if side == "bid" else -qty
    if abs(net) < EPS or (net > 0) == (d > 0):
        new = net + d
        return new, (abs(net) * avg + qty * price) / abs(new), 0.0
    close = min(qty, abs(net))
    realized = close * (price - avg) * (1 if net > 0 else -1)
    new = net + d
    if abs(new) < EPS:
        return 0.0, 0.0, realized
    return new, (avg if abs(new) <= abs(net) and (new > 0) == (net > 0) else price), realized


@dataclass
class Position:
    yes: float = 0.0
    no: float = 0.0
    bid: Order | None = None
    ask: Order | None = None
    rewards: float = 0.0
    seen_trades: set = field(default_factory=set)
    initialized: bool = False
    last_mid: float | None = None
    avg: float = 0.0  # average entry price of the net position (YES terms)
    realized: float = 0.0  # P&L locked in by closing trades
    slug: str = ""
    question: str = ""
    blocked_until: float = 0.0  # loss limit / toxic fills: only exit the position until then
    block_reason: str = ""
    loss_base: float = 0.0  # trading P&L when the market was (re)admitted; the loss limit counts from here
    mk: dict = field(default_factory=dict)  # markouts: {"60": [sum_cents*size, size, n], "300": [...]}

    def trading_pnl(self, mid: float | None) -> float:
        return self.realized + self.unrealized(mid)

    def add_markout(self, horizon: int, cents: float, size: float) -> None:
        a = self.mk.setdefault(str(horizon), [0.0, 0.0, 0])
        a[0] += cents * size
        a[1] += size
        a[2] += 1

    def markout(self, horizon: int) -> tuple[float | None, int]:
        """Size-weighted average markout in cents, and the number of fills measured."""
        a = self.mk.get(str(horizon))
        if not a or a[1] <= 0:
            return None, 0
        return a[0] / a[1], a[2]

    def unrealized(self, mid: float | None) -> float:
        net = self.yes - self.no
        return 0.0 if mid is None or abs(net) < EPS else net * (mid - self.avg)

    @property
    def net_yes(self) -> float:
        return self.yes - self.no


class PaperExchange:
    def __init__(self, bankroll: float, maker_fee: float = 0.0):
        self.start_cash = bankroll
        self.cash = bankroll
        self.maker_fee = maker_fee
        self.pos: dict[str, Position] = {}
        self.fills: list[Fill] = []
        self.needs_rebuild = False

    def p(self, cid: str) -> Position:
        return self.pos.setdefault(cid, Position())

    # ---- capital ---------------------------------------------------------
    def reserved(self, exclude: Order | None = None) -> float:
        r = 0.0
        for ps in self.pos.values():
            if ps.bid and ps.bid is not exclude:
                r += ps.bid.remaining * ps.bid.price
            if ps.ask and ps.ask is not exclude:
                r += max(0.0, ps.ask.remaining - ps.yes) * (1 - ps.ask.price)
        return r

    def free_cash(self, exclude: Order | None = None) -> float:
        return self.cash - self.reserved(exclude)

    # ---- orders ----------------------------------------------------------
    def set_order(self, cid: str, side: str, price: float | None, size: float, book_size_at_price: float, now: float) -> bool:
        """Place/replace/cancel our order. Returns True if the resting order changed."""
        ps = self.p(cid)
        cur: Order | None = getattr(ps, side)
        if price is None or size <= 0:
            setattr(ps, side, None)
            return cur is not None
        if cur and abs(cur.price - price) < EPS and cur.remaining >= 0.5 * size:
            return False  # keep queue position
        cost = size * price if side == "bid" else max(0.0, size - ps.yes) * (1 - price)
        if cost > self.free_cash(exclude=cur) + EPS:
            setattr(ps, side, None)
            return cur is not None
        setattr(ps, side, Order(side, price, size, queue_ahead=book_size_at_price, placed_at=now))
        return True

    # ---- fills -----------------------------------------------------------
    def _execute(self, cid: str, o: Order, qty: float, now: float, reason: str) -> None:
        if qty <= EPS:
            return
        ps = self.p(cid)
        fee = qty * self.maker_fee * min(o.price, 1 - o.price)
        net_before = ps.yes - ps.no
        if o.side == "bid":
            self.cash -= qty * o.price + fee
            ps.yes += qty
        else:
            self.cash -= qty * (1 - o.price) + fee
            ps.no += qty
        o.filled += qty
        _, ps.avg, realized = apply_fill(net_before, ps.avg, o.side, o.price, qty)
        ps.realized += realized - fee
        merged = min(ps.yes, ps.no)
        if merged > 0:
            ps.yes -= merged
            ps.no -= merged
            self.cash += merged
        self.fills.append(Fill(now, cid, o.side, o.price, qty, reason, realized - fee, ps.yes - ps.no, ps.avg))

    def on_trades(self, cid: str, trades: list[dict], yes_token: str, now: float) -> int:
        """Process real trades (from the data API) against our resting orders."""
        ps = self.p(cid)
        # collapse maker/taker rows of the same match into one print
        prints: dict[tuple, tuple[float, float, float]] = {}
        window: set = set()
        for t in trades:
            key = (t.get("transaction_hash"), t.get("token_id"), float(t["price"]))
            window.add(key)
            if key in ps.seen_trades:
                continue
            px = float(t["price"])
            yes_px = px if t.get("token_id") == yes_token else round(1 - px, 6)
            size, ts = float(t["size"]), float(t.get("timestamp", 0))
            prev = prints.get(key)
            if prev is None or size > prev[1]:
                prints[key] = (yes_px, size, ts)
        # the API only returns the latest trades, so remembering this window is enough (bounded memory)
        ps.seen_trades = window | set(prints.keys()) if len(ps.seen_trades) > 5000 else ps.seen_trades | window
        if not ps.initialized:
            ps.initialized = True  # ignore history from before we started
            return 0
        for yes_px, size, ts in sorted(prints.values(), key=lambda x: x[2]):
            for o in (ps.bid, ps.ask):
                if not o or o.remaining <= EPS or ts < o.placed_at:
                    continue
                better = yes_px < o.price - EPS if o.side == "bid" else yes_px > o.price + EPS
                at = abs(yes_px - o.price) < EPS
                if better:
                    self._execute(cid, o, min(size, o.remaining), now, "trade-through")
                elif at:
                    eat = min(size, o.queue_ahead)
                    o.queue_ahead -= eat
                    self._execute(cid, o, min(size - eat, o.remaining), now, "queue")
        return len(prints)

    def on_book(self, cid: str, best_bid: float | None, best_ask: float | None, now: float) -> None:
        ps = self.p(cid)
        if ps.bid and best_ask is not None and best_ask <= ps.bid.price + EPS:
            self._execute(cid, ps.bid, ps.bid.remaining, now, "book-cross")
        if ps.ask and best_bid is not None and best_bid >= ps.ask.price - EPS:
            self._execute(cid, ps.ask, ps.ask.remaining, now, "book-cross")
        for side in ("bid", "ask"):
            o = getattr(ps, side)
            if o and o.remaining <= EPS:
                setattr(ps, side, None)

    # ---- valuation -------------------------------------------------------
    def inventory_value(self, mids: dict[str, float]) -> float:
        v = 0.0
        for cid, ps in self.pos.items():
            m = mids.get(cid, ps.last_mid)
            if m is None:
                continue
            v += ps.yes * m + ps.no * (1 - m)
        return v

    def total_rewards(self) -> float:
        return sum(ps.rewards for ps in self.pos.values())

    def equity(self, mids: dict[str, float]) -> float:
        return self.cash + self.inventory_value(mids)

    # ---- persistence -----------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "saved_at": time.time(),
            "start_cash": self.start_cash,
            "cash": self.cash,
            "positions": {
                cid: {"yes": ps.yes, "no": ps.no, "rewards": ps.rewards, "last_mid": ps.last_mid,
                      "avg": ps.avg, "realized": ps.realized, "slug": ps.slug, "question": ps.question,
                      "blocked_until": ps.blocked_until, "block_reason": ps.block_reason,
                      "loss_base": ps.loss_base, "mk": ps.mk}
                for cid, ps in self.pos.items()
            },
        }

    def load_dict(self, d: dict) -> None:
        self.start_cash = d["start_cash"]
        self.cash = d["cash"]
        for cid, v in d.get("positions", {}).items():
            ps = self.p(cid)
            ps.yes, ps.no, ps.rewards, ps.last_mid = v["yes"], v["no"], v["rewards"], v.get("last_mid")
            ps.avg, ps.realized = v.get("avg", 0.0), v.get("realized", 0.0)
            ps.slug, ps.question = v.get("slug", ""), v.get("question", "")
            ps.blocked_until, ps.block_reason = v.get("blocked_until", 0.0), v.get("block_reason", "")
            ps.loss_base, ps.mk = v.get("loss_base", 0.0), v.get("mk", {})
        self.needs_rebuild = any("avg" not in v for v in d.get("positions", {}).values())

    def rebuild_cost_basis(self, fills: list[tuple[str, str, float, float]]) -> None:
        """Recompute avg/realized from historical fills (cid, side, price, size) for older state files."""
        for ps in self.pos.values():
            ps.avg = ps.realized = 0.0
        nets: dict[str, float] = {}
        for cid, side, price, size in fills:
            ps = self.p(cid)
            net, ps.avg, r = apply_fill(nets.get(cid, 0.0), ps.avg, side, price, size)
            nets[cid] = net
            ps.realized += r
        self.needs_rebuild = False
