"""Risk helpers: fill toxicity (markouts), recent price unrest and market rotation."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

MARKOUT_HORIZONS = (60, 300)  # seconds after a fill at which we look at the mid again


def markout_cents(side: str, price: float, mid_later: float) -> float:
    """What the fill was worth a while later, in cents per share (negative = the price moved against us)."""
    return (mid_later - price) * 100 if side == "bid" else (price - mid_later) * 100


@dataclass
class PendingFill:
    cid: str
    side: str
    price: float
    size: float
    ts: float
    done: dict = field(default_factory=dict)  # horizon -> markout (cents)


class MarkoutTracker:
    """Measures every fill again 1 and 5 minutes later."""

    def __init__(self):
        self.pending: list[PendingFill] = []

    def add(self, cid: str, side: str, price: float, size: float, ts: float) -> None:
        self.pending.append(PendingFill(cid, side, price, size, ts))

    def update(self, cid: str, mid: float, now: float) -> list[tuple[PendingFill, int, float]]:
        """Record markouts that came due for this market; returns (fill, horizon, cents)."""
        out = []
        for pf in self.pending:
            if pf.cid != cid:
                continue
            for h in MARKOUT_HORIZONS:
                if h not in pf.done and now - pf.ts >= h:
                    pf.done[h] = markout_cents(pf.side, pf.price, mid)
                    out.append((pf, h, pf.done[h]))
        self.pending = [pf for pf in self.pending if len(pf.done) < len(MARKOUT_HORIZONS)]
        return out

    def drop(self, cid: str) -> None:
        self.pending = [pf for pf in self.pending if pf.cid != cid]


class UnrestTracker:
    """How far the mid wandered recently (max - min over a time window)."""

    def __init__(self, window: float = 600.0):
        self.window = window
        self.mids: dict[str, deque] = {}

    def add(self, cid: str, mid: float, now: float) -> float:
        d = self.mids.setdefault(cid, deque())
        d.append((now, mid))
        while d and now - d[0][0] > self.window:
            d.popleft()
        return self.range(cid)

    def range(self, cid: str) -> float:
        d = self.mids.get(cid)
        if not d:
            return 0.0
        vals = [m for _, m in d]
        return max(vals) - min(vals)


def plan_rotation(current: list, candidates: list, target: int, has_position, blocked, key=lambda m: m.slug):
    """Decide which markets to drop and add.

    current/candidates are market objects (candidates best first). A market stays if it still has a
    position (we first want to get out) or still ranks among the candidates and is not blocked.
    Free places are filled with the best candidates that are not blocked and not already traded.
    Returns (keep, removed, added).
    """
    cand_keys = {key(c) for c in candidates}
    keep, removed = [], []
    for m in current:
        if has_position(m) or (key(m) in cand_keys and not blocked(m)):
            keep.append(m)
        else:
            removed.append(m)
    have = {key(m) for m in keep}
    added = []
    for c in candidates:
        if len(keep) + len(added) >= target:
            break
        if key(c) not in have and not blocked(c):
            added.append(c)
            have.add(key(c))
    return keep, removed, added
