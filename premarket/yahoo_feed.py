"""Consolidated 1-minute bars by polling Yahoo — the premarket bot's tape.

Why this exists (2026-07-13): Alpaca's free websocket streams only trades
executed ON the IEX exchange. The $1-$10 low-float gappers this bot trades
print almost entirely on other venues — the stream delivered 36 bars for a
whole 4-symbol session on 2026-07-10, and zero premarket bars ever. The bot
wasn't selective; it was blind.

Yahoo's chart API serves consolidated (all-venue) 1-minute OHLCV including
extended hours, roughly a minute behind live. Polling it and dispatching only
newly COMPLETED minutes gives the strategy the same tape the backtest ran on
(premarket/backtest.py uses the identical source).

Honest costs of this choice: signals lag the tape by ~1-2 minutes (poll
cadence + Yahoo's own delay), and Yahoo throttles datacenter IPs unpredictably
— a cycle that returns nothing is retried next cycle, and persistent failure
degrades to exactly today's blindness, no worse.
"""

from datetime import time as dtime
from typing import Dict, List, Optional

import pandas as pd

ET = "America/New_York"
FIELDS = ("Open", "High", "Low", "Close", "Volume")


def fetch_day_bars(symbols: List[str], tz: str = ET) -> Dict[str, pd.DataFrame]:
    """One poll: today's 1m bars (prepost) for every symbol, tz-converted.
    Returns {symbol: OHLCV frame}; symbols Yahoo can't serve are simply absent.
    Never raises — a failed poll is an empty dict and the next cycle retries."""
    import yfinance as yf
    if not symbols:
        return {}
    try:
        df = yf.download(symbols, period="1d", interval="1m", prepost=True,
                         auto_adjust=False, progress=False, threads=False,
                         group_by="ticker")
    except Exception as e:  # noqa: BLE001 — throttled/blocked: retry next cycle
        print(f"[feed] yahoo poll failed ({type(e).__name__}: {str(e)[:120]})")
        return {}
    if df is None or df.empty:
        return {}

    out: Dict[str, pd.DataFrame] = {}
    multi = isinstance(df.columns, pd.MultiIndex)
    for s in symbols:
        try:
            sub = df[s] if multi else df
        except KeyError:
            continue
        sub = sub.dropna(subset=["Close"])
        if sub.empty:
            continue
        idx = sub.index
        sub = (sub.tz_convert(tz) if idx.tz is not None
               else sub.tz_localize("UTC").tz_convert(tz))
        out[s] = sub
        if not multi:       # single-symbol download: flat columns, one frame
            break
    return out


def new_completed_bars(df: pd.DataFrame, after: Optional[pd.Timestamp],
                       now: pd.Timestamp, start_t: dtime,
                       end_t: dtime) -> List[dict]:
    """Bars newer than `after`, inside [start_t, end_t], and fully CLOSED
    (Yahoo stamps a bar at its minute's START and mutates it until the minute
    ends — dispatching it early would hand the strategy a moving candle)."""
    if df is None or df.empty:
        return []
    if df.index.tz is None:
        df = df.tz_localize(ET)
    bars = []
    for t, r in df.iterrows():
        if after is not None and t <= after:
            continue
        if not (start_t <= t.time() <= end_t):
            continue
        if t + pd.Timedelta(seconds=60) > now:
            continue                      # still forming
        try:
            b = {"t": t, "o": float(r["Open"]), "h": float(r["High"]),
                 "l": float(r["Low"]), "c": float(r["Close"]),
                 "v": float(r["Volume"])}
        except (TypeError, ValueError):
            continue
        if b["c"] != b["c"]:              # NaN close
            continue
        bars.append(b)
    return bars
