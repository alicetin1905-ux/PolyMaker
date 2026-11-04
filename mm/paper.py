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
    def set_order(self, cid: str, side: str, price: float | None, size: float, book_size_at_price: float, now: float) -> None:
        ps = self.p(cid)
        cur: Order | None = getattr(ps, side)
        if price is None or size <= 0:
            setattr(ps, side, None)
            return
        if cur and abs(cur.price - price) < EPS and cur.remaining >= 0.5 * size:
            return  # keep queue position
        cost = size * price if side == "bid" else max(0.0, size - ps.yes) * (1 - price)
        if cost > self.free_cash(exclude=cur) + EPS:
            setattr(ps, side, None)
            return
        setattr(ps, side, Order(side, price, size, queue_ahead=book_size_at_price, placed_at=now))

    # ---- fills -----------------------------------------------------------
    def _execute(self, cid: str, o: Order, qty: float, now: float, reason: str) -> None:
        if qty <= EPS:
            return
        ps = self.p(cid)
        fee = qty * self.maker_fee * min(o.price, 1 - o.price)
        if o.side == "bid":
            self.cash -= qty * o.price + fee
            ps.yes += qty
        else:
            self.cash -= qty * (1 - o.price) + fee
            ps.no += qty
        o.filled += qty
        merged = min(ps.yes, ps.no)
        if merged > 0:
            ps.yes -= merged
            ps.no -= merged
            self.cash += merged
        self.fills.append(Fill(now, cid, o.side, o.price, qty, reason))

    def on_trades(self, cid: str, trades: list[dict], yes_token: str, now: float) -> None:
        """Process real trades (from the data API) against our resting orders."""
        ps = self.p(cid)
        # collapse maker/taker rows of the same match into one print
        prints: dict[tuple, tuple[float, float, float]] = {}
        for t in trades:
            key = (t.get("transaction_hash"), t.get("token_id"), float(t["price"]))
            if key in ps.seen_trades:
                continue
            px = float(t["price"])
            yes_px = px if t.get("token_id") == yes_token else round(1 - px, 6)
            size, ts = float(t["size"]), float(t.get("timestamp", 0))
            prev = prints.get(key)
            if prev is None or size > prev[1]:
                prints[key] = (yes_px, size, ts)
        ps.seen_trades.update(prints.keys())
        if not ps.initialized:
            ps.initialized = True  # ignore history from before we started
            return
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
                cid: {"yes": ps.yes, "no": ps.no, "rewards": ps.rewards, "last_mid": ps.last_mid}
                for cid, ps in self.pos.items()
            },
        }

    def load_dict(self, d: dict) -> None:
        self.start_cash = d["start_cash"]
        self.cash = d["cash"]
        for cid, v in d.get("positions", {}).items():
            ps = self.p(cid)
            ps.yes, ps.no, ps.rewards, ps.last_mid = v["yes"], v["no"], v["rewards"], v.get("last_mid")
