"""Free FX data via yfinance, plus a CSV path for offline use.

yfinance serves FX majors (e.g. "EURUSD=X"). Intraday history limits: 15m for
~the last 60 days, 1h for ~2 years. FX has no real volume, so the strategy is
price-only (no volume filters). Indexed in UTC.
"""

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
