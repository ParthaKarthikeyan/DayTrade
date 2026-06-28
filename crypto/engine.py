"""Crypto backtest engine: same fade / trend logic as the FX research, but with a
PERCENTAGE cost model (fees + spread in basis points of notional) instead of FX pips,
and spot position sizing (no leverage by default). cost_bps is the round-trip cost; we
charge half on each leg as a fraction of fill price. Everything is causal.
"""

from typing import List

import numpy as np
import pandas as pd

from sim.indicators import ema
from forex.engine import metrics            # generic: needs {trades, curve, end}
from forex.research import atr              # reuse the ATR helper


def _size(equity, risk_pct, dist, entry, max_leverage=1.0):
    if dist <= 0 or entry <= 0:
        return 0.0
    return min(equity * risk_pct / dist, equity * max_leverage / entry)


def run_fade(df: pd.DataFrame, *, start_cash: float = 2000.0, cost_bps: float = 20.0,
             risk_pct: float = 0.01, sma_period: int = 20, band_k: float = 2.0,
             stop_k: float = 3.0, trend_ema: int = 200, max_hold: int = 24) -> dict:
    """Bollinger-band mean-reversion fade, trend-aligned, with % cost."""
    if len(df) < max(sma_period, trend_ema) + 5:
        return {"trades": [], "curve": [start_cash], "end": start_cash}
    d = df.copy()
    d["sma"] = d["close"].rolling(sma_period).mean()
    d["std"] = d["close"].rolling(sma_period).std()
    d["ema"] = ema(d["close"], trend_ema)
    d["upper"] = d["sma"] + band_k * d["std"]
    d["lower"] = d["sma"] - band_k * d["std"]
    side_frac = cost_bps / 2.0 / 10000.0      # per-leg cost as fraction of price

    equity, pos = start_cash, None
    trades, curve = [], [equity]

    def close(px, reason, ts):
        nonlocal equity, pos
        gross = pos["side"] * pos["units"] * (px - pos["entry"])
        cost = side_frac * pos["units"] * (pos["entry"] + px)
        pnl = gross - cost
        trades.append({"side": pos["side"], "entry_time": pos["entry_time"],
                       "exit_time": ts, "entry": pos["entry"], "exit": px,
                       "units": pos["units"], "pnl": pnl, "reason": reason})
        equity += pnl
        curve.append(equity)
        pos = None

    for ts, r in d.iterrows():
        if np.isnan(r["std"]) or r["std"] <= 0 or np.isnan(r["ema"]):
            continue
        if pos is not None:
            side = pos["side"]
            pos["bars"] += 1
            stop_hit = (side == 1 and r["low"] <= pos["stop"]) or \
                       (side == -1 and r["high"] >= pos["stop"])
            tp_hit = (side == 1 and r["high"] >= r["sma"]) or \
                     (side == -1 and r["low"] <= r["sma"])
            if stop_hit:
                close(pos["stop"], "stop", ts)
            elif tp_hit:
                close(r["sma"], "mean", ts)
            elif pos["bars"] >= max_hold:
                close(r["close"], "time", ts)
        if pos is None:
            long_sig = r["close"] < r["lower"] and r["close"] > r["ema"]
            short_sig = r["close"] > r["upper"] and r["close"] < r["ema"]
            side = 1 if long_sig else (-1 if short_sig else 0)
            if side:
                entry = r["close"]
                dist = stop_k * r["std"]
                units = _size(equity, risk_pct, dist, entry)
                if units > 0:
                    pos = {"side": side, "entry": entry, "stop": entry - side * dist,
                           "units": units, "bars": 0, "entry_time": ts}
    if pos is not None:
        last = d.iloc[-1]
        close(last["close"], "eod", last.name)
    return {"trades": trades, "curve": curve, "end": equity}


