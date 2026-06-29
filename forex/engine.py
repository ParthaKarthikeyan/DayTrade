"""Forex backtest engine: three comparable variants on one bar series.

- pure_orb         : session opening-range breakout, either direction
- trend_orb        : same, but only in the higher-timeframe trend's direction
- pure_trend       : EMA fast/slow crossover trend-follower (no session)

Shared model: risk a fixed % of equity per trade sized off the stop distance,
model a round-trip spread, compound the account, and (for ORB) a 2R target with
a breakeven move at +1R, a per-session single trade, and a 5% daily breaker.
Prices are FX majors (price-only; FX has no reliable volume).
"""

from typing import Dict

import pandas as pd

from sim.indicators import ema
from .config import ForexConfig


def _prep(df: pd.DataFrame, cfg: ForexConfig) -> pd.DataFrame:
    out = df.copy()
    out["ema"] = ema(out["close"], cfg.trend_ema)
    out["fast"] = ema(out["close"], cfg.fast_ema)
    out["slow"] = ema(out["close"], cfg.slow_ema)
    out["m"] = out.index.hour * 60 + out.index.minute
    out["date"] = out.index.date
    return out


def _or_levels(df: pd.DataFrame, cfg: ForexConfig):
    s = cfg.session_start_utc * 60
    win = df[(df["m"] >= s) & (df["m"] < s + cfg.or_minutes)]
    return (win.groupby("date")["high"].max().to_dict(),
            win.groupby("date")["low"].min().to_dict())


def _trade(side, et, xt, entry, exit_px, units, reason):
    return {"side": side, "entry_time": et, "exit_time": xt, "entry": entry,
            "exit": exit_px, "units": units, "pnl": side * units * (exit_px - entry),
            "reason": reason}


def run_orb(df: pd.DataFrame, cfg: ForexConfig, trend_filtered: bool) -> dict:
    df = _prep(df, cfg)
    or_hi, or_lo = _or_levels(df, cfg)
    s = cfg.session_start_utc * 60
    or_ready, no_new, flatten = s + cfg.or_minutes, s + cfg.no_new_min, s + cfg.flatten_min
    hs = cfg.spread_pips * cfg.pip / 2.0
    cap = cfg.max_stop_pips * cfg.pip

    equity = cfg.start_cash
    day_eq = equity
    cur_date = None
    pos = None
    traded, halted = set(), set()
    trades, curve = [], [equity]

    def close(px, reason, ts):
        nonlocal equity, pos
        fill = px - pos["side"] * hs
        t = _trade(pos["side"], pos["entry_time"], ts, pos["entry"], fill, pos["units"], reason)
        equity += t["pnl"]; trades.append(t); curve.append(equity); pos = None

    for ts, r in df.iterrows():
        d, m = r["date"], r["m"]
        if d != cur_date:
            cur_date, day_eq = d, equity

        if pos is not None:
            side = pos["side"]
            if m >= flatten:
                close(r["close"], "time_stop", ts)
            elif (side == 1 and r["low"] <= pos["stop"]) or (side == -1 and r["high"] >= pos["stop"]):
                close(pos["stop"], "stop", ts)
            elif (side == 1 and r["high"] >= pos["target"]) or (side == -1 and r["low"] <= pos["target"]):
                close(pos["target"], "target", ts)
            else:  # breakeven at +1R
                onr = pos["entry"] + side * pos["risk"]
                if side == 1 and r["high"] >= onr:
                    pos["stop"] = max(pos["stop"], pos["entry"])
                elif side == -1 and r["low"] <= onr:
                    pos["stop"] = min(pos["stop"], pos["entry"])

        if equity <= day_eq * (1 - cfg.daily_max_loss_pct):
            halted.add(d)
            if pos is not None:
                close(r["close"], "breaker", ts)

        if (pos is None and d not in traded and d not in halted and d in or_hi
                and or_ready <= m <= no_new):
            c, hi, lo = r["close"], or_hi[d], or_lo[d]
            long_ok = c > hi and (not trend_filtered or c > r["ema"])
            short_ok = c < lo and (not trend_filtered or c < r["ema"])
            side = 1 if long_ok else (-1 if short_ok else 0)
            if side:
                entry = c + side * hs
                stop = lo if side == 1 else hi
                dist = abs(entry - stop)
                if dist > cap:
                    stop, dist = entry - side * cap, cap
                if dist > 0:
                    units = min(equity * cfg.risk_per_trade_pct / dist,
                                equity * cfg.max_leverage / entry)
                    if units > 0:
                        pos = {"side": side, "entry": entry, "stop": stop, "risk": dist,
                               "target": entry + side * cfg.reward_risk * dist,
                               "units": units, "entry_time": ts}
                        traded.add(d)

    if pos is not None:
        last = df.iloc[-1]
        close(last["close"], "eod", last.name)
    return {"trades": trades, "curve": curve, "end": equity}


