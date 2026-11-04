"""Rank reward-paying markets by expected rewards per dollar of capital."""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timezone

from .api import Book, Market, PolymarketAPI
from .rewards import competitor_q, our_share
from .strategy import StrategyConfig, make_quote


@dataclass
class Candidate:
    market: Market
    mid: float
    bid: float
    ask: float
    size: float
    share: float
    exp_reward_day: float
    capital: float
    days_left: float
    book_spread: float
    vol_1d: float | None = None  # stdev of hourly mid changes over last week, in cents

    @property
    def reward_per_100(self) -> float:
        return 100 * self.exp_reward_day / self.capital if self.capital > 0 else 0.0


def days_until(iso: str | None) -> float:
    if not iso:
        return 9999.0
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return 9999.0
    return (dt - datetime.now(timezone.utc)).total_seconds() / 86400


def evaluate(m: Market, book: Book, cfg: StrategyConfig) -> Candidate | None:
    mid = book.mid
    if mid is None:
        return None
    q = make_quote(book, 0.0, m.reward_max_spread, m.reward_min_size, cfg)
    if q.bid is None or q.ask is None:
        return None
    others = competitor_q(book, mid, m.reward_max_spread, m.reward_min_size)
    share = our_share(q.bid, q.bid_size, q.ask, q.ask_size, book, mid, m.reward_max_spread, m.reward_min_size, others)
    capital = q.bid * q.bid_size + (1 - q.ask) * q.ask_size
    return Candidate(
        market=m,
        mid=mid,
        bid=q.bid,
        ask=q.ask,
        size=q.bid_size,
        share=share,
        exp_reward_day=share * m.reward_daily,
        capital=capital,
        days_left=days_until(m.end_date),
        book_spread=(book.best_ask or 0) - (book.best_bid or 0),
    )


def scan(
    api: PolymarketAPI,
    cfg: StrategyConfig,
    min_days_left: float = 3.0,
    max_capital: float | None = None,
    max_book_spread: float = 0.05,
    with_vol: int = 15,
) -> list[Candidate]:
    markets = [m for m in api.reward_markets() if m.reward_daily > 0 and days_until(m.end_date) >= min_days_left]
    books = api.books([m.yes_token for m in markets])
    cands: list[Candidate] = []
    for m in markets:
        b = books.get(m.yes_token)
        if not b:
            continue
        c = evaluate(m, b, cfg)
        if not c or c.book_spread > max_book_spread:
            continue
        if max_capital is not None and c.capital > max_capital:
            continue
        cands.append(c)
    cands.sort(key=lambda c: c.reward_per_100, reverse=True)
    for c in cands[:with_vol]:
        try:
            h = api.price_history(c.market.yes_token, "1w", 60)
            moves = [abs(b[1] - a[1]) * 100 for a, b in zip(h, h[1:])]
            c.vol_1d = statistics.pstdev(moves) * (24**0.5) if len(moves) > 5 else None
        except RuntimeError:
            pass
    return cands


def print_table(cands: list[Candidate], n: int = 20) -> None:
    hdr = f"{'#':>2} {'$/dag':>6} {'$/dag/100':>9} {'aandeel':>7} {'pool':>6} {'kapitaal':>8} {'mid':>5} {'quotes':>13} {'vol/dag':>7} {'dagen':>5}  markt"
    print(hdr)
    print("-" * len(hdr))
    for i, c in enumerate(cands[:n], 1):
        vol = f"{c.vol_1d:5.1f}c" if c.vol_1d is not None else "   ?"
        print(
            f"{i:>2} {c.exp_reward_day:6.2f} {c.reward_per_100:9.2f} {c.share*100:6.1f}% {c.market.reward_daily:6.0f}"
            f" {c.capital:8.0f} {c.mid:5.3f} {c.bid:.3f}/{c.ask:.3f} {vol:>7} {min(c.days_left, 999):5.0f}  {c.market.question[:60]}"
        )
