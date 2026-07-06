"""Overnight research pass, run BEFORE the 7:00 ET session starts.

Rationale (and the reason it's news-first): companies deliberately release
market-moving headlines outside regular hours to avoid LULD volatility halts —
big caps after the 16:00 close, small caps during premarket. The 7:00 movers
screener only sees what has ALREADY printed volume on the free IEX feed, which
is late and thin for small caps. This pass instead asks "who has a fresh
catalyst since yesterday's close?", checks each name's latest print against
yesterday's close, and hands the bot a candidate list before the session opens.

Data: Alpaca's free news API (Benzinga feed) + the stocks snapshot endpoint.
Everything fails soft — an exception here must never stop the trading session.
"""

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import List, Optional
from zoneinfo import ZoneInfo

import requests

from sim.config import Config
from .scanner import is_common_stock

NEWS_URL = "https://data.alpaca.markets/v1beta1/news"
SNAPSHOT_URL = "https://data.alpaca.markets/v2/stocks/snapshots"
MAX_ARTICLES = 500           # a night of headlines; plenty for a candidate list
MAX_SYMBOLS = 100            # snapshot at most this many news names


def _headers(cfg: Config) -> dict:
    return {"Apca-Api-Key-Id": cfg.alpaca_key,
            "Apca-Api-Secret-Key": cfg.alpaca_secret}


def fetch_overnight_news(cfg: Config, since: datetime) -> List[dict]:
    """All articles since `since` (paginated). Each: {headline, symbols, created_at}."""
    articles, token = [], None
    while len(articles) < MAX_ARTICLES:
        params = {"start": since.isoformat(), "limit": 50, "sort": "desc"}
        if token:
            params["page_token"] = token
        r = requests.get(NEWS_URL, headers=_headers(cfg), params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        articles.extend(data.get("news", []))
        token = data.get("next_page_token")
        if not token:
            break
    return articles


def count_symbol_headlines(articles: List[dict]) -> dict:
    """symbol -> {count, latest_headline}. Pure, testable."""
    counts: Counter = Counter()
    latest: dict = {}
    for a in articles:                      # articles arrive newest-first
        for s in a.get("symbols", []):
            s = s.upper()
            counts[s] += 1
            latest.setdefault(s, a.get("headline", ""))
    return {s: {"count": counts[s], "headline": latest.get(s, "")} for s in counts}


def fetch_snapshots(cfg: Config, symbols: List[str]) -> dict:
    """symbol -> {price, prev_close} from the latest trade + prior daily bar."""
    out = {}
    for i in range(0, len(symbols), 50):    # endpoint takes ~100; stay modest
        batch = symbols[i:i + 50]
        r = requests.get(SNAPSHOT_URL, headers=_headers(cfg),
                         params={"symbols": ",".join(batch), "feed": cfg.data_feed},
                         timeout=20)
        r.raise_for_status()
        for sym, snap in (r.json() or {}).items():
            if not snap:
                continue
            trade = snap.get("latestTrade") or {}
            prev = snap.get("prevDailyBar") or {}
            price = float(trade.get("p") or 0) or float((snap.get("dailyBar") or {}).get("c") or 0)
            prev_close = float(prev.get("c") or 0)
            if price > 0 and prev_close > 0:
                out[sym] = {"price": price, "prev_close": prev_close}
    return out


def rank_candidates(news: dict, snaps: dict, cfg: Config) -> List[dict]:
    """Pure: keep tradable-band common stocks with a catalyst, rank by premarket
    move (who's confirming) then by headline count (who's most covered)."""
    rows = []
    for sym, meta in news.items():
        if not is_common_stock(sym) or sym not in snaps:
            continue
        s = snaps[sym]
        if not (cfg.pm_price_min <= s["price"] <= cfg.pm_price_max):
            continue
        gap = (s["price"] / s["prev_close"] - 1.0) * 100.0
        if gap < cfg.pm_prescan_min_gap_pct:
            continue                          # catalyst but the tape disagrees so far
        rows.append({"symbol": sym, "price": round(s["price"], 2),
                     "gap_pct": round(gap, 1), "headlines": meta["count"],
                     "headline": meta["headline"][:100]})
    rows.sort(key=lambda r: (r["gap_pct"], r["headlines"]), reverse=True)
    return rows[: cfg.pm_top_n]


def overnight_research(cfg: Config, now_et: Optional[datetime] = None) -> List[dict]:
    """The full pass: news since yesterday's close -> snapshots -> ranked list."""
    now_et = now_et or datetime.now(ZoneInfo(cfg.timezone))
    prior_close = (now_et - timedelta(days=1)).replace(hour=16, minute=0, second=0,
                                                       microsecond=0)
    if prior_close.weekday() >= 5:            # weekend boot: reach back to Friday
        prior_close -= timedelta(days=prior_close.weekday() - 4)
    articles = fetch_overnight_news(cfg, prior_close)
    news = count_symbol_headlines(articles)
    # snapshot the most-covered names first — coverage correlates with interest
    symbols = sorted(news, key=lambda s: news[s]["count"], reverse=True)[:MAX_SYMBOLS]
    snaps = fetch_snapshots(cfg, symbols)
    rows = rank_candidates(news, snaps, cfg)
    print(f"[prescan] {len(articles)} headlines since {prior_close:%Y-%m-%d %H:%M} ET "
          f"-> {len(news)} symbols -> {len(rows)} candidates in band")
    return rows


def research_md(rows: List[dict], now_et: datetime) -> str:
    out = [f"## Overnight research — {now_et:%Y-%m-%d %H:%M} ET", ""]
    if not rows:
        out.append("_No news-catalyst candidates in the tradable band yet; the 7:00 "
                   "movers scan stands alone today._")
        return "\n".join(out)
    out += ["| Symbol | Last | Move% | Headlines | Latest headline |",
            "|---|--:|--:|--:|---|"]
    for r in rows:
        out.append(f"| {r['symbol']} | {r['price']:.2f} | {r['gap_pct']:+.1f} | "
                   f"{r['headlines']} | {r['headline']} |")
    out.append("")
    out.append("_Seeded into the session watchlist alongside the 7:00 live movers scan._")
    return "\n".join(out)
