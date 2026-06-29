"""Forward paper-trading book for the diversified $10k futures trend system.

This is the pre-real-money validation gate. No broker, no real cash: it simulates
a $10k paper account that trades WHOLE micro contracts on the exact deployment rules
(`run_trend_contracts`), and it records what those rules do day by day on fresh bars.

Why a deterministic *replay* rather than an incremental tick: the strategy's position
sizing is a fixed fraction of STARTING equity, so the entire equity curve is a pure
function of the price history. Replaying the whole history each day therefore yields a
stable, reproducible curve — and as real calendar time passes, its tail extends with
bars that did not exist when the rules were frozen. Those post-deployment bars are the
genuine out-of-sample forward record; everything before the deployment stamp is just
the backtest the rules were chosen on, shown for continuity.

Two books run in parallel (5% and 8% risk/trade) because paper is free and the live
experience of each drawdown is the most useful thing to learn before funding anything.
"""

from __future__ import annotations

import pandas as pd

from futures.contracts import SPECS
from futures.engine import run_trend_contracts
from forex.engine import metrics

# Sleeves and per-instrument tradable history, kept identical to run_futures_deploy.py
# so the paper book and the deployment backtest are the same system.
SLEEVES = {
    "ES=F": "S&P 500", "GC=F": "Gold", "CL=F": "Crude oil",
    "6E=F": "Euro FX", "BTC-USD": "Bitcoin",
}
HISTORY = {"ES=F": 7, "GC=F": 7, "CL=F": 7, "6E=F": 7, "BTC-USD": 5}
TREND = dict(entry_lookback=20, exit_lookback=10, atr_period=14, atr_stop_mult=3.0,
             trend_fast=50, trend_slow=200, cost_per_contract=2.0)
RISK_BOOKS = {"5pct": 0.05, "8pct": 0.08}


def _daily_pnl(trades):
    if not trades:
        return pd.Series(dtype=float)
    s = pd.Series([t["pnl"] for t in trades],
                  index=[pd.Timestamp(t["exit_time"]).normalize() for t in trades])
    return s.groupby(level=0).sum()


def book_snapshot(frames: dict, cash: float, risk_pct: float) -> dict:
    """Replay every sleeve at `risk_pct` of starting `cash`, combine realized $ PnL into
    one equity curve, and read off today's target micro positions. Returns a JSON-ready
    snapshot: headline equity/return/drawdown, the open position per sleeve (the orders a
    real account would be holding right now), and a daily equity curve for the dashboard."""
    risk_dollars = cash * risk_pct
    pnls, per, positions = [], {}, []
    for tkr, df in frames.items():
        sym, pv, _margin = SPECS[tkr]
        res = run_trend_contracts(df, point_value=pv, risk_dollars=risk_dollars,
                                  start_cash=cash, **TREND)
        per[tkr] = res
        pnls.append(_daily_pnl(res["trades"]))
        op = res["open"]
        if op is not None:
            positions.append({
                "sleeve": SLEEVES[tkr], "ticker": tkr, "micro": sym,
                "side": "long" if op["side"] == 1 else "short",
                "contracts": op["contracts"], "entry": round(op["entry"], 4),
                "stop": round(op["stop"], 4), "unrealized": round(op["unrealized"], 2),
            })

    combined = (pd.concat(pnls).groupby(level=0).sum().sort_index()
                if any(len(p) for p in pnls) else pd.Series(dtype=float))
    open_unreal = sum(p["unrealized"] for p in positions)
    if combined.empty:
        span = [min(df.index[0] for df in frames.values()),
                max(df.index[-1] for df in frames.values())]
        curve = pd.Series([cash, cash], index=span)
    else:
        idx = pd.date_range(combined.index.min(), combined.index.max(), freq="D")
        curve = cash + combined.reindex(idx, fill_value=0.0).cumsum()

    all_tr = [t for res in per.values() for t in res["trades"]]
    realized_end = float(curve.iloc[-1])
    m = metrics({"trades": all_tr, "curve": list(curve), "end": realized_end}, cash)
    equity = realized_end + open_unreal          # mark open positions to last close
    pf = m["profit_factor"]
    return {
        "risk_pct": risk_pct,
        "start_cash": cash,
        "equity": round(equity, 2),
        "realized": round(realized_end, 2),
        "open_unrealized": round(open_unreal, 2),
        "ret_pct": round((equity / cash - 1) * 100, 2),
        "max_dd": round(m["max_dd"], 2),
        "profit_factor": None if pf == float("inf") else round(pf, 2),
        "trades": m["trades"],
        "win_rate": round(m["win_rate"], 1),
        "positions": positions,
        "equity_curve": [[d.strftime("%Y-%m-%d"), round(float(e), 2)]
                         for d, e in curve.items()],
    }
