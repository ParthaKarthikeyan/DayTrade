"""Integer-contract daily trend engine for futures.

Same Donchian/EMA trend logic as the research engine, but P&L is computed in DOLLARS
via the contract point value, position size is a WHOLE number of contracts, and cost
is a flat $ per contract round-trip (micro futures commissions, ~$1/side).

Risk per trade is either a fixed $ budget (`risk_dollars`, legacy: a fraction of
STARTING equity) or — more honestly — `risk_pct` of the sleeve's CURRENT equity, so
sizing compounds and de-risks in drawdowns. `margin_per_contract` additionally caps
the position at what the sleeve's equity can actually margin. Everything causal.
"""

import numpy as np
import pandas as pd

from sim.indicators import ema
from forex.research import atr
from futures.contracts import contracts_for


def run_trend_contracts(df: pd.DataFrame, *, point_value: float,
                        risk_dollars: float = 0.0, risk_pct: float = 0.0,
                        start_cash: float, entry_lookback: int = 20, exit_lookback: int = 10,
                        atr_period: int = 14, atr_stop_mult: float = 3.0,
                        trend_fast: int = 50, trend_slow: int = 200,
                        cost_per_contract: float = 2.0,
                        margin_per_contract: float = 0.0) -> dict:
    """Donchian breakout + EMA regime filter + ATR chandelier trail, sized in whole
    contracts so that the initial stop risks <= the trade's risk budget. The budget is
    `risk_pct` of CURRENT equity when given, else the fixed `risk_dollars`. When
    `margin_per_contract` > 0, contracts are also capped by equity / margin (a real
    account can't hold what it can't margin). Returns realized trades (with $ pnl),
    the equity curve, the final equity, and the still-open position (if any, marked
    to the last close) so a forward paper bot can read today's target state."""
    if len(df) < trend_slow + entry_lookback + 5:
        return {"trades": [], "curve": [start_cash], "end": start_cash, "open": None,
                "skipped_unaffordable": 0}

    d = df.copy()
    d["fast"] = ema(d["close"], trend_fast)
    d["slow"] = ema(d["close"], trend_slow)
    d["atr"] = atr(d, atr_period)
    d["don_hi"] = d["high"].rolling(entry_lookback).max().shift(1)
    d["don_lo"] = d["low"].rolling(entry_lookback).min().shift(1)
    d["exit_hi"] = d["high"].rolling(exit_lookback).max().shift(1)
    d["exit_lo"] = d["low"].rolling(exit_lookback).min().shift(1)

    equity, pos = start_cash, None
    trades, curve = [], [equity]
    skipped = 0          # signals we couldn't afford even 1 contract for

    def close(px, reason, ts):
        nonlocal equity, pos
        gross = pos["side"] * pos["contracts"] * point_value * (px - pos["entry"])
        cost = pos["contracts"] * cost_per_contract
        pnl = gross - cost
        trades.append({"side": pos["side"], "entry_time": pos["entry_time"],
                       "exit_time": ts, "entry": pos["entry"], "exit": px,
                       "contracts": pos["contracts"], "pnl": pnl, "reason": reason})
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
                budget = equity * risk_pct if risk_pct > 0 else risk_dollars
                n = contracts_for(budget, dist, point_value)
                if margin_per_contract > 0:
                    n = min(n, int(max(equity, 0.0) // margin_per_contract))
                if n >= 1:
                    pos = {"side": side, "entry": entry, "stop": entry - side * dist,
                           "peak": entry, "contracts": n, "entry_time": ts}
                elif dist > 0:
                    skipped += 1

    open_pos = None
    if pos is not None:
        last = d.iloc[-1]
        mark = pos["side"] * pos["contracts"] * point_value * (last["close"] - pos["entry"])
        open_pos = {"side": pos["side"], "contracts": pos["contracts"],
                    "entry": pos["entry"], "stop": pos["stop"],
                    "entry_time": str(pos["entry_time"]), "unrealized": mark}
    return {"trades": trades, "curve": curve, "end": equity, "open": open_pos,
            "skipped_unaffordable": skipped}


def _prep(df: pd.DataFrame, entry_lookback, exit_lookback, atr_period,
          trend_fast, trend_slow) -> pd.DataFrame:
    d = df.copy()
    d["fast"] = ema(d["close"], trend_fast)
    d["slow"] = ema(d["close"], trend_slow)
    d["atr"] = atr(d, atr_period)
    d["don_hi"] = d["high"].rolling(entry_lookback).max().shift(1)
    d["don_lo"] = d["low"].rolling(entry_lookback).min().shift(1)
    d["exit_hi"] = d["high"].rolling(exit_lookback).max().shift(1)
    d["exit_lo"] = d["low"].rolling(exit_lookback).min().shift(1)
    return d


def run_trend_portfolio(frames: dict, *, specs: dict, risk_pct: float,
                        start_cash: float, entry_lookback: int = 20,
                        exit_lookback: int = 10, atr_period: int = 14,
                        atr_stop_mult: float = 3.0, trend_fast: int = 50,
                        trend_slow: int = 200, cost_per_contract: float = 2.0) -> dict:
    """Joint replay of every sleeve against ONE shared account.

    This fixes the old per-sleeve-on-full-account flaw honestly while keeping whole
    micros tradable at $10k: the risk budget is `risk_pct` of the account's CURRENT
    equity (compounds, de-risks in drawdowns), and a new position is only opened if
    the account can margin it ON TOP of everything already held — concurrent
    exposure is bounded by real margin, not allowed to stack unchecked.

    `frames`: ticker -> daily OHLC.  `specs`: ticker -> (micro, point_value, margin).
    Returns {trades, curve (realized, daily), dates, end, open: {ticker: pos},
    skipped_unaffordable}.
    """
    prepped = {t: _prep(df, entry_lookback, exit_lookback, atr_period,
                        trend_fast, trend_slow) for t, df in frames.items()}
    all_dates = sorted(set().union(*(set(d.index) for d in prepped.values())))

    equity = start_cash
    positions: dict = {}                # ticker -> open position dict
    trades, skipped = [], 0
    curve_dates, curve_vals = [], []

    def used_margin():
        return sum(specs[t][2] * p["contracts"] for t, p in positions.items())

    def close(tkr, px, reason, ts):
        nonlocal equity
        pos = positions.pop(tkr)
        pv = specs[tkr][1]
        gross = pos["side"] * pos["contracts"] * pv * (px - pos["entry"])
        pnl = gross - pos["contracts"] * cost_per_contract
        trades.append({"ticker": tkr, "side": pos["side"],
                       "entry_time": pos["entry_time"], "exit_time": ts,
                       "entry": pos["entry"], "exit": px,
                       "contracts": pos["contracts"], "pnl": pnl, "reason": reason})
        equity += pnl

    for ts in all_dates:
        for tkr, d in prepped.items():
            if ts not in d.index:
                continue
            r = d.loc[ts]
            if np.isnan(r["slow"]) or np.isnan(r["atr"]) or np.isnan(r["don_hi"]):
                continue
            long_regime, short_regime = r["fast"] > r["slow"], r["fast"] < r["slow"]
            pos = positions.get(tkr)
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
                    close(tkr, pos["stop"], "stop", ts)
                elif don_exit:
                    close(tkr, r["close"], "exit_break", ts)
                elif (side == 1 and short_regime) or (side == -1 and long_regime):
                    close(tkr, r["close"], "regime_flip", ts)
            if tkr not in positions:
                long_sig = long_regime and r["close"] > r["don_hi"]
                short_sig = short_regime and r["close"] < r["don_lo"]
                side = 1 if long_sig else (-1 if short_sig else 0)
                if side:
                    entry = r["close"]
                    dist = atr_stop_mult * r["atr"]
                    n = contracts_for(equity * risk_pct, dist, specs[tkr][1])
                    margin = specs[tkr][2]
                    if margin > 0:
                        free = max(equity - used_margin(), 0.0)
                        n = min(n, int(free // margin))
                    if n >= 1:
                        positions[tkr] = {"side": side, "entry": entry,
                                          "stop": entry - side * dist, "peak": entry,
                                          "contracts": n, "entry_time": ts}
                    elif dist > 0:
                        skipped += 1
        curve_dates.append(ts)
        curve_vals.append(equity)

    open_out = {}
    for tkr, pos in positions.items():
        last = prepped[tkr]["close"].dropna().iloc[-1]
        pv = specs[tkr][1]
        mark = pos["side"] * pos["contracts"] * pv * (float(last) - pos["entry"])
        open_out[tkr] = {"side": pos["side"], "contracts": pos["contracts"],
                         "entry": pos["entry"], "stop": pos["stop"],
                         "entry_time": str(pos["entry_time"]), "unrealized": mark}
    return {"trades": trades, "curve": curve_vals, "dates": curve_dates,
            "end": equity, "open": open_out, "skipped_unaffordable": skipped}
