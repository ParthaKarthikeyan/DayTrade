"""Free deep-history crypto OHLC.

Coinbase Exchange's public candles endpoint needs no key, is reachable from US
runners, and goes back years. Max 300 candles per request, newest-first, so we
paginate backwards. Granularity is in seconds (60, 300, 900, 3600, 21600, 86400).
yfinance (BTC-USD, ~recent intraday) is a shallow fallback.
"""

import time
from datetime import datetime, timedelta, timezone

import pandas as pd

UTC = "UTC"
GRAN = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "6h": 21600, "1d": 86400}


def _parse(rows) -> pd.DataFrame:
    """Coinbase rows are [time, low, high, open, close, volume] (unix seconds)."""
    out = []
    for c in rows:
        out.append((pd.Timestamp(int(c[0]), unit="s", tz="UTC"),
                    float(c[3]), float(c[2]), float(c[1]), float(c[4]), float(c[5])))
    if not out:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame(out, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df = df.set_index("timestamp")
    return df[~df.index.duplicated()].sort_index()


def get_coinbase(product: str, interval: str = "1h", years: float = 4.0) -> pd.DataFrame:
    """Fetch deep historical candles from Coinbase (paginated, free, no key)."""
    import requests

    gran = GRAN[interval]
    url = f"https://api.exchange.coinbase.com/products/{product}/candles"
    headers = {"User-Agent": "daytrade-research"}
    end = datetime.now(timezone.utc)
    target = end - timedelta(days=int(years * 365))
    frames, cur_end = [], end
    for _ in range(3000):                       # safety cap on pages
        cur_start = max(cur_end - timedelta(seconds=gran * 300), target)
        params = {"granularity": gran, "start": cur_start.isoformat(),
                  "end": cur_end.isoformat()}
        for attempt in range(3):                # tolerate transient 429/5xx
            r = requests.get(url, params=params, headers=headers, timeout=30)
            if r.status_code == 200:
                break
            time.sleep(0.5 * (attempt + 1))
        r.raise_for_status()
        rows = r.json()
        if not rows:
            break
        frames.append(_parse(rows))
        oldest = min(int(c[0]) for c in rows)
        cur_end = datetime.fromtimestamp(oldest, tz=timezone.utc) - timedelta(seconds=gran)
        if cur_end <= target:
            break
        time.sleep(0.15)                        # be gentle on the public endpoint
    if not frames:
        raise RuntimeError(f"Coinbase returned no candles for {product} ({interval})")
    df = pd.concat(frames)
    return df[~df.index.duplicated()].sort_index()


def get_yf(symbol: str, period: str = "60d", interval: str = "1h") -> pd.DataFrame:
    import yfinance as yf
    df = yf.download(symbol, period=period, interval=interval,
                     auto_adjust=False, progress=False)
    if df.empty:
        raise RuntimeError(f"yfinance returned no data for {symbol}")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
    df = df.tz_localize(UTC) if df.index.tz is None else df.tz_convert(UTC)
    return df.dropna().sort_index()
