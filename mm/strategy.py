"""Quote generation: symmetric quotes around the midpoint, skewed by inventory."""
from __future__ import annotations

import math
from dataclasses import dataclass

from .api import Book


@dataclass
class StrategyConfig:
    order_size: float = 50.0  # shares per side (raised to the reward min_size if needed)
    spread_frac: float = 0.5  # quote distance as a fraction of the reward max_spread
    min_distance_ticks: int = 1  # never quote closer than this to the mid
    max_position: float = 150.0  # max net YES (or NO) shares before we stop adding
    skew: float = 1.0  # how hard inventory shifts quotes (1.0 = up to one full quote-distance)
    min_mid: float = 0.05  # don't quote markets that are basically decided
    max_mid: float = 0.95


@dataclass
class Quote:
    bid: float | None
    bid_size: float
    ask: float | None
    ask_size: float
    reason: str = ""


def round_down(p: float, tick: float) -> float:
    return round(math.floor(p / tick + 1e-9) * tick, 6)


def round_up(p: float, tick: float) -> float:
    return round(math.ceil(p / tick - 1e-9) * tick, 6)


def make_quote(
    book: Book,
    net_yes: float,
    max_spread_c: float,
    reward_min_size: float,
    cfg: StrategyConfig,
) -> Quote:
    mid = book.mid
    if mid is None:
        return Quote(None, 0, None, 0, "no two-sided book")
    if not (cfg.min_mid <= mid <= cfg.max_mid):
        return Quote(None, 0, None, 0, f"mid {mid:.3f} outside [{cfg.min_mid}, {cfg.max_mid}]")

    tick = book.tick
    size = max(cfg.order_size, reward_min_size, book.min_order_size)
    # distance from mid, in price units; stay inside the reward band
    band = max_spread_c / 100.0 if max_spread_c > 0 else 0.03
    dist = max(band * cfg.spread_frac, cfg.min_distance_ticks * tick)

    # inventory skew: long YES -> shift both quotes down (sell more eagerly, buy less)
    inv = max(-1.0, min(1.0, net_yes / cfg.max_position)) if cfg.max_position > 0 else 0.0
    center = mid - cfg.skew * inv * dist

    bid = round_down(center - dist, tick)
    ask = round_up(center + dist, tick)

    # post-only: never cross the book
    if book.best_ask is not None and bid >= book.best_ask:
        bid = round(book.best_ask - tick, 6)
    if book.best_bid is not None and ask <= book.best_bid:
        ask = round(book.best_bid + tick, 6)

    bid_size = ask_size = size
    reason = ""
    if net_yes >= cfg.max_position:
        bid, bid_size, reason = None, 0, "max long YES: bid off"
    if net_yes <= -cfg.max_position:
        ask, ask_size, reason = None, 0, "max long NO: ask off"
    if bid is not None and bid <= 0:
        bid, bid_size = None, 0
    if ask is not None and ask >= 1:
        ask, ask_size = None, 0
    return Quote(bid, bid_size, ask, ask_size, reason)
