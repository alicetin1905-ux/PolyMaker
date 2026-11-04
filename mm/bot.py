"""Paper-trading main loop."""
from __future__ import annotations

import csv
import json
import os
import signal
import time
from dataclasses import dataclass

from .api import Market, PolymarketAPI
from .paper import PaperExchange
from .rewards import our_share
from .scanner import days_until
from .strategy import StrategyConfig, make_quote


@dataclass
class BotConfig:
    interval: float = 15.0  # seconds between ticks
    hours: float | None = None  # stop after this many hours (None = until Ctrl-C)
    kill_move: float = 0.04  # pull quotes if mid jumps more than this in one tick
    cooldown: float = 300.0  # seconds to stay out after a jump
    stop_days_before_end: float = 2.0
    log_dir: str = "logs"
    print_every: int = 1  # print status every N ticks


class PaperBot:
    def __init__(self, api: PolymarketAPI, markets: list[Market], ex: PaperExchange, scfg: StrategyConfig, bcfg: BotConfig):
        self.api, self.markets, self.ex, self.scfg, self.bcfg = api, markets, ex, scfg, bcfg
        self.paused_until: dict[str, float] = {}
        self.mids: dict[str, float] = {}
        self.last_tick: float | None = None
        self.running = True
        os.makedirs(bcfg.log_dir, exist_ok=True)
        self.fills_path = os.path.join(bcfg.log_dir, "fills.csv")
        self.equity_path = os.path.join(bcfg.log_dir, "equity.csv")
        self.state_path = os.path.join(bcfg.log_dir, "state.json")
        self._n_logged_fills = 0
        self._ticks = 0
        for path, hdr in (
            (self.fills_path, ["time", "market", "side", "price", "size", "reason"]),
            (self.equity_path, ["time", "cash", "inventory", "rewards", "equity", "pnl_trading", "pnl_total"]),
        ):
            if not os.path.exists(path):
                with open(path, "w", newline="") as f:
                    csv.writer(f).writerow(hdr)

    # ------------------------------------------------------------------
    def tick(self) -> None:
        now = time.time()
        dt = 0.0 if self.last_tick is None else min(now - self.last_tick, 3 * self.bcfg.interval)
        self.last_tick = now
        books = self.api.books([m.yes_token for m in self.markets])
        for m in self.markets:
            b = books.get(m.yes_token)
            if not b or b.mid is None:
                continue
            ps = self.ex.p(m.condition_id)
            mid = b.mid
            self.mids[m.condition_id] = mid

            # 1. fills from the book and from real trades
            self.ex.on_book(m.condition_id, b.best_bid, b.best_ask, now)
            try:
                self.ex.on_trades(m.condition_id, self.api.trades(m.condition_id, 100), m.yes_token, now)
            except RuntimeError as e:
                print(f"  ! trades {m.slug}: {e}")
            self.ex.on_book(m.condition_id, b.best_bid, b.best_ask, now)  # clear filled orders

            # 2. risk checks
            if ps.last_mid is not None and abs(mid - ps.last_mid) > self.bcfg.kill_move:
                self.paused_until[m.condition_id] = now + self.bcfg.cooldown
                print(f"  ! {m.slug}: mid jumped {ps.last_mid:.3f} -> {mid:.3f}, quotes off for {self.bcfg.cooldown:.0f}s")
            ps.last_mid = mid
            stop = (
                now < self.paused_until.get(m.condition_id, 0)
                or days_until(m.end_date) < self.bcfg.stop_days_before_end
            )

            # 3. requote
            q = make_quote(b, ps.net_yes, m.reward_max_spread, m.reward_min_size, self.scfg)
            if stop:
                q.bid = q.ask = None
            self.ex.set_order(m.condition_id, "bid", q.bid, q.bid_size, b.size_at("bid", q.bid) if q.bid else 0, now)
            self.ex.set_order(m.condition_id, "ask", q.ask, q.ask_size, b.size_at("ask", q.ask) if q.ask else 0, now)

            # 4. rewards accrual (our orders are not in the real book, so `others` = whole book)
            bo, ao = ps.bid, ps.ask
            share = our_share(
                bo.price if bo else None, bo.remaining if bo else 0,
                ao.price if ao else None, ao.remaining if ao else 0,
                b, mid, m.reward_max_spread, m.reward_min_size,
            )
            ps.rewards += share * m.reward_daily * dt / 86400

        self._log(now)

    def _log(self, now: float) -> None:
        new = self.ex.fills[self._n_logged_fills :]
        if new:
            names = {m.condition_id: m.slug for m in self.markets}
            with open(self.fills_path, "a", newline="") as f:
                w = csv.writer(f)
                for fl in new:
                    w.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(fl.ts)), names.get(fl.market, fl.market),
                                fl.side, fl.price, round(fl.size, 2), fl.reason])
                    print(f"  * FILL {fl.side.upper():3} {fl.size:7.1f} @ {fl.price:.3f}  {names.get(fl.market)} ({fl.reason})")
            self._n_logged_fills = len(self.ex.fills)
        inv = self.ex.inventory_value(self.mids)
        rew = self.ex.total_rewards()
        eq = self.ex.cash + inv
        pnl_trading = eq - self.ex.start_cash
        with open(self.equity_path, "a", newline="") as f:
            csv.writer(f).writerow([int(now), round(self.ex.cash, 4), round(inv, 4), round(rew, 4), round(eq, 4),
                                    round(pnl_trading, 4), round(pnl_trading + rew, 4)])
        state = self.ex.to_dict()
        state["markets"] = [m.slug for m in self.markets]
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=1)
        os.replace(tmp, self.state_path)  # atomic: no corrupt state if the Mac loses power
        self._ticks += 1
        if (self._ticks - 1) % max(1, self.bcfg.print_every):
            return
        print(
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] kas ${self.ex.cash:8.2f} | inventaris ${inv:7.2f} | "
            f"handel P&L ${pnl_trading:+7.2f} | beloning ${rew:6.3f} | totaal ${pnl_trading + rew:+7.2f}"
        )
        for m in self.markets:
            ps = self.ex.p(m.condition_id)
            b = f"{ps.bid.price:.3f}x{ps.bid.remaining:.0f}(q{ps.bid.queue_ahead:.0f})" if ps.bid else "   -   "
            a = f"{ps.ask.price:.3f}x{ps.ask.remaining:.0f}(q{ps.ask.queue_ahead:.0f})" if ps.ask else "   -   "
            print(f"    {m.slug[:40]:40} mid {self.mids.get(m.condition_id, 0):.3f}  bid {b:22} ask {a:22} netYES {ps.net_yes:+7.1f}")

    # ------------------------------------------------------------------
    def run(self) -> None:
        signal.signal(signal.SIGINT, lambda *_: setattr(self, "running", False))
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "running", False))
        end = time.time() + self.bcfg.hours * 3600 if self.bcfg.hours else None
        while self.running and (end is None or time.time() < end):
            t0 = time.time()
            try:
                self.tick()
            except Exception as e:  # keep running on unexpected API data / network trouble
                print(f"  ! tick mislukt: {type(e).__name__}: {e}")
            while self.running and time.time() - t0 < self.bcfg.interval:
                time.sleep(0.5)
        print("\nGestopt. Samenvatting:")
        print_report(self.state_path, self.equity_path, self.fills_path)


