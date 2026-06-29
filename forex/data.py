"""Free FX data via yfinance, plus a CSV path for offline use.

yfinance serves FX majors (e.g. "EURUSD=X"). Intraday history limits: 15m for
~the last 60 days, 1h for ~2 years. FX has no real volume, so the strategy is
price-only (no volume filters). Indexed in UTC.
"""

import os
from datetime import datetime, timedelta, timezone

import pandas as pd

UTC = "UTC"


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.lower)[["open", "high", "low", "close"]]
    df = df.tz_localize(UTC) if df.index.tz is None else df.tz_convert(UTC)
    return df.dropna().sort_index()


def get_fx(pair: str, period: str = "60d", interval: str = "15m") -> pd.DataFrame:
    import yfinance as yf
    df = yf.download(pair, period=period, interval=interval,
                     auto_adjust=False, progress=False)
    if df.empty:
        raise RuntimeError(f"yfinance returned no data for {pair} ({interval}/{period})")
    return _normalize(df)


def from_csv(path: str) -> pd.DataFrame:
    """CSV with a 'timestamp' column + open/high/low/close (UTC)."""
    df = pd.read_csv(path, parse_dates=["timestamp"], index_col="timestamp")
    return _normalize(df)


# --- OANDA (deep free history) --------------------------------------------
def to_oanda(symbol: str) -> str:
    """Map a yfinance FX symbol to OANDA's instrument name: EURUSD=X -> EUR_USD."""
    s = symbol.replace("=X", "").upper()
    return f"{s[:3]}_{s[3:6]}" if "_" not in s and len(s) >= 6 else s


def _parse_candles(payload: dict) -> pd.DataFrame:
    """Parse an OANDA candles response into a UTC OHLC frame (testable, no I/O)."""
    rows = []
    for c in payload.get("candles", []):
        if not c.get("complete"):
            continue
        m = c["mid"]
        rows.append((pd.Timestamp(c["time"]), float(m["o"]), float(m["h"]),
                     float(m["l"]), float(m["c"])))
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close"])
    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close"]).set_index("timestamp")
    df = df.tz_convert(UTC) if df.index.tz is not None else df.tz_localize(UTC)
    return df[~df.index.duplicated()].sort_index()


def get_oanda(instrument: str, years: float = 2.0, granularity: str = "M15") -> pd.DataFrame:
    """Fetch deep historical candles from OANDA (paginated, free with a token)."""
    import requests

    token = os.getenv("OANDA_API_KEY", "")
    if not token:
        raise RuntimeError("OANDA_API_KEY not set")
    env = os.getenv("OANDA_ENV", "practice").lower()
    base = "https://api-fxpractice.oanda.com" if env != "live" else "https://api-fxtrade.oanda.com"
    url = f"{base}/v3/instruments/{instrument}/candles"
    headers = {"Authorization": f"Bearer {token}"}

    end = datetime.now(timezone.utc)
    cursor = end - timedelta(days=int(years * 365))
    frames = []
    for _ in range(200):                       # safety cap on pages
        params = {"granularity": granularity, "price": "M", "count": 5000,
                  "from": cursor.isoformat().replace("+00:00", "Z")}
        r = requests.get(url, headers=headers, params=params, timeout=30)
        r.raise_for_status()
        candles = r.json().get("candles", [])
        if not candles:
            break
        frames.append(_parse_candles({"candles": candles}))
        last = pd.Timestamp(candles[-1]["time"]).to_pydatetime()
        nxt = last + timedelta(minutes=1)
        if nxt <= cursor or nxt >= end:
            break
        cursor = nxt
    if not frames:
        raise RuntimeError(f"OANDA returned no candles for {instrument}")
    df = pd.concat(frames)
    return df[~df.index.duplicated()].sort_index()