def run_trend(df: pd.DataFrame, cfg: ForexConfig) -> dict:
    df = _prep(df, cfg)
    hs = cfg.spread_pips * cfg.pip / 2.0
    cap = cfg.max_stop_pips * cfg.pip
    equity = cfg.start_cash
    pos, prev = None, None
    trades, curve = [], [equity]

    def close(px, reason, ts):
        nonlocal equity, pos
        fill = px - pos["side"] * hs
        t = _trade(pos["side"], pos["entry_time"], ts, pos["entry"], fill, pos["units"], reason)
        equity += t["pnl"]; trades.append(t); curve.append(equity); pos = None

    for ts, r in df.iterrows():
        direction = 1 if r["fast"] > r["slow"] else (-1 if r["fast"] < r["slow"] else 0)
        if pos is not None:
            stop_hit = (pos["side"] == 1 and r["low"] <= pos["stop"]) or \
                       (pos["side"] == -1 and r["high"] >= pos["stop"])
            if stop_hit:
                close(pos["stop"], "stop", ts)
            elif direction and direction != pos["side"]:
                close(r["close"], "flip", ts)

        if pos is None and direction and prev is not None:
            crossed = (direction == 1 and prev["fast"] <= prev["slow"]) or \
                      (direction == -1 and prev["fast"] >= prev["slow"])
            if crossed:
                entry = r["close"] + direction * hs
                stop = entry - direction * cap
                units = min(equity * cfg.risk_per_trade_pct / cap,
                            equity * cfg.max_leverage / entry)
                if units > 0:
                    pos = {"side": direction, "entry": entry, "stop": stop,
                           "units": units, "entry_time": ts}
        prev = r

    if pos is not None:
        last = df.iloc[-1]
        close(last["close"], "eod", last.name)
    return {"trades": trades, "curve": curve, "end": equity}


def metrics(res: dict, start_cash: float) -> dict:
    trades, curve = res["trades"], res["curve"]
    wins = [t for t in trades if t["pnl"] > 0]
    gw = sum(t["pnl"] for t in wins)
    gl = -sum(t["pnl"] for t in trades if t["pnl"] < 0)
    peak, mdd = curve[0], 0.0
    for e in curve:
        peak = max(peak, e)
        mdd = max(mdd, (peak - e) / peak if peak else 0.0)
    return {
        "end": res["end"], "ret": (res["end"] / start_cash - 1) * 100,
        "trades": len(trades), "win_rate": (len(wins) / len(trades) * 100) if trades else 0,
        "profit_factor": (gw / gl) if gl > 0 else (float("inf") if gw > 0 else 0.0),
        "max_dd": mdd * 100,
    }


def run_variants(df: pd.DataFrame, cfg: ForexConfig) -> Dict[str, dict]:
    return {
        "pure_orb": metrics(run_orb(df, cfg, trend_filtered=False), cfg.start_cash),
        "trend_orb": metrics(run_orb(df, cfg, trend_filtered=True), cfg.start_cash),
        "pure_trend": metrics(run_trend(df, cfg), cfg.start_cash),
    }