def print_report(state_path: str, equity_path: str, fills_path: str) -> None:
    if not os.path.exists(equity_path):
        print("Nog geen data. Start eerst: python -m mm paper")
        return
    with open(equity_path) as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print("Nog geen data.")
        return
    first, last = rows[0], rows[-1]
    hours = (int(last["time"]) - int(first["time"])) / 3600
    with open(fills_path) as f:
        fills = list(csv.DictReader(f))
    vol = sum(float(r["price"]) * float(r["size"]) for r in fills)
    pnl_trade, rew = float(last["pnl_trading"]), float(last["rewards"])
    total = pnl_trade + rew
    print(f"  Looptijd:          {hours:.1f} uur")
    print(f"  Fills:             {len(fills)}  (volume ${vol:,.2f})")
    print(f"  Handel P&L:        ${pnl_trade:+.2f}  (incl. ongerealiseerd op mid)")
    print(f"  Beloningen (schat):${rew:+.2f}")
    print(f"  Totaal:            ${total:+.2f}")
    worst = min(float(r["pnl_total"]) for r in rows)
    print(f"  Slechtste moment:  ${worst:+.2f}")
    if hours >= 1:
        print(f"  Per dag (extrapol.): ${total / hours * 24:+.2f}   <- pas betrouwbaar na meerdere dagen")
