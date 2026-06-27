"""Synthetic 1-minute trading days for offline tests and demos.

No network needed. Builds a US/Eastern-indexed OHLCV DataFrame for a single
session so the engine can be exercised deterministically.
"""

import numpy as np
import pandas as pd

TZ = "America/New_York"


def _frame(date: str, closes, highs=None, lows=None, vol=10_000) -> pd.DataFrame:
    n = len(closes)
    idx = pd.date_range(f"{date} 09:30", periods=n, freq="1min", tz=TZ)
    closes = np.asarray(closes, dtype=float)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    highs = np.asarray(highs, dtype=float) if highs is not None else np.maximum(opens, closes)
    lows = np.asarray(lows, dtype=float) if lows is not None else np.minimum(opens, closes)
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes,
         "volume": np.full(n, vol, dtype=float)}, index=idx)


def uptrend_breakout(date: str = "2026-06-26", n: int = 120) -> pd.DataFrame:
    """Flat opening range, then a clean breakout that runs up all day."""
    base = 100.0
    closes = [base + (0.01 if i % 2 else -0.01) for i in range(15)]  # OR ~100
    price = base
    for i in range(n - 15):
        price += 0.15  # steady climb -> breakout + trail profits
        closes.append(price)
    highs = [c + 0.05 for c in closes]
    lows = [c - 0.05 for c in closes]
    return _frame(date, closes, highs, lows)


def crash_day(date: str = "2026-06-26", n: int = 120) -> pd.DataFrame:
    """Breaks DOWN hard after the opening range -> the bot shorts the breakdown."""
    base = 100.0
    closes = [base + (0.01 if i % 2 else -0.01) for i in range(15)]
    price = base
    for i in range(n - 15):
        price -= 0.6  # sharp sustained drop
        closes.append(price)
    highs = [c + 0.05 for c in closes]
    lows = [c - 0.05 for c in closes]
    return _frame(date, closes, highs, lows)


def bull_trap(date: str = "2026-06-26") -> pd.DataFrame:
    """Breaks UP (long entry) then collapses through the stop -> one capped loss.

    Use with allow_short=False so it stays a clean single-loss test of the
    per-trade stop.
    """
    closes = [100.0 + (0.01 if i % 2 else -0.01) for i in range(15)]
    closes.append(100.6)                       # breakout -> long entry
    for i in range(40):
        closes.append(100.6 - (i + 1) * 0.8)   # immediate collapse
    highs = [c + 0.05 for c in closes]
    lows = [c - 0.05 for c in closes]
    return _frame(date, closes, highs, lows)


def sawtooth_down(date: str = "2026-06-26", cycles: int = 15) -> pd.DataFrame:
    """Repeated up-spike (entry) then drop-through-stop, bleeding ~0.6%/cycle.

    Unprotected this would lose ~0.6% * cycles; the 5% daily breaker must halt
    trading around -5%. Use with ema_fast=1, ema_slow=2 so re-entries trigger.
    """
    closes = [100.0 + (0.01 if i % 2 else -0.01) for i in range(15)]
    for _ in range(cycles):
        closes += [100.6, 99.2]
    highs = [c + 0.05 for c in closes]
    lows = [c - 0.05 for c in closes]
    return _frame(date, closes, highs, lows)
