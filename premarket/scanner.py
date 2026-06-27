"""Find the morning's momentum candidates.

Two paths share one pure ranking function:
- backtest: gaps are reconstructed from daily bars (yfinance), since there is no
  free historical record of "who gapped".
- live: Alpaca's market-movers screener supplies the gainers directly.
"""

from typing import List

from sim.config import Config


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


def scan_live(cfg: Config) -> List[str]:
    """Live watchlist via Alpaca's market-movers screener (gainers)."""
    from alpaca.data.historical.screener import ScreenerClient
    from alpaca.data.requests import MarketMoversRequest

    client = ScreenerClient(cfg.alpaca_key, cfg.alpaca_secret)
    resp = client.get_market_movers(MarketMoversRequest(top=50))
    rows = [
        {"symbol": m.symbol, "ref_price": float(m.price),
         "gap_pct": float(m.percent_change)}
        for m in resp.gainers
    ]
    return [r["symbol"] for r in rank_gappers(rows, cfg)]
