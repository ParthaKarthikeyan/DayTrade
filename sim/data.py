"""Market data adapters.

Backtest needs one trading day of 1-minute OHLCV bars indexed by US/Eastern
timestamps with columns: open, high, low, close, volume.

Priority: Alpaca (if keys present) -> yfinance fallback -> local CSV. The CSV
path keeps the simulator fully runnable offline and in tests.
"""

import time
from datetime import datetime

import pandas as pd

from .config import Config

REGULAR_START = "09:30"
REGULAR_END = "16:00"


def _filter_regular_hours(df: pd.DataFrame, tz: str) -> pd.DataFrame:
    if df.index.tz is None:
        df = df.tz_localize("UTC")
    df = df.tz_convert(tz)
    df = df.between_time(REGULAR_START, REGULAR_END)
    return df[["open", "high", "low", "close", "volume"]].sort_index()


def from_csv(path: str, tz: str = "America/New_York") -> pd.DataFrame:
    """Load bars from CSV. Needs a 'timestamp' column plus OHLCV columns."""
    df = pd.read_csv(path, parse_dates=["timestamp"], index_col="timestamp")
    df.columns = [c.lower() for c in df.columns]
    return _filter_regular_hours(df, tz)


def from_alpaca(symbol: str, date: str, cfg: Config) -> pd.DataFrame:
    """Fetch 1-minute bars for a single date from Alpaca's historical API."""
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    client = StockHistoricalDataClient(cfg.alpaca_key, cfg.alpaca_secret)
    start = pd.Timestamp(f"{date} 00:00", tz=cfg.timezone).tz_convert("UTC")
    end = pd.Timestamp(f"{date} 23:59", tz=cfg.timezone).tz_convert("UTC")
    req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Minute,
                           start=start.to_pydatetime(), end=end.to_pydatetime(),
                           feed=cfg.data_feed)

    # Retry transient failures (e.g. 429 rate limits) with backoff, so throttled
    # days aren't silently skipped during a large multi-day/multi-symbol run.
    last_err = None
    for attempt in range(5):
        try:
            bars = client.get_stock_bars(req).df
            break
        except Exception as e:  # noqa: BLE001 - APIError/HTTP/transport all retryable
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    else:
        raise RuntimeError(f"Alpaca request failed for {symbol} {date}: {last_err}")

    time.sleep(0.3)  # light pacing to stay under the free 200 req/min limit
    if bars.empty:
        raise RuntimeError(f"Alpaca returned no bars for {symbol} on {date}")
    # Multi-index (symbol, timestamp) -> single symbol frame.
    bars = bars.reset_index(level=0, drop=True)
    return _filter_regular_hours(bars, cfg.timezone)


def from_yfinance(symbol: str, date: str, tz: str = "America/New_York") -> pd.DataFrame:
    """Fallback: yfinance 1m bars (only available for ~last 30 days)."""
    import yfinance as yf

    start = pd.Timestamp(date)
    end = start + pd.Timedelta(days=1)
    df = yf.download(symbol, start=start.date(), end=end.date(),
                     interval="1m", progress=False, auto_adjust=False)
    if df.empty:
        raise RuntimeError(
            f"yfinance returned no 1m bars for {symbol} on {date} "
            "(1m history is limited to ~30 days)")
    df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in df.columns]
    return _filter_regular_hours(df, tz)


def get_day_bars(symbol: str, date: str, cfg: Config, csv: str = None) -> pd.DataFrame:
    """Resolve bars from the best available source."""
    if csv:
        return from_csv(csv, cfg.timezone)
    if cfg.has_alpaca:
        return from_alpaca(symbol, date, cfg)
    return from_yfinance(symbol, date, cfg.timezone)
