"""Rank reward-paying markets by expected rewards per dollar of capital,
after filtering out markets that are prone to sudden price jumps."""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .api import Book, Market, PolymarketAPI
from .rewards import competitor_q, our_share
from .strategy import StrategyConfig, make_quote

_MONTH = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
# "between November 9 and November 15", "between Nov 9-15", "on November 5", "by November 30?"
_DATE_WINDOW = re.compile(
    rf"\bbetween\s+{_MONTH}\s+\d{{1,2}}|\bon\s+{_MONTH}\s+\d{{1,2}}\b|\b(by|before)\s+{_MONTH}\s+\d{{1,2}}\b",
    re.IGNORECASE,
)
_LIVE_KEYWORDS = re.compile(
    r"\b(vs\.?|versus|o/u|over/under|spread:|halftime|1st half|map \d|game \d|set \d|tweet|tweets|say .* during)\b",
    re.IGNORECASE,
)


@dataclass
class Filters:
    min_days_left: float = 30.0  # markets resolving sooner jump more
    min_mid: float = 0.20
    max_mid: float = 0.80
    max_book_spread: float = 0.05
    max_capital: float | None = None
    max_daily_vol_c: float = 4.0  # stdev of daily mid changes over the last month (cents)
    max_daily_jump_c: float = 10.0  # largest single-day move over the last month (cents)
    exclude_date_windows: bool = True
    exclude_sports_live: bool = True


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
    vol_c: float | None = None  # stdev of daily changes, cents
    jump_c: float | None = None  # max daily change, cents
    notes: list[str] = field(default_factory=list)

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


def static_reject(m: Market, f: Filters) -> str | None:
    """Reasons to skip a market that need no extra API calls."""
    if m.reward_daily <= 0:
        return "geen rewards"
    if days_until(m.end_date) < f.min_days_left:
        return f"eindigt binnen {f.min_days_left:.0f} dagen"
    if f.exclude_date_windows and _DATE_WINDOW.search(m.question):
        return "datumvenster"
    if f.exclude_sports_live and (m.game_start or _LIVE_KEYWORDS.search(m.question)):
        return "sport/live-event"
    return None


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


def daily_moves(api: PolymarketAPI, token_id: str) -> tuple[float | None, float | None]:
    """(stdev, max) of absolute daily price changes over the last month, in cents."""
    try:
        h = api.price_history(token_id, "1m", 1440)
    except RuntimeError:
        return None, None
    moves = [(b[1] - a[1]) * 100 for a, b in zip(h, h[1:])]
    if len(moves) < 7:
        return None, None
    return statistics.pstdev(moves), max(abs(x) for x in moves)


def scan(
    api: PolymarketAPI,
    cfg: StrategyConfig,
    f: Filters | None = None,
    want: int = 20,
    max_history_calls: int = 250,
    verbose: bool = True,
) -> list[Candidate]:
    f = f or Filters()
    all_markets = api.reward_markets()
    rejected: dict[str, int] = {}
    markets = []
    for m in all_markets:
        r = static_reject(m, f)
        if r:
            rejected[r] = rejected.get(r, 0) + 1
        else:
            markets.append(m)

    books = api.books([m.yes_token for m in markets])
    cands: list[Candidate] = []
    for m in markets:
        b = books.get(m.yes_token)
        c = evaluate(m, b, cfg) if b else None
        if not c:
            rejected["geen bruikbaar orderboek"] = rejected.get("geen bruikbaar orderboek", 0) + 1
            continue
        reason = None
        if not (f.min_mid <= c.mid <= f.max_mid):
            reason = f"mid buiten {f.min_mid:.2f}-{f.max_mid:.2f}"
        elif c.book_spread > f.max_book_spread:
            reason = "spread in boek te breed"
        elif f.max_capital is not None and c.capital > f.max_capital:
            reason = "te veel kapitaal nodig"
        elif c.exp_reward_day <= 0:
            reason = "geen rewards bij onze quotes"
        if reason:
            rejected[reason] = rejected.get(reason, 0) + 1
        else:
            cands.append(c)
    cands.sort(key=lambda c: c.reward_per_100, reverse=True)

    # volatility check (one API call per market), best candidates first
    out: list[Candidate] = []
    for c in cands[:max_history_calls]:
        c.vol_c, c.jump_c = daily_moves(api, c.market.yes_token)
        if c.vol_c is None:
            rejected["te weinig koershistorie"] = rejected.get("te weinig koershistorie", 0) + 1
        elif c.vol_c > f.max_daily_vol_c:
            rejected["te beweeglijk"] = rejected.get("te beweeglijk", 0) + 1
        elif c.jump_c > f.max_daily_jump_c:
            rejected["recente koerssprong"] = rejected.get("recente koerssprong", 0) + 1
        else:
            out.append(c)
            if len(out) >= want:
                break

    if verbose:
        print(f"{len(all_markets)} markten met rewards, {len(out)} door de filters. Afgewezen:")
        for r, n in sorted(rejected.items(), key=lambda x: -x[1]):
            print(f"   {n:5}  {r}")
        print()
    return out


def print_table(cands: list[Candidate], n: int = 20) -> None:
    hdr = (f"{'#':>2} {'$/dag':>6} {'$/dag/100':>9} {'aandeel':>7} {'pool':>5} {'kapitaal':>8} {'mid':>5}"
           f" {'quotes':>11} {'vol/d':>5} {'sprong':>6} {'dagen':>5}  markt")
    print(hdr)
    print("-" * len(hdr))
    for i, c in enumerate(cands[:n], 1):
        vol = f"{c.vol_c:4.1f}c" if c.vol_c is not None else "   ?"
        jmp = f"{c.jump_c:4.0f}c" if c.jump_c is not None else "   ?"
        print(
            f"{i:>2} {c.exp_reward_day:6.2f} {c.reward_per_100:9.2f} {c.share*100:6.1f}% {c.market.reward_daily:5.0f}"
            f" {c.capital:8.0f} {c.mid:5.3f} {c.bid:.3f}/{c.ask:.3f} {vol:>5} {jmp:>6} {min(c.days_left, 999):5.0f}"
            f"  {c.market.question[:60]}"
        )