def run_trend(df: pd.DataFrame, *, start_cash: float = 2000.0, cost_bps: float = 20.0,
              risk_pct: float = 0.01, entry_lookback: int = 20, exit_lookback: int = 10,
              atr_period: int = 14, atr_stop_mult: float = 3.0,
              trend_fast: int = 50, trend_slow: int = 200) -> dict:
    """Donchian breakout trend-follower with ATR chandelier stop and % cost."""
    if len(df) < trend_slow + entry_lookback + 5:
        return {"trades": [], "curve": [start_cash], "end": start_cash}
    d = df.copy()
    d["fast"] = ema(d["close"], trend_fast)
    d["slow"] = ema(d["close"], trend_slow)
    d["atr"] = atr(d, atr_period)
    d["don_hi"] = d["high"].rolling(entry_lookback).max().shift(1)
    d["don_lo"] = d["low"].rolling(entry_lookback).min().shift(1)
    d["exit_hi"] = d["high"].rolling(exit_lookback).max().shift(1)
    d["exit_lo"] = d["low"].rolling(exit_lookback).min().shift(1)
    side_frac = cost_bps / 2.0 / 10000.0

    equity, pos = start_cash, None
    trades, curve = [], [equity]

    def close(px, reason, ts):
        nonlocal equity, pos
        gross = pos["side"] * pos["units"] * (px - pos["entry"])
        cost = side_frac * pos["units"] * (pos["entry"] + px)
        pnl = gross - cost
        trades.append({"side": pos["side"], "entry_time": pos["entry_time"],
                       "exit_time": ts, "entry": pos["entry"], "exit": px,
                       "units": pos["units"], "pnl": pnl, "reason": reason})
        equity += pnl
        curve.append(equity)
        pos = None

    for ts, r in d.iterrows():
        if np.isnan(r["slow"]) or np.isnan(r["atr"]) or np.isnan(r["don_hi"]):
            continue
        long_regime, short_regime = r["fast"] > r["slow"], r["fast"] < r["slow"]
        if pos is not None:
            side = pos["side"]
            if side == 1:
                pos["peak"] = max(pos["peak"], r["high"])
                pos["stop"] = max(pos["stop"], pos["peak"] - atr_stop_mult * r["atr"])
            else:
                pos["peak"] = min(pos["peak"], r["low"])
                pos["stop"] = min(pos["stop"], pos["peak"] + atr_stop_mult * r["atr"])
            stop_hit = (side == 1 and r["low"] <= pos["stop"]) or \
                       (side == -1 and r["high"] >= pos["stop"])
            don_exit = (side == 1 and r["close"] < r["exit_lo"]) or \
                       (side == -1 and r["close"] > r["exit_hi"])
            if stop_hit:
                close(pos["stop"], "stop", ts)
            elif don_exit:
                close(r["close"], "exit_break", ts)
            elif (side == 1 and short_regime) or (side == -1 and long_regime):
                close(r["close"], "regime_flip", ts)
        if pos is None:
            long_sig = long_regime and r["close"] > r["don_hi"]
            short_sig = short_regime and r["close"] < r["don_lo"]
            side = 1 if long_sig else (-1 if short_sig else 0)
            if side:
                entry = r["close"]
                dist = atr_stop_mult * r["atr"]
                units = _size(equity, risk_pct, dist, entry)
                if units > 0:
                    pos = {"side": side, "entry": entry, "stop": entry - side * dist,
                           "peak": entry, "units": units, "entry_time": ts}
    if pos is not None:
        last = d.iloc[-1]
        close(last["close"], "eod", last.name)
    return {"trades": trades, "curve": curve, "end": equity}


STRATEGIES = {
    "fade": (run_fade, dict(sma_period=20, band_k=2.0, stop_k=3.0,
                            trend_ema=200, max_hold=24)),
    "trend": (run_trend, dict(entry_lookback=20, exit_lookback=10,
                              atr_stop_mult=3.0, trend_fast=50, trend_slow=200)),
}


def split_train_test(df, train_frac=0.65):
    cut = int(len(df) * train_frac)
    return df.iloc[:cut], df.iloc[cut:]


def walk_forward(df, fn, params, cost_bps, n_folds=5, start_cash=2000.0):
    n, seg, folds = len(df), len(df) // n_folds, []
    for i in range(n_folds):
        a, b = i * seg, ((i + 1) * seg if i < n_folds - 1 else n)
        chunk = df.iloc[a:b]
        if len(chunk) < 50:
            continue
        folds.append(metrics(fn(chunk, start_cash=start_cash, cost_bps=cost_bps, **params),
                             start_cash))
    return folds
