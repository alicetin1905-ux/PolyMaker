"""Polymarket liquidity-reward scoring (approximation of the published formula).

Per order:   S(v, s) = ((v - s) / v)^2 * size
  v = max_spread in cents, s = distance from the midpoint in cents.
Per maker:   Q_one = score of bids on YES (≙ asks on NO)
             Q_two = score of asks on YES (≙ bids on NO)
             mid in [0.10, 0.90]: Q_min = max(min(Q1, Q2), max(Q1, Q2) / c)   (c = 3)
             otherwise:           Q_min = min(Q1, Q2)  (two-sided quoting required)
Daily pool is split pro rata on Q_min (sampled every minute).
"""
from __future__ import annotations

from .api import Book

C_SINGLE_SIDED = 3.0


def order_score(price: float, size: float, mid: float, max_spread_c: float, min_size: float) -> float:
    if size < min_size or max_spread_c <= 0:
        return 0.0
    s = abs(price - mid) * 100.0
    if s >= max_spread_c:
        return 0.0
    return ((max_spread_c - s) / max_spread_c) ** 2 * size


def q_min(q_one: float, q_two: float, mid: float) -> float:
    if 0.10 <= mid <= 0.90:
        return max(min(q_one, q_two), max(q_one, q_two) / C_SINGLE_SIDED)
    return min(q_one, q_two)


def book_scores(book: Book, mid: float, max_spread_c: float, min_size: float) -> tuple[float, float]:
    """Total (Q_one, Q_two) of all resting liquidity, treating each level as one order."""
    q1 = sum(order_score(l.price, l.size, mid, max_spread_c, min_size) for l in book.bids)
    q2 = sum(order_score(l.price, l.size, mid, max_spread_c, min_size) for l in book.asks)
    return q1, q2


def competitor_q(book: Book, mid: float, max_spread_c: float, min_size: float) -> float:
    """Conservative estimate of everyone else's summed Q_min.

    Sum of per-maker minimums <= minimum of the sums, so using the book totals
    over-estimates competition and therefore under-estimates our share.
    """
    q1, q2 = book_scores(book, mid, max_spread_c, min_size)
    return q_min(q1, q2, mid)


def our_share(
    bid: float | None,
    bid_size: float,
    ask: float | None,
    ask_size: float,
    book: Book,
    mid: float,
    max_spread_c: float,
    min_size: float,
    others_q: float | None = None,
) -> float:
    q1 = order_score(bid, bid_size, mid, max_spread_c, min_size) if bid is not None else 0.0
    q2 = order_score(ask, ask_size, mid, max_spread_c, min_size) if ask is not None else 0.0
    mine = q_min(q1, q2, mid)
    if mine <= 0:
        return 0.0
    if others_q is None:
        others_q = competitor_q(book, mid, max_spread_c, min_size)
    return mine / (mine + others_q)
