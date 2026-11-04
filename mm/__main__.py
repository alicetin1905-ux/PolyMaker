"""CLI:  python -m mm scan | paper | report"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .api import PolymarketAPI
from .bot import BotConfig, PaperBot, print_report
from .paper import PaperExchange
from .scanner import Filters, print_table, scan
from .strategy import StrategyConfig


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mm", description="Polymarket market-making bot (alleen paper trading)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def strat_args(p):
        p.add_argument("--size", type=float, default=50, help="shares per kant (minimaal de reward min_size)")
        p.add_argument("--spread-frac", type=float, default=0.5, help="afstand tot mid als fractie van reward max_spread")
        p.add_argument("--max-pos", type=float, default=150, help="max netto positie (shares) per markt")
        p.add_argument("--min-days", type=float, default=30, help="sla markten over die binnen N dagen eindigen")
        p.add_argument("--min-mid", type=float, default=0.20, help="alleen markten met mid tussen min-mid en max-mid")
        p.add_argument("--max-mid", type=float, default=0.80)
        p.add_argument("--max-vol", type=float, default=4.0, help="max stdev dagbeweging (centen, laatste maand)")
        p.add_argument("--max-jump", type=float, default=10.0, help="max grootste dagsprong (centen, laatste maand)")
        p.add_argument("--allow-risky", action="store_true", help="datumvenster- en sportmarkten niet uitsluiten")

    s = sub.add_parser("scan", help="toon de beste markten voor liquidity rewards")
    strat_args(s)
    s.add_argument("--top", type=int, default=20)
    s.add_argument("--max-capital", type=float, default=None, help="max kapitaal per markt ($)")

    p = sub.add_parser("paper", help="draai de bot met nepgeld op live data")
    strat_args(p)
    p.add_argument("--bankroll", type=float, default=500)
    p.add_argument("--markets", type=int, default=3, help="aantal markten (top uit de scan)")
    p.add_argument("--slug", action="append", default=[], help="specifieke markt-slug (meerdere mogelijk)")
    p.add_argument("--interval", type=float, default=15)
    p.add_argument("--hours", type=float, default=None)
    p.add_argument("--log-dir", default="logs")
    p.add_argument("--resume", action="store_true", help="ga verder met kas/posities uit logs/state.json")

    r = sub.add_parser("report", help="samenvatting van een paper-run")
    r.add_argument("--log-dir", default="logs")

    a = ap.parse_args(argv)
    if a.cmd == "report":
        d = a.log_dir
        print_report(os.path.join(d, "state.json"), os.path.join(d, "equity.csv"), os.path.join(d, "fills.csv"))
        return 0

    api = PolymarketAPI()
    scfg = StrategyConfig(order_size=a.size, spread_frac=a.spread_frac, max_position=a.max_pos)

    def filters(max_capital):
        return Filters(
            min_days_left=a.min_days, min_mid=a.min_mid, max_mid=a.max_mid, max_capital=max_capital,
            max_daily_vol_c=a.max_vol, max_daily_jump_c=a.max_jump,
            exclude_date_windows=not a.allow_risky, exclude_sports_live=not a.allow_risky,
        )

    if a.cmd == "scan":
        print("Markten met liquidity rewards ophalen...")
        cands = scan(api, scfg, filters(a.max_capital), want=a.top)
        print_table(cands, a.top)
        print("\n$/dag = geschatte beloning bij deze quotes, $/dag/100 = per $100 kapitaal.")
        print("Schatting is conservatief én aanname: concurrentie verandert continu.")
        return 0

    # paper
    per_market = a.bankroll / max(1, a.markets if not a.slug else len(a.slug))
    if a.slug:
        allm = {m.slug: m for m in api.reward_markets()}
        missing = [s_ for s_ in a.slug if s_ not in allm]
        if missing:
            print(f"Niet gevonden (of geen rewards): {missing}")
            return 1
        markets = [allm[s_] for s_ in a.slug]
    else:
        print("Scannen naar markten...")
        cands = scan(api, scfg, filters(per_market), want=a.markets)
        print_table(cands, a.markets)
        markets = [c.market for c in cands[: a.markets]]
    if not markets:
        print("Geen geschikte markten gevonden.")
        return 1

    ex = PaperExchange(a.bankroll)
    state = os.path.join(a.log_dir, "state.json")
    if a.resume and os.path.exists(state):
        with open(state) as f:
            ex.load_dict(json.load(f))
        print(f"Hervat: kas ${ex.cash:.2f}")
    print(f"\nPAPER TRADING met ${a.bankroll:.0f} nepgeld op {len(markets)} markt(en). Ctrl-C om te stoppen.\n")
    PaperBot(api, markets, ex, scfg, BotConfig(interval=a.interval, hours=a.hours, log_dir=a.log_dir)).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
