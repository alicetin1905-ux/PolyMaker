"""Local web dashboard for the paper bot (stdlib only).

It tails the bot's log directory (events.jsonl, live.json, equity.csv, fills.csv),
runs the market scanner in the background, and serves a single-page UI.
"""
from __future__ import annotations

import csv
import json
import os
import re
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .api import PolymarketAPI
from .events import write_json_atomic
from .scanner import Filters, scan
from .strategy import StrategyConfig

WEB_DIR = os.path.join(os.path.dirname(__file__), "web")
MID_SAMPLE_SECS = 60  # keep one mid per market per minute for history charts
MID_HISTORY_POINTS = 60 * 24 * 7  # one week


class CsvTail:
    """Incrementally reads a growing CSV file (header + rows)."""

    def __init__(self, path: str, keep: int):
        self.path, self.keep = path, keep
        self.rows: deque = deque(maxlen=keep)
        self.pos = 0
        self.header: list[str] | None = None
        self.ino = None
        self.lock = threading.Lock()

    def read(self) -> list[list[str]]:
        with self.lock:
            try:
                st = os.stat(self.path)
            except FileNotFoundError:
                self.rows.clear()
                self.pos, self.header, self.ino = 0, None, None
                return []
            if st.st_ino != self.ino or st.st_size < self.pos:  # new or truncated file (reset)
                self.rows.clear()
                self.pos, self.header, self.ino = 0, None, st.st_ino
            if st.st_size > self.pos:
                with open(self.path, newline="") as f:
                    f.seek(self.pos)
                    chunk = f.read()
                    # only consume complete lines
                    cut = chunk.rfind("\n")
                    if cut >= 0:
                        chunk = chunk[: cut + 1]
                        self.pos += len(chunk.encode())
                        for row in csv.reader(chunk.splitlines()):
                            if not row:
                                continue
                            if self.header is None:
                                self.header = row
                            else:
                                self.rows.append(row)
            return list(self.rows)


