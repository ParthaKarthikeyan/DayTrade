"""Daily futures/proxy data via yfinance.

yfinance serves continuous front-month futures (ES=F, GC=F, …) and crypto (BTC-USD)
with long daily history — enough for a diversified daily trend backtest. Continuous
contracts carry roll artefacts (a known caveat for absolute returns), but daily trend
direction over years is robust to them. Indexed in UTC.
"""

import pandas as pd

UTC = "UTC"


def get_daily(ticker: str, years: int = 12) -> pd.DataFrame:
    import yfinance as yf
    df = yf.download(ticker, period=f"{int(years)}y", interval="1d",
                     auto_adjust=False, progress=False)
    if df is None or df.empty:
        raise RuntimeError(f"yfinance returned no data for {ticker}")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
    df = df.tz_localize(UTC) if df.index.tz is None else df.tz_convert(UTC)
    return df.dropna().sort_index()
