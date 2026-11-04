"""Publish a static copy of the dashboard to GitHub Pages (branch gh-pages).

The page itself fetches live prices and order books straight from Polymarket;
the bot's own numbers (balance, positions, fills, events) come from JSON files
that this module refreshes every few minutes. The branch is kept at a single
commit (amend + force push) so the repository history does not grow.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time

from .dashboard import RANGES, WEB_DIR, Hub
from .scanner import Filters
from .strategy import StrategyConfig

KEEP_EVENTS = {"fill", "trades", "pause", "candidate", "scan_done", "error"}
MAX_HISTORY = 1500  # points per market chart
MIN_EVERY = 360  # GitHub Pages rebuilds at most ~10x per hour
STATIC_FLAG = "<script>window.POLYMAKER_STATIC = true;</script>"


def _dump(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, separators=(",", ":"), default=float)


def _safe(slug: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", slug)[:150]


def build_site(log_dir: str, out_dir: str, every: float) -> dict:
    hub = Hub(log_dir, StrategyConfig(), Filters(), scan_every=0)
    hub.load_once()
    hub.scan_state["status"] = "klaar"
    hub.scan_state["next_at"] = None

    data = os.path.join(out_dir, "data")
    if os.path.isdir(data):
        shutil.rmtree(data)
    os.makedirs(data)

    snap = hub.snapshot()
    snap.pop("last_seq", None)
    snap["published_at"] = time.time()
    snap["publish_every"] = every
    _dump(os.path.join(data, "snapshot.json"), snap)

    evs = [{k: v for k, v in e.items() if k != "seq"} for e in hub.events if e.get("type") in KEEP_EVENTS][-400:]
    _dump(os.path.join(data, "events.json"), {"events": evs})

    for name, secs in list(RANGES.items()) + [("alles", None)]:
        pts = hub.equity_series(time.time() - secs if secs else None, 800)
        _dump(os.path.join(data, f"equity_{name}.json"), {"points": pts})

    _dump(os.path.join(data, "fills.json"), {"fills": hub.fill_rows(None, 500)})
    _dump(os.path.join(data, "scan.json"), hub.scan_state)

    slugs = [m.get("slug") for m in (snap.get("live") or {}).get("markets", [])]
    slugs += [c["slug"] for c in hub.scan_state.get("candidates", [])]
    index = {}
    for slug in dict.fromkeys(s for s in slugs if s):
        m = hub.market(slug)
        h = m["history"]
        if len(h) > MAX_HISTORY:
            step = len(h) / MAX_HISTORY
            m["history"] = [h[int(i * step)] for i in range(MAX_HISTORY)] + [h[-1]]
        fname = _safe(slug) + ".json"
        index[slug] = fname
        _dump(os.path.join(data, "markets", fname), m)
    _dump(os.path.join(data, "markets", "index.json"), index)

    with open(os.path.join(WEB_DIR, "index.html")) as f:
        html = f.read()
    html = html.replace("</head>", STATIC_FLAG + "\n</head>", 1)
    with open(os.path.join(out_dir, "index.html"), "w") as f:
        f.write(html)
    open(os.path.join(out_dir, ".nojekyll"), "w").close()
    return snap


def _git(site: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=site, check=check, capture_output=True, text=True)


def default_remote() -> str | None:
    try:
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        r = subprocess.run(["git", "remote", "get-url", "origin"], cwd=here, capture_output=True, text=True, check=True)
        return r.stdout.strip() or None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def pages_url(remote: str) -> str | None:
    m = re.search(r"github\.com[:/]([^/]+)/([^/.]+?)(?:\.git)?/?$", remote or "")
    return f"https://{m.group(1).lower()}.github.io/{m.group(2)}/" if m else None


def publish_once(log_dir: str, site: str, remote: str, branch: str, every: float, runs: int = 0) -> None:
    os.makedirs(site, exist_ok=True)
    if not os.path.isdir(os.path.join(site, ".git")):
        _git(site, "init", "-q")
        _git(site, "symbolic-ref", "HEAD", f"refs/heads/{branch}")
        _git(site, "config", "user.name", "PolyMaker")
        _git(site, "config", "user.email", "polymaker@localhost")
    build_site(log_dir, site, every)
    _git(site, "add", "-A")
    has_commit = _git(site, "rev-parse", "--verify", "-q", "HEAD", check=False).returncode == 0
    msg = "Dashboard data " + time.strftime("%Y-%m-%d %H:%M")
    if has_commit:
        _git(site, "commit", "-q", "--amend", "--allow-empty", "-m", msg, "--date", "now")
    else:
        _git(site, "commit", "-q", "--allow-empty", "-m", msg)
    r = subprocess.run(["git", "push", "-q", "-f", remote, f"HEAD:refs/heads/{branch}"], cwd=site, text=True,
                       capture_output=not os.isatty(0))
    if r.returncode != 0:
        raise RuntimeError(f"git push mislukt: {(r.stderr or '').strip()[:300]}")
    if runs and runs % 30 == 0:  # drop the amended commits locally
        _git(site, "reflog", "expire", "--expire=now", "--all", check=False)
        _git(site, "gc", "-q", "--prune=now", check=False)


def run(log_dir: str, site: str, remote: str | None, branch: str, every: float, once: bool) -> int:
    remote = remote or default_remote()
    if not remote:
        print("Geen git-remote gevonden. Geef --remote https://github.com/<naam>/<repo>.git op.")
        return 1
    every = max(MIN_EVERY, every)
    url = pages_url(remote)
    runs = 0
    while True:
        t0 = time.time()
        try:
            publish_once(log_dir, site, remote, branch, every, runs)
            runs += 1
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] gepubliceerd naar {branch}" + (f" -> {url}" if url else ""), flush=True)
        except (RuntimeError, subprocess.CalledProcessError, OSError) as e:
            err = getattr(e, "stderr", "") or str(e)
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] publiceren mislukt: {err.strip()[:300]}", flush=True)
            if once:
                return 1
        if once:
            return 0
        time.sleep(max(30, every - (time.time() - t0)))
