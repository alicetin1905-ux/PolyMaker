"""Paper-trading main loop."""
from __future__ import annotations

import csv
import json
import os
import signal
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .api import Market, PolymarketAPI
from .events import EventLog, write_json_atomic
from .paper import PaperExchange
from .rewards import our_share
from .risk import MarkoutTracker, UnrestTracker, plan_rotation
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
    # 1. loss limit per market
    max_loss: float = 3.0  # $ trading loss in one market before it is blocked (0 = off)
    block_hours: float = 24.0  # how long a blocked market only exits its position
    # 2. toxic fills
    toxic_cents: float = 1.0  # block when fills are worth less than -X cents per share 5 minutes later (0 = off)
    toxic_min_fills: int = 5  # ... measured over at least this many fills
    # 3. rotation
    rotate_hours: float = 24.0  # look for better markets every N hours (0 = off)
    target_markets: int = 0  # how many markets to trade (0 = keep the starting number)


class PaperBot:
    def __init__(self, api: PolymarketAPI, markets: list[Market], ex: PaperExchange, scfg: StrategyConfig,
                 bcfg: BotConfig, rescan: Callable[[], list[Market]] | None = None, last_rotation: float | None = None):
        self.api, self.markets, self.ex, self.scfg, self.bcfg = api, markets, ex, scfg, bcfg
        self.rescan = rescan
        self.target = bcfg.target_markets or len(markets)
        self.paused_until: dict[str, float] = {}
        self.mids: dict[str, float] = {}
        self.last_tick: float | None = None
        self.running = True
        os.makedirs(bcfg.log_dir, exist_ok=True)
        self.fills_path = os.path.join(bcfg.log_dir, "fills.csv")
        self.equity_path = os.path.join(bcfg.log_dir, "equity.csv")
        self.state_path = os.path.join(bcfg.log_dir, "state.json")
        self.markouts_path = os.path.join(bcfg.log_dir, "markouts.csv")
        self._n_logged_fills = 0
        self._ticks = 0
        self.events = EventLog(bcfg.log_dir)
        self.live_path = os.path.join(bcfg.log_dir, "live.json")
        self.live_markets: dict[str, dict] = {}
        self.markouts = MarkoutTracker()
        self._n_tracked = 0
        self.unrest = UnrestTracker(600.0)
        # rotation runs the (slow) scanner in a background thread and applies the result in the main loop
        # the starting market choice counts as a rotation; after a restart we continue the saved schedule
        self.last_rotation: float = last_rotation or time.time()
        self.next_rotation = self.last_rotation + bcfg.rotate_hours * 3600 if (rescan and bcfg.rotate_hours > 0) else None
        self._scan_thread: threading.Thread | None = None
        self._scan_result: list[Market] | None = None
        self._scan_error: str | None = None
        self.started_at = time.time()
        self._fills_before = 0  # fills logged by earlier runs (for the total count)
        if os.path.exists(self.fills_path):
            with open(self.fills_path) as f:
                self._fills_before = max(0, sum(1 for line in f if line.strip()) - 1)
        if os.path.exists(self.equity_path):
            with open(self.equity_path) as f:
                rows = csv.reader(f)
                next(rows, None)
                first = next(rows, None)
                if first:
                    self.started_at = float(first[0])
        for path, hdr in (
            (self.fills_path, ["time", "market", "side", "price", "size", "reason", "realized", "pos_after", "avg_after"]),
            (self.equity_path, ["time", "cash", "inventory", "rewards", "equity", "pnl_trading", "pnl_total"]),
            (self.markouts_path, ["fill_time", "market", "side", "price", "size", "after_s", "mid", "markout_cents"]),
        ):
            if not os.path.exists(path):
                with open(path, "w", newline="") as f:
                    csv.writer(f).writerow(hdr)

    # ------------------------------------------------------------------
    def _block(self, m: Market, ps, now: float, reason: str, detail: str) -> None:
        ps.blocked_until = now + self.bcfg.block_hours * 3600
        ps.block_reason = reason
        print(f"  ! {m.slug}: GEBLOKKEERD ({reason}: {detail}) tot {time.strftime('%d-%m %H:%M', time.localtime(ps.blocked_until))}")
        self.events.emit("block", m=m.slug, reason=reason, detail=detail, until=ps.blocked_until)

    def _risk_checks(self, m: Market, ps, mid: float, now: float) -> None:
        if ps.blocked_until and now >= ps.blocked_until:  # block expired: start counting afresh
            ps.blocked_until, ps.block_reason = 0.0, ""
            ps.loss_base = ps.trading_pnl(mid)
            ps.mk = {}
            self.events.emit("unblock", m=m.slug)
        if ps.blocked_until:
            return
        loss = ps.trading_pnl(mid) - ps.loss_base
        if self.bcfg.max_loss > 0 and loss < -self.bcfg.max_loss:
            self._block(m, ps, now, "verlies", f"handel {loss:+.2f} dollar")
            return
        avg5, n5 = ps.markout(300)
        if self.bcfg.toxic_cents > 0 and n5 >= self.bcfg.toxic_min_fills and avg5 is not None and avg5 < -self.bcfg.toxic_cents:
            self._block(m, ps, now, "giftige fills", f"gem. {avg5:+.2f} cent na 5 min over {n5} fills")

    def tick(self) -> None:
        now = time.time()
        dt = 0.0 if self.last_tick is None else min(now - self.last_tick, 3 * self.bcfg.interval)
        self.last_tick = now
        self._maybe_rotate(now)
        books = self.api.books([m.yes_token for m in self.markets])
        for m in self.markets:
            b = books.get(m.yes_token)
            if not b or b.mid is None:
                self.events.emit("scan", m=m.slug, mid=None)
                self.live_markets.setdefault(m.slug, {}).update(status="geen boek")
                continue
            ps = self.ex.p(m.condition_id)
            ps.slug, ps.question = m.slug, m.question
            mid = b.mid
            self.mids[m.condition_id] = mid
            self.events.emit("scan", m=m.slug, mid=mid, bb=b.best_bid, ba=b.best_ask)
            recent = self.unrest.add(m.condition_id, mid, now)

            # 1. fills from the book and from real trades
            self.ex.on_book(m.condition_id, b.best_bid, b.best_ask, now)
            n_new = 0
            try:
                n_new = self.ex.on_trades(m.condition_id, self.api.trades(m.condition_id, 100), m.yes_token, now)
            except RuntimeError as e:
                print(f"  ! trades {m.slug}: {e}")
                self.events.emit("error", m=m.slug, msg=f"trades: {e}"[:200])
            if n_new:
                self.events.emit("trades", m=m.slug, n=n_new)
            self.ex.on_book(m.condition_id, b.best_bid, b.best_ask, now)  # clear filled orders
            self._register_fills()

            # 2. how did earlier fills turn out? (markouts after 1 and 5 minutes)
            for pf, horizon, cents in self.markouts.update(m.condition_id, mid, now):
                ps.add_markout(horizon, cents, pf.size)
                with open(self.markouts_path, "a", newline="") as f:
                    csv.writer(f).writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(pf.ts)), m.slug, pf.side,
                                            pf.price, round(pf.size, 2), horizon, round(mid, 4), round(cents, 3)])

            # 3. risk checks: jump pause, loss limit, toxic fills
            if ps.last_mid is not None and abs(mid - ps.last_mid) > self.bcfg.kill_move:
                self.paused_until[m.condition_id] = now + self.bcfg.cooldown
                print(f"  ! {m.slug}: mid jumped {ps.last_mid:.3f} -> {mid:.3f}, quotes off for {self.bcfg.cooldown:.0f}s")
                self.events.emit("pause", m=m.slug, frm=ps.last_mid, to=mid, secs=self.bcfg.cooldown)
            ps.last_mid = mid
            self._risk_checks(m, ps, mid, now)
            days_left = days_until(m.end_date)
            paused = now < self.paused_until.get(m.condition_id, 0)
            ending = days_left < self.bcfg.stop_days_before_end
            blocked = bool(ps.blocked_until)

            # 4. requote
            q = make_quote(b, ps.net_yes, m.reward_max_spread, m.reward_min_size, self.scfg, recent)
            if paused or ending:
                q.bid = q.ask = None
            elif blocked:  # only the order that shrinks the position, never new exposure
                net = ps.net_yes
                if net > 0:
                    q.bid, q.ask_size = None, min(q.ask_size, net)
                elif net < 0:
                    q.ask, q.bid_size = None, min(q.bid_size, -net)
                else:
                    q.bid = q.ask = None
            for side, px, sz in (("bid", q.bid, q.bid_size), ("ask", q.ask, q.ask_size)):
                if self.ex.set_order(m.condition_id, side, px, sz, b.size_at(side, px) if px else 0, now):
                    self.events.emit("requote", m=m.slug, side=side, price=px)

            # 5. rewards accrual (our orders are not in the real book, so `others` = whole book)
            bo, ao = ps.bid, ps.ask
            share = our_share(
                bo.price if bo else None, bo.remaining if bo else 0,
                ao.price if ao else None, ao.remaining if ao else 0,
                b, mid, m.reward_max_spread, m.reward_min_size,
            )
            ps.rewards += share * m.reward_daily * dt / 86400

            mk1, n1 = ps.markout(60)
            mk5, n5 = ps.markout(300)
            if blocked:
                status = "geblokkeerd"
            elif paused:
                status = "pauze"
            elif ending:
                status = "stopt (einde nadert)"
            else:
                status = "quote" if (bo or ao) else "wacht"
            lm = self.live_markets.setdefault(m.slug, {"trades_seen": 0})
            lm.update(
                slug=m.slug, question=m.question, condition_id=m.condition_id, yes_token=m.yes_token,
                end_date=m.end_date, days_left=round(days_left, 1), reward_daily=m.reward_daily,
                max_spread=m.reward_max_spread, min_size=m.reward_min_size, tick=b.tick,
                mid=mid, best_bid=b.best_bid, best_ask=b.best_ask,
                bid=[bo.price, round(bo.remaining, 2), round(bo.queue_ahead, 2)] if bo else None,
                ask=[ao.price, round(ao.remaining, 2), round(ao.queue_ahead, 2)] if ao else None,
                yes=round(ps.yes, 4), no=round(ps.no, 4), net=round(ps.net_yes, 4),
                value=round(ps.yes * mid + ps.no * (1 - mid), 4), rewards=round(ps.rewards, 5),
                avg=round(ps.avg, 4), realized=round(ps.realized, 4), unrealized=round(ps.unrealized(mid), 4),
                pnl=round(ps.realized + ps.unrealized(mid) + ps.rewards, 4),
                share=round(share, 4), est_reward_day=round(share * m.reward_daily, 4),
                status=status,
                paused_until=self.paused_until.get(m.condition_id) if paused else None,
                blocked_until=ps.blocked_until or None, block_reason=ps.block_reason or None,
                loss_since=round(ps.trading_pnl(mid) - ps.loss_base, 4),
                mk1=None if mk1 is None else round(mk1, 3), mk5=None if mk5 is None else round(mk5, 3), mk_n=n5,
                unrest=round(recent, 4), dist=round(q.dist, 4),
                trades_seen=lm["trades_seen"] + n_new, last_update=now,
            )

        self._log(now)

    # ---- fills -> markout tracking -----------------------------------------
    def _register_fills(self) -> None:
        for fl in self.ex.fills[self._n_tracked:]:
            self.markouts.add(fl.market, fl.side, fl.price, fl.size, fl.ts)
        self._n_tracked = len(self.ex.fills)

    # ---- 3. rotation ---------------------------------------------------------
    def _maybe_rotate(self, now: float) -> None:
        if self.next_rotation is None:
            return
        if self._scan_thread is None and now >= self.next_rotation:
            def work():
                try:
                    self._scan_result = self.rescan()
                except Exception as e:  # network trouble: try again next time
                    self._scan_error = f"{type(e).__name__}: {e}"[:200]
            print("  ~ markten verversen: scanner gestart")
            self.events.emit("rotate_start")
            self._scan_thread = threading.Thread(target=work, daemon=True)
            self._scan_thread.start()
            return
        if self._scan_thread is not None and not self._scan_thread.is_alive():
            self._scan_thread = None
            self.next_rotation = now + self.bcfg.rotate_hours * 3600
            if self._scan_error or self._scan_result is None:
                self.events.emit("error", msg=f"markten verversen mislukt: {self._scan_error}")
                self._scan_error = None
                return
            cands, self._scan_result = self._scan_result, None
            self.apply_rotation(cands, now)

    def apply_rotation(self, candidates: list[Market], now: float) -> tuple[list[Market], list[Market]]:
        def has_position(m):
            return abs(self.ex.p(m.condition_id).net_yes) >= 1

        def blocked(m):
            return self.ex.p(m.condition_id).blocked_until > now

        keep, removed, added = plan_rotation(self.markets, candidates, self.target, has_position, blocked)
        for m in removed:
            for side in ("bid", "ask"):
                self.ex.set_order(m.condition_id, side, None, 0, 0, now)
            self.markouts.drop(m.condition_id)
            self.live_markets.pop(m.slug, None)
        self.markets = keep + added
        self.last_rotation = now
        print(f"  ~ markten ververst: -{len(removed)} +{len(added)}")
        self.events.emit("rotate", out=[m.slug for m in removed], into=[m.slug for m in added],
                         questions={m.slug: m.question for m in removed + added})
        return removed, added

    # ---- logging ---------------------------------------------------------------
    def _retired(self) -> list[dict]:
        """Markets that are no longer traded but still count in the results."""
        active = {m.condition_id for m in self.markets}
        out = []
        for cid, ps in self.ex.pos.items():
            if cid in active or not ps.slug:
                continue
            if abs(ps.realized) < 1e-9 and ps.rewards < 1e-9 and abs(ps.net_yes) < 1e-9:
                continue
            mid = ps.last_mid
            mk5, n5 = ps.markout(300)
            out.append({
                "slug": ps.slug, "question": ps.question, "net": round(ps.net_yes, 4), "avg": round(ps.avg, 4),
                "mid": mid, "realized": round(ps.realized, 4), "unrealized": round(ps.unrealized(mid), 4),
                "rewards": round(ps.rewards, 5), "pnl": round(ps.realized + ps.unrealized(mid) + ps.rewards, 4),
                "mk5": None if mk5 is None else round(mk5, 3), "mk_n": n5, "status": "uit rotatie",
                "blocked_until": ps.blocked_until or None, "block_reason": ps.block_reason or None,
            })
        return out

    def _log(self, now: float) -> None:
        new = self.ex.fills[self._n_logged_fills :]
        if new:
            names = {cid: ps.slug for cid, ps in self.ex.pos.items() if ps.slug}
            names.update({m.condition_id: m.slug for m in self.markets})
            with open(self.fills_path, "a", newline="") as f:
                w = csv.writer(f)
                for fl in new:
                    w.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(fl.ts)), names.get(fl.market, fl.market),
                                fl.side, fl.price, round(fl.size, 2), fl.reason,
                                round(fl.realized, 4), round(fl.pos_after, 2), round(fl.avg_after, 4)])
                    print(f"  * FILL {fl.side.upper():3} {fl.size:7.1f} @ {fl.price:.3f}  {names.get(fl.market)} ({fl.reason})")
                    self.events.emit("fill", m=names.get(fl.market, fl.market), side=fl.side, price=fl.price,
                                     size=round(fl.size, 2), reason=fl.reason, realized=round(fl.realized, 4),
                                     pos=round(fl.pos_after, 2), avg=round(fl.avg_after, 4))
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
        state["last_rotation"] = self.last_rotation
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=1)
        os.replace(tmp, self.state_path)  # atomic: no corrupt state if the Mac loses power
        self.events.emit("tick", n=self._ticks + 1, equity=round(eq, 4), pnl=round(pnl_trading, 4), rewards=round(rew, 5))
        b = self.bcfg
        write_json_atomic(self.live_path, {
            "updated": now, "started_at": self.started_at, "interval": b.interval, "ticks": self._ticks + 1,
            "mode": "paper", "start_cash": self.ex.start_cash, "cash": round(self.ex.cash, 4),
            "inventory": round(inv, 4), "equity": round(eq, 4), "rewards": round(rew, 5),
            "pnl_trading": round(pnl_trading, 4), "pnl_total": round(pnl_trading + rew, 4),
            "n_fills": self._fills_before + self._n_logged_fills,
            "realized": round(sum(p.realized for p in self.ex.pos.values()), 4),
            "unrealized": round(sum(p.unrealized(self.mids.get(c, p.last_mid)) for c, p in self.ex.pos.items()), 4),
            "reserved": round(self.ex.reserved(), 4),
            "settings": {
                "size": self.scfg.order_size, "max_pos": self.scfg.max_position, "spread_frac": self.scfg.spread_frac,
                "join": self.scfg.join_best, "vol_factor": self.scfg.vol_factor,
                "kill_move": b.kill_move, "cooldown": b.cooldown, "interval": b.interval,
                "max_loss": b.max_loss, "block_hours": b.block_hours, "toxic_cents": b.toxic_cents,
                "toxic_min_fills": b.toxic_min_fills, "rotate_hours": b.rotate_hours if self.rescan else 0,
                "target_markets": self.target,
            },
            "rotation": {"last": self.last_rotation, "next": self.next_rotation, "scanning": self._scan_thread is not None},
            "markets": [self.live_markets[m.slug] for m in self.markets if m.slug in self.live_markets],
            "retired": self._retired(),
        })
        self._ticks += 1
        if (self._ticks - 1) % max(1, b.print_every):
            return
        print(
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] kas ${self.ex.cash:8.2f} | inventaris ${inv:7.2f} | "
            f"handel P&L ${pnl_trading:+7.2f} | beloning ${rew:6.3f} | totaal ${pnl_trading + rew:+7.2f}"
        )
        for m in self.markets:
            ps = self.ex.p(m.condition_id)
            bq = f"{ps.bid.price:.3f}x{ps.bid.remaining:.0f}(q{ps.bid.queue_ahead:.0f})" if ps.bid else "   -   "
            aq = f"{ps.ask.price:.3f}x{ps.ask.remaining:.0f}(q{ps.ask.queue_ahead:.0f})" if ps.ask else "   -   "
            flag = " [GEBLOKKEERD]" if ps.blocked_until else ""
            print(f"    {m.slug[:40]:40} mid {self.mids.get(m.condition_id, 0):.3f}  bid {bq:22} ask {aq:22} netYES {ps.net_yes:+7.1f}{flag}")

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
                self.events.emit("error", msg=f"{type(e).__name__}: {e}"[:200])
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
    if hours >= 6:
        print(f"  Per dag (extrapol.): ${total / hours * 24:+.2f}   <- pas betrouwbaar na meerdere dagen")
    try:
        with open(state_path) as f:
            st = json.load(f)
        now = time.time()
        blocked = [v.get("slug") or cid[:10] for cid, v in st.get("positions", {}).items() if v.get("blocked_until", 0) > now]
        if blocked:
            print(f"  Geblokkeerd:       {', '.join(blocked)}")
    except (OSError, ValueError):
        pass