class Hub:
    def __init__(self, log_dir: str, scfg: StrategyConfig, filters: Filters, scan_every: float, n_candidates: int = 25):
        self.log_dir = log_dir
        self.scfg, self.filters, self.scan_every, self.n_candidates = scfg, filters, scan_every, n_candidates
        self.api = PolymarketAPI()
        self.lock = threading.Lock()
        self.events: deque = deque(maxlen=4000)
        self.seq = 0
        self.mids: dict[str, deque] = defaultdict(lambda: deque(maxlen=MID_HISTORY_POINTS))
        self.last_mid: dict[str, float] = {}
        self.equity = CsvTail(os.path.join(log_dir, "equity.csv"), keep=200_000)
        self.fills = CsvTail(os.path.join(log_dir, "fills.csv"), keep=5000)
        self.scan_state: dict = {"status": "wacht", "phase": None, "done": 0, "total": 0,
                                 "last_done": None, "next_at": time.time() + 5, "candidates": [], "stats": {}}
        self.known_candidates: set[str] = set()
        self.book_cache: dict[str, tuple[float, dict]] = {}
        self.hist_cache: dict[str, tuple[float, list]] = {}
        self.scan_path = os.path.join(log_dir, "scan.json")
        try:  # keep the last scan results across restarts
            with open(self.scan_path) as f:
                saved = json.load(f)
            saved.update(status="klaar", phase=None, next_at=time.time() + 5)
            self.scan_state.update(saved)
            self.known_candidates = {c["slug"] for c in saved.get("candidates", [])}
        except (FileNotFoundError, ValueError, KeyError):
            pass

    # ---- events ----------------------------------------------------------
    def _push(self, ev: dict) -> None:
        with self.lock:
            self.seq += 1
            ev["seq"] = self.seq
            self.events.append(ev)

    def _ingest(self, ev: dict, live: bool) -> None:
        if ev.get("type") == "scan" and ev.get("mid") is not None:
            slug, mid, ts = ev["m"], ev["mid"], ev["ts"]
            hist = self.mids[slug]
            if not hist or ts - hist[-1][0] >= MID_SAMPLE_SECS or abs(mid - hist[-1][1]) > 1e-9:
                hist.append((ts, mid))
            prev = self.last_mid.get(slug)
            ev["moved"] = prev is not None and abs(prev - mid) > 1e-9
            self.last_mid[slug] = mid
        if live:
            self._push(ev)

    def load_once(self) -> None:
        """Read the whole event log once (used by the GitHub Pages publisher)."""
        path = os.path.join(self.log_dir, "events.jsonl")
        for p in (path + ".1", path):
            try:
                with open(p) as f:
                    for line in f:
                        try:
                            self._ingest(json.loads(line), live=True)
                        except ValueError:
                            pass
            except FileNotFoundError:
                pass

    def tail_events(self) -> None:
        path = os.path.join(self.log_dir, "events.jsonl")
        # history: older rotated file + current file (mid history only)
        for p in (path + ".1", path):
            try:
                with open(p) as f:
                    for line in f:
                        try:
                            self._ingest(json.loads(line), live=False)
                        except ValueError:
                            pass
            except FileNotFoundError:
                pass
        f, ino = None, None
        pos = os.path.getsize(path) if os.path.exists(path) else 0
        while True:
            try:
                st = os.stat(path)
                if f is None or st.st_ino != ino or st.st_size < pos:
                    rotated = f is not None or st.st_size < pos
                    if f:
                        f.close()
                    f, ino = open(path), st.st_ino
                    if rotated:
                        pos = 0
                    f.seek(pos)
                buf = f.read()
                if buf:
                    cut = buf.rfind("\n")
                    if cut >= 0:
                        complete = buf[: cut + 1]
                        pos += len(complete.encode())
                        f.seek(pos)
                        for line in complete.splitlines():
                            try:
                                self._ingest(json.loads(line), live=True)
                            except ValueError:
                                pass
                    else:
                        f.seek(pos)
            except FileNotFoundError:
                f, pos = None, 0
            time.sleep(0.4)

    # ---- scanner -----------------------------------------------------------
    def scanner_loop(self) -> None:
        time.sleep(5)
        while True:
            self.run_scan()
            self.scan_state["next_at"] = time.time() + self.scan_every
            time.sleep(self.scan_every)

    def run_scan(self) -> None:
        st = self.scan_state
        st.update(status="scant", phase="markten", done=0, total=0, started=time.time())
        self._push({"ts": time.time(), "type": "scan_start"})
        last_emit = [0.0]

        def progress(phase, done, total):
            st.update(phase=phase, done=done, total=total)
            now = time.time()
            if now - last_emit[0] > 0.7:
                last_emit[0] = now
                self._push({"ts": now, "type": "scan_progress", "phase": phase, "done": done, "total": total})

        stats: dict = {}
        try:
            cands = scan(self.api, self.scfg, self.filters, want=self.n_candidates, verbose=False,
                         progress=progress, stats=stats)
        except Exception as e:  # network trouble etc.
            st.update(status="fout", error=f"{type(e).__name__}: {e}"[:200])
            self._push({"ts": time.time(), "type": "error", "msg": f"scanner: {e}"[:200]})
            return
        live = self.read_live() or {}
        active = {m.get("slug") for m in live.get("markets", [])}
        out = []
        for i, c in enumerate(cands, 1):
            m = c.market
            is_new = m.slug not in self.known_candidates
            row = {
                "rank": i, "slug": m.slug, "question": m.question, "yes_token": m.yes_token,
                "reward_day": round(c.exp_reward_day, 3), "per_100": round(c.reward_per_100, 3),
                "share": round(c.share, 4), "pool": m.reward_daily, "capital": round(c.capital, 2),
                "mid": c.mid, "bid": c.bid, "ask": c.ask, "vol_c": c.vol_c, "jump_c": c.jump_c,
                "days_left": round(c.days_left, 1), "active": m.slug in active,
                "new": is_new and bool(self.known_candidates),
            }
            out.append(row)
            if row["new"]:  # first scan only fills the baseline, later scans announce what's new
                self._push({"ts": time.time(), "type": "candidate", "m": m.slug, "q": m.question[:120],
                            "reward_day": row["reward_day"], "new": row["new"], "rank": i})
        self.known_candidates |= {r["slug"] for r in out}
        st.update(status="klaar", phase=None, candidates=out, stats=stats, last_done=time.time(), error=None)
        try:
            write_json_atomic(self.scan_path, {"candidates": out, "stats": stats, "last_done": st["last_done"]})
        except OSError:
            pass
        self._push({"ts": time.time(), "type": "scan_done", "total": stats.get("total"), "passed": len(out)})

    # ---- data access -------------------------------------------------------
    def read_live(self) -> dict | None:
        try:
            with open(os.path.join(self.log_dir, "live.json")) as f:
                return json.load(f)
        except (FileNotFoundError, ValueError):
            return None

    def equity_series(self, since: float | None, max_points: int = 600) -> list[list[float]]:
        rows = self.equity.read()
        pts = []
        for r in rows:
            try:
                t = float(r[0])
            except (ValueError, IndexError):
                continue
            if since and t < since:
                continue
            # time, total P&L, rewards, trading P&L, equity
            pts.append([t, float(r[6]), float(r[3]), float(r[5]), float(r[4])])
        if len(pts) > max_points:
            step = len(pts) / max_points
            pts = [pts[int(i * step)] for i in range(max_points)] + [pts[-1]]
        return pts

    def fill_rows(self, slug: str | None = None, limit: int = 300) -> list[dict]:
        rows = self.fills.read()
        out = []
        for r in reversed(rows):
            if len(r) < 6 or (slug and r[1] != slug):
                continue
            out.append({"time": r[0], "market": r[1], "side": r[2], "price": float(r[3]), "size": float(r[4]), "reason": r[5]})
            if len(out) >= limit:
                break
        return out

    def sparks(self, slugs: list[str], seconds: float = 6 * 3600, points: int = 60) -> dict:
        now = time.time()
        out = {}
        for s in slugs:
            h = [p for p in self.mids.get(s, ()) if p[0] >= now - seconds]
            if len(h) > points:
                step = len(h) / points
                h = [h[int(i * step)] for i in range(points)] + [h[-1]]
            out[s] = [[round(t), m] for t, m in h]
        return out

    def snapshot(self) -> dict:
        live = self.read_live()
        slugs = [m.get("slug") for m in (live or {}).get("markets", [])]
        st = dict(self.scan_state)
        st["candidates"] = st.get("candidates", [])[:10]
        with self.lock:
            last = self.seq
        return {
            "server_time": time.time(), "live": live, "last_seq": last,
            "equity": self.equity_series(time.time() - 24 * 3600, 300),
            "fills": self.fill_rows(limit=30), "scan": st, "sparks": self.sparks(slugs),
        }

    def events_after(self, after: int) -> dict:
        with self.lock:
            last = self.seq
            if after > last:  # dashboard restarted: tell the client to resync
                return {"reset": True, "last": last, "events": []}
            evs = [e for e in self.events if e["seq"] > after][-600:]
        return {"last": last, "events": evs}

    def market(self, slug: str) -> dict:
        live = self.read_live() or {}
        entry = next((m for m in live.get("markets", []) if m.get("slug") == slug), None)
        cand = next((c for c in self.scan_state.get("candidates", []) if c["slug"] == slug), None)
        hist = [[round(t), m] for t, m in self.mids.get(slug, ())]
        return {"live": entry, "candidate": cand, "history": hist, "fills": self.fill_rows(slug, 500),
                "settings": live.get("settings")}

    def history(self, token: str) -> list:
        now = time.time()
        hit = self.hist_cache.get(token)
        if hit and now - hit[0] < 300:
            return hit[1]
        pts = [[t, p] for t, p in self.api.price_history(token, "1w", 60)]
        if len(self.hist_cache) > 100:
            self.hist_cache.clear()
        self.hist_cache[token] = (now, pts)
        return pts

    def book(self, token: str) -> dict:
        now = time.time()
        hit = self.book_cache.get(token)
        if hit and now - hit[0] < 4:
            return hit[1]
        b = self.api.book(token)
        data = {"bids": [[l.price, l.size] for l in b.bids[:25]], "asks": [[l.price, l.size] for l in b.asks[:25]],
                "mid": b.mid, "tick": b.tick, "time": now}
        self.book_cache[token] = (now, data)
        if len(self.book_cache) > 200:
            self.book_cache.clear()
        return data


