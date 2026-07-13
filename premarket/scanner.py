"""Find the morning's momentum candidates.

Two paths share one pure ranking function:
- backtest: gaps are reconstructed from daily bars (yfinance), since there is no
  free historical record of "who gapped".
- live: Alpaca's market-movers screener supplies the gainers directly.
"""

from typing import List

from sim.config import Config


def is_common_stock(symbol: str) -> bool:
    """Exclude warrants/units/rights — they trade thin and follow different rules.
    Catches the suffix conventions seen in live watchlists: '.WS', '.U', '.R',
    and 5-letter tickers ending in W/U/R (e.g. OPTXW, NTRBW). Also rejects
    exchange-prefixed foreign listings from news feeds (e.g. 'TSX:ATZ') — one
    of those in a snapshot batch 400s the whole request."""
    s = symbol.upper()
    if "." in s or "/" in s or ":" in s:
        return False
    return not (len(s) == 5 and s[-1] in "WUR")


def rank_gappers(rows: List[dict], cfg: Config) -> List[dict]:
    """Pure: filter by price band + min gap %, then rank by gap descending.

    Each row needs: symbol, ref_price (the price used for the band), gap_pct.
    """
    keep = [
        r for r in rows
        if cfg.pm_price_min <= r["ref_price"] <= cfg.pm_price_max
        and r["gap_pct"] >= cfg.pm_min_gap_pct
    ]
    keep.sort(key=lambda r: r["gap_pct"], reverse=True)
    return keep[: cfg.pm_top_n]


def float_ok(symbol: str, cfg: Config) -> bool:
    """Low-float filter via yfinance. Fail-open: keep the name if data is missing."""
    try:
        import yfinance as yf
        info = yf.Ticker(symbol).get_info()
        shares = info.get("floatShares") or info.get("sharesOutstanding")
        if not shares:
            return True
        return float(shares) <= cfg.pm_max_float_shares
    except Exception:
        return True


def has_recent_news(symbol: str) -> bool:
    """Weak free news check (yfinance). Fail-open on error."""
    try:
        import yfinance as yf
        return bool(yf.Ticker(symbol).news)
    except Exception:
        return True


def scan_live(cfg: Config) -> List[str]:
    """Live whole-market watchlist via Alpaca's movers screener, enriched for free.

    Filters to Ross's criteria we can get without paid data: price band, % gain,
    top movers, and low float (yfinance). News is optional (pm_require_news).
    """
    from alpaca.data.historical.screener import ScreenerClient
    from alpaca.data.requests import MarketMoversRequest

    client = ScreenerClient(cfg.alpaca_key, cfg.alpaca_secret)
    resp = client.get_market_movers(MarketMoversRequest(top=50))
    cands = [
        {"symbol": m.symbol, "ref_price": float(m.price), "gap_pct": float(m.percent_change)}
        for m in resp.gainers
        if cfg.pm_price_min <= float(m.price) <= cfg.pm_price_max
        and float(m.percent_change) >= cfg.pm_min_gain_pct
    ]
    cands.sort(key=lambda r: r["gap_pct"], reverse=True)

    out: List[str] = []
    for c in cands:
        if not is_common_stock(c["symbol"]):
            continue
        if not float_ok(c["symbol"], cfg):
            continue
        if cfg.pm_require_news and not has_recent_news(c["symbol"]):
            continue
        out.append(c["symbol"])
        if len(out) >= cfg.pm_top_n:
            break
    return out
