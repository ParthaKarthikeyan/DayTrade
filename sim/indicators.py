"""Technical indicators used by the strategy.

All indicators are *causal*: the value at bar i depends only on bars <= i, so
computing them once over the whole day and then iterating bar-by-bar introduces
no look-ahead bias.
"""

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average (standard adjust=False recursion)."""
    return series.ewm(span=period, adjust=False).mean()


def vwap(df: pd.DataFrame) -> pd.Series:
    """Session VWAP from the start of the frame, using the typical price.

    Expects columns: high, low, close, volume. Resets implicitly because each
    backtest frame is a single trading day.
    """
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    cum_vol = df["volume"].cumsum()
    cum_pv = (typical * df["volume"]).cumsum()
    return cum_pv / cum_vol.replace(0, np.nan)


def add_indicators(df: pd.DataFrame, ema_fast: int, ema_slow: int) -> pd.DataFrame:
    """Return a copy of df with ema_fast, ema_slow and vwap columns added."""
    out = df.copy()
    out["ema_fast"] = ema(out["close"], ema_fast)
    out["ema_slow"] = ema(out["close"], ema_slow)
    out["vwap"] = vwap(out)
    return out
