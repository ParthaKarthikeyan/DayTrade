"""Out-of-sample forex strategy research.

The intraday Session-ORB families lost money across the board over 2 years (every
profit factor < 1.0). The diagnosis: too many trades, each paying the spread. This
module tests the opposite hypothesis — *lower-frequency trend following* — and does
so honestly:

- FIXED, textbook parameters (Donchian 20/10, ATR x3, EMA 50/200). No parameter
  sweep, so there is nothing to curve-fit to.
- Several majors, not one lucky pair.
- A chronological TRAIN / TEST split: rules are "developed" on the older slice and
  judged on a held-out newer slice they never saw. A strategy only counts as a
  candidate if it is profitable OUT-OF-SAMPLE across multiple pairs.

Everything here is causal (indicators use only past bars), so iterating bar-by-bar
after precomputing introduces no look-ahead.
"""

from typing import Dict, List

import numpy as np
import pandas as pd

from sim.indicators import ema
from .engine import metrics


# --- helpers ---------------------------------------------------------------
def pip_size(pair: str) -> float:
    """JPY pairs quote to 2 dp (1 pip = 0.01); other majors to 4 dp (0.0001)."""
    return 0.01 if "JPY" in pair.upper() else 0.0001


# Slightly conservative round-trip retail spreads (pips) per major.
SPREAD_PIPS = {
    "EUR_USD": 1.0, "GBP_USD": 1.5, "USD_JPY": 1.0,
    "AUD_USD": 1.2, "USD_CAD": 1.7, "NZD_USD": 1.8, "EUR_JPY": 1.6,
    "USD_CHF": 1.8, "EUR_GBP": 1.5,
}


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average true range (Wilder-ish, EMA-smoothed). Causal: uses prior close."""
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


# --- a single, parameterized trend-following engine ------------------------
def run_trend_breakout(df: pd.DataFrame, pair: str, *, start_cash: float = 2000.0,
                       risk_pct: float = 0.01, max_leverage: float = 30.0,
                       entry_lookback: int = 20, exit_lookback: int = 10,
                       atr_period: int = 14, atr_stop_mult: float = 3.0,
                       trend_fast: int = 50, trend_slow: int = 200,
                       use_donchian: bool = True) -> dict:
    """Lower-frequency trend follower.

    Regime filter: only long when EMA(fast) > EMA(slow), only short when below.
    Entry: Donchian breakout (close beyond the prior `entry_lookback`-bar extreme);
           if use_donchian is False, entry is the EMA cross itself.
    Stop:  ATR-multiple from entry, ratcheted with a chandelier trail (lets winners
           run). Also exits on an opposite `exit_lookback`-bar Donchian break.
    """
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
    d["fast_prev"] = d["fast"].shift(1)
    d["slow_prev"] = d["slow"].shift(1)

    pip = pip_size(pair)
    hs = SPREAD_PIPS.get(pair, 1.5) * pip / 2.0   # half-spread per side

    equity = start_cash
    pos = None
    trades: List[dict] = []
    curve = [equity]

    def close(px, reason, ts):
        nonlocal equity, pos
        fill = px - pos["side"] * hs
        pnl = pos["side"] * pos["units"] * (fill - pos["entry"])
        trades.append({"side": pos["side"], "entry_time": pos["entry_time"],
                       "exit_time": ts, "entry": pos["entry"], "exit": fill,
                       "units": pos["units"], "pnl": pnl, "reason": reason})
        equity += pnl
        curve.append(equity)
        pos = None

    for ts, r in d.iterrows():
        if np.isnan(r["slow"]) or np.isnan(r["atr"]) or np.isnan(r["don_hi"]):
            continue
        long_regime = r["fast"] > r["slow"]
        short_regime = r["fast"] < r["slow"]

        if pos is not None:
            side = pos["side"]
            # chandelier trail
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
            if use_donchian:
                long_sig = long_regime and r["close"] > r["don_hi"]
                short_sig = short_regime and r["close"] < r["don_lo"]
            else:  # EMA cross entry
                crossed_up = r["fast_prev"] <= r["slow_prev"] and r["fast"] > r["slow"]
                crossed_dn = r["fast_prev"] >= r["slow_prev"] and r["fast"] < r["slow"]
                long_sig, short_sig = crossed_up, crossed_dn
            side = 1 if long_sig else (-1 if short_sig else 0)
            if side:
                entry = r["close"] + side * hs
                dist = atr_stop_mult * r["atr"]
                if dist > 0:
                    units = min(equity * risk_pct / dist, equity * max_leverage / entry)
                    if units > 0:
                        pos = {"side": side, "entry": entry,
                               "stop": entry - side * dist, "peak": entry,
                               "units": units, "entry_time": ts}

    if pos is not None:
        last = d.iloc[-1]
        close(last["close"], "eod", last.name)
    return {"trades": trades, "curve": curve, "end": equity}


# --- a mean-reversion engine (Bollinger fade) ------------------------------
def run_mean_reversion(df: pd.DataFrame, pair: str, *, start_cash: float = 2000.0,
                       risk_pct: float = 0.01, max_leverage: float = 30.0,
                       sma_period: int = 20, band_k: float = 2.0,
                       stop_k: float = 3.5, trend_ema: int = 200,
                       max_hold: int = 20) -> dict:
    """Fade Bollinger-band extremes back to the mean.

    FX majors range more than they trend intraday, so the hypothesis is: when price
    stretches `band_k` std-devs from its SMA, it reverts. Long the lower band / short
    the upper band; take profit at the SMA (mean); stop at `stop_k` std beyond entry;
    bail after `max_hold` bars. A long-EMA filter blocks fading *with* a strong trend
    (don't catch a falling knife): only fade longs above the trend EMA region.
    """
    if len(df) < max(sma_period, trend_ema) + 5:
        return {"trades": [], "curve": [start_cash], "end": start_cash}

    d = df.copy()
    d["sma"] = d["close"].rolling(sma_period).mean()
    d["std"] = d["close"].rolling(sma_period).std()
    d["ema"] = ema(d["close"], trend_ema)
    d["upper"] = d["sma"] + band_k * d["std"]
    d["lower"] = d["sma"] - band_k * d["std"]

    pip = pip_size(pair)
    hs = SPREAD_PIPS.get(pair, 1.5) * pip / 2.0
    equity = start_cash
    pos = None
    trades: List[dict] = []
    curve = [equity]

    def close(px, reason, ts):
        nonlocal equity, pos
        fill = px - pos["side"] * hs
        pnl = pos["side"] * pos["units"] * (fill - pos["entry"])
        trades.append({"side": pos["side"], "entry_time": pos["entry_time"],
                       "exit_time": ts, "entry": pos["entry"], "exit": fill,
                       "units": pos["units"], "pnl": pnl, "reason": reason})
        equity += pnl
        curve.append(equity)
        pos = None

    for ts, r in d.iterrows():
        if np.isnan(r["std"]) or r["std"] <= 0 or np.isnan(r["ema"]):
            continue
        if pos is not None:
            side, held = pos["side"], pos["bars"]
            pos["bars"] += 1
            stop_hit = (side == 1 and r["low"] <= pos["stop"]) or \
                       (side == -1 and r["high"] >= pos["stop"])
            tp_hit = (side == 1 and r["high"] >= r["sma"]) or \
                     (side == -1 and r["low"] <= r["sma"])
            if stop_hit:
                close(pos["stop"], "stop", ts)
            elif tp_hit:
                close(r["sma"], "mean", ts)
            elif held >= max_hold:
                close(r["close"], "time", ts)

        if pos is None:
            # fade longs only above the trend EMA, shorts only below (avoid knives)
            long_sig = r["close"] < r["lower"] and r["close"] > r["ema"]
            short_sig = r["close"] > r["upper"] and r["close"] < r["ema"]
            side = 1 if long_sig else (-1 if short_sig else 0)
            if side:
                entry = r["close"] + side * hs
                dist = stop_k * r["std"]
                if dist > 0:
                    units = min(equity * risk_pct / dist, equity * max_leverage / entry)
                    if units > 0:
                        pos = {"side": side, "entry": entry,
                               "stop": entry - side * dist, "units": units,
                               "bars": 0, "entry_time": ts}

    if pos is not None:
        last = d.iloc[-1]
        close(last["close"], "eod", last.name)
    return {"trades": trades, "curve": curve, "end": equity}


# --- strategy catalogue (FIXED params — no sweep). (fn, params) per entry ---
STRATEGIES = {
    "donchian_trend": (run_trend_breakout,
                       dict(use_donchian=True, entry_lookback=20, exit_lookback=10,
                            atr_stop_mult=3.0, trend_fast=50, trend_slow=200)),
    "ema_cross_trend": (run_trend_breakout,
                        dict(use_donchian=False, atr_stop_mult=3.0,
                             trend_fast=50, trend_slow=200)),
    "bollinger_fade": (run_mean_reversion,
                       dict(sma_period=20, band_k=2.0, stop_k=3.5,
                            trend_ema=200, max_hold=20)),
}


# Robustness neighbourhood for the Bollinger-fade candidate. This is NOT a sweep to
# pick the best — we run the whole neighbourhood and ask "is the edge stable across
# nearby params, or only at one fragile point?" A real edge survives its neighbours.
MR_ROBUST_GRID = {
    f"sma{s}_k{k}": (run_mean_reversion,
                     dict(sma_period=s, band_k=k, stop_k=3.5, trend_ema=200, max_hold=20))
    for s in (15, 20, 25) for k in (1.5, 2.0, 2.5)
}


def split_train_test(df: pd.DataFrame, train_frac: float = 0.65):
    n = len(df)
    cut = int(n * train_frac)
    return df.iloc[:cut], df.iloc[cut:]


def walk_forward(df: pd.DataFrame, pair: str, fn, params, n_folds: int = 5,
                 start_cash: float = 2000.0) -> List[dict]:
    """Chop the series into `n_folds` sequential, non-overlapping out-of-sample chunks
    and evaluate the (fixed-param) strategy on each independently. The right temporal
    robustness check for a single pair: a real edge holds across most time windows, a
    fluke shows up in one. Params are fixed, so there is no fitting/leakage — each chunk
    is a clean OOS period (its first ~200 bars are indicator warm-up that won't trade).
    """
    n = len(df)
    seg = n // n_folds
    folds = []
    for i in range(n_folds):
        a = i * seg
        b = (i + 1) * seg if i < n_folds - 1 else n
        chunk = df.iloc[a:b]
        if len(chunk) < 50:
            continue
        folds.append(metrics(fn(chunk, pair, start_cash=start_cash, **params), start_cash))
    return folds


def evaluate(df: pd.DataFrame, pair: str, start_cash: float = 2000.0,
             train_frac: float = 0.65, catalogue=None) -> Dict[str, dict]:
    """Run every strategy in `catalogue` (default STRATEGIES) on train + held-out test."""
    catalogue = catalogue or STRATEGIES
    train, test = split_train_test(df, train_frac)
    out = {}
    for name, (fn, params) in catalogue.items():
        out[name] = {
            "train": metrics(fn(train, pair, start_cash=start_cash, **params), start_cash),
            "test": metrics(fn(test, pair, start_cash=start_cash, **params), start_cash),
        }
    return out
