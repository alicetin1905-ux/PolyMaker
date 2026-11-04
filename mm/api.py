"""Read-only client for Polymarket's public APIs (no account or keys needed)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import requests

CLOB = "https://clob.polymarket.com"
DATA = "https://data-api.polymarket.com/v2"


@dataclass
class Level:
    price: float
    size: float


@dataclass
class Book:
    token_id: str
    bids: list[Level]  # best (highest) first
    asks: list[Level]  # best (lowest) first
    tick: float
    min_order_size: float
    ts: float = field(default_factory=time.time)

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2

    def size_at(self, side: str, price: float) -> float:
        levels = self.bids if side == "bid" else self.asks
        return sum(l.size for l in levels if abs(l.price - price) < 1e-9)


@dataclass
class Market:
    condition_id: str
    question: str
    slug: str
    yes_token: str
    no_token: str
    end_date: str | None
    reward_daily: float  # USDC per day for the whole market
    reward_min_size: float  # shares
    reward_max_spread: float  # cents from midpoint
    tick: float
    min_order_size: float
    neg_risk: bool


class PolymarketAPI:
    def __init__(self, timeout: float = 15.0, retries: int = 3):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "paper-mm-bot/0.1"
        self.timeout = timeout
        self.retries = retries

    def _req(self, method: str, url: str, **kw):
        last = None
        for i in range(self.retries):
            try:
                r = self.s.request(method, url, timeout=self.timeout, **kw)
                if r.status_code == 429 or r.status_code >= 500:
                    raise requests.HTTPError(f"{r.status_code} {r.text[:200]}")
                r.raise_for_status()
                return r.json()
            except (requests.RequestException, ValueError) as e:
                last = e
                time.sleep(1.5 * (2**i))
        raise RuntimeError(f"API request failed: {url}: {last}")

    # ---- markets -------------------------------------------------------
    def reward_markets(self, max_pages: int = 10) -> list[Market]:
        """All active markets that currently pay liquidity rewards."""
        out: list[Market] = []
        cursor = ""
        for _ in range(max_pages):
            d = self._req("GET", f"{CLOB}/sampling-markets", params={"next_cursor": cursor})
            for m in d.get("data", []):
                mk = parse_market(m)
                if mk:
                    out.append(mk)
            cursor = d.get("next_cursor") or ""
            if not cursor or cursor == "LTE=":
                break
        return out

    # ---- order books ---------------------------------------------------
    def books(self, token_ids: list[str]) -> dict[str, Book]:
        res: dict[str, Book] = {}
        for i in range(0, len(token_ids), 50):
            chunk = token_ids[i : i + 50]
            d = self._req("POST", f"{CLOB}/books", json=[{"token_id": t} for t in chunk])
            for b in d:
                bk = parse_book(b)
                res[bk.token_id] = bk
        return res

    def book(self, token_id: str) -> Book:
        return parse_book(self._req("GET", f"{CLOB}/book", params={"token_id": token_id}))

    # ---- trades --------------------------------------------------------
    def trades(self, condition_id: str, limit: int = 100) -> list[dict]:
        d = self._req("GET", f"{DATA}/trades", params={"condition": condition_id, "limit": limit})
        return d.get("data", []) if isinstance(d, dict) else d

    def price_history(self, token_id: str, interval: str = "1w", fidelity: int = 60) -> list[tuple[int, float]]:
        d = self._req(
            "GET",
            f"{CLOB}/prices-history",
            params={"market": token_id, "interval": interval, "fidelity": fidelity},
        )
        return [(h["t"], float(h["p"])) for h in d.get("history", [])]


def parse_market(m: dict) -> Market | None:
    if not (m.get("active") and not m.get("closed") and m.get("accepting_orders")):
        return None
    toks = m.get("tokens") or []
    if len(toks) != 2:
        return None
    yes = next((t for t in toks if str(t.get("outcome")).lower() == "yes"), toks[0])
    no = toks[1] if yes is toks[0] else toks[0]
    rw = m.get("rewards") or {}
    rate = sum(float(r.get("rewards_daily_rate", 0)) for r in rw.get("rates") or [])
    return Market(
        condition_id=m["condition_id"],
        question=m.get("question", ""),
        slug=m.get("market_slug", ""),
        yes_token=yes["token_id"],
        no_token=no["token_id"],
        end_date=m.get("end_date_iso"),
        reward_daily=rate,
        reward_min_size=float(rw.get("min_size") or 0),
        reward_max_spread=float(rw.get("max_spread") or 0),
        tick=float(m.get("minimum_tick_size") or 0.01),
        min_order_size=float(m.get("minimum_order_size") or 5),
        neg_risk=bool(m.get("neg_risk")),
    )


def parse_book(b: dict) -> Book:
    bids = sorted((Level(float(x["price"]), float(x["size"])) for x in b.get("bids", [])), key=lambda l: -l.price)
    asks = sorted((Level(float(x["price"]), float(x["size"])) for x in b.get("asks", [])), key=lambda l: l.price)
    return Book(
        token_id=b.get("asset_id", ""),
        bids=bids,
        asks=asks,
        tick=float(b.get("tick_size") or 0.01),
        min_order_size=float(b.get("min_order_size") or 5),
    )