RANGES = {"1u": 3600, "24u": 86400, "7d": 7 * 86400, "30d": 30 * 86400}


def make_handler(hub: Hub):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj, separators=(",", ":"), default=float).encode(), "application/json")

        def do_GET(self):  # noqa: N802
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if u.path in ("/", "/index.html"):
                    with open(os.path.join(WEB_DIR, "index.html"), "rb") as f:
                        self._send(200, f.read(), "text/html; charset=utf-8")
                elif u.path == "/api/snapshot":
                    self._json(hub.snapshot())
                elif u.path == "/api/events":
                    self._json(hub.events_after(int(q.get("after", "0") or 0)))
                elif u.path == "/api/equity":
                    secs = RANGES.get(q.get("range", "24u"))
                    self._json({"points": hub.equity_series(time.time() - secs if secs else None, 800)})
                elif u.path == "/api/market":
                    self._json(hub.market(q.get("slug", "")))
                elif u.path == "/api/book":
                    tok = q.get("token", "")
                    if not re.fullmatch(r"\d{10,100}", tok):
                        return self._json({"error": "ongeldig token"}, 400)
                    self._json(hub.book(tok))
                elif u.path == "/api/history":
                    tok = q.get("token", "")
                    if not re.fullmatch(r"\d{10,100}", tok):
                        return self._json({"error": "ongeldig token"}, 400)
                    self._json({"points": hub.history(tok)})
                elif u.path == "/api/scan":
                    self._json(hub.scan_state)
                elif u.path == "/api/fills":
                    self._json({"fills": hub.fill_rows(q.get("slug") or None, 500)})
                else:
                    self._json({"error": "niet gevonden"}, 404)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                try:
                    self._json({"error": f"{type(e).__name__}: {e}"}, 500)
                except Exception:
                    pass

    return Handler


def serve(log_dir: str, host: str, port: int, scfg: StrategyConfig, filters: Filters, scan_every: float) -> None:
    os.makedirs(log_dir, exist_ok=True)
    hub = Hub(log_dir, scfg, filters, scan_every)
    threading.Thread(target=hub.tail_events, daemon=True).start()
    if scan_every > 0:
        threading.Thread(target=hub.scanner_loop, daemon=True).start()
    try:
        srv = ThreadingHTTPServer((host, port), make_handler(hub))
    except OSError as e:
        print(f"Poort {port} is niet beschikbaar ({e.strerror}). Kies een andere poort met --port "
              f"(of op de Mac: DASH_PORT=8791 ./mac/bot.sh apply).")
        raise SystemExit(2)
    srv.daemon_threads = True
    shown = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
    print(f"Dashboard: http://{shown}:{port}  (logmap: {os.path.abspath(log_dir)})")
    if host == "0.0.0.0":
        print("Ook bereikbaar vanaf andere apparaten in je netwerk via http://<naam-van-je-mac>.local:%d" % port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
