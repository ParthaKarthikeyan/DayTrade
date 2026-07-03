"""Forward paper-trading book for the diversified $10k futures trend system.

This is the pre-real-money validation gate. No broker, no real cash: it simulates
a $10k paper account that trades WHOLE micro contracts on the exact deployment rules
(`run_trend_contracts`), and it records what those rules do day by day on fresh bars.

Why a deterministic *replay* rather than an incremental tick: the equity curve is a
pure function of the price history given frozen rules, so replaying the whole history
each day yields a stable, reproducible curve — and as real calendar time passes, its
tail extends with bars that did not exist when the rules were frozen. Those
post-deployment bars are the genuine out-of-sample forward record; everything before
the deployment stamp is just the backtest the rules were chosen on, kept for context.

Portfolio honesty (rules v2):
- all sleeves replay jointly against ONE account (not each on its own full copy);
- risk per trade is % of the account's CURRENT equity (compounds, de-risks in DDs);
- a new position only opens if the account can margin it on top of everything already
  held, so concurrent exposure is bounded by real margin instead of stacking unchecked.
(A strict per-sleeve capital split was tried first, but $10k/5 = $2k per sleeve can't
afford even one micro contract at sane risk — the joint margin-capped account is the
version that is both honest and actually tradable at this size.)

Two books run in parallel (5% and 8% risk/trade) because paper is free and the live
experience of each drawdown is the most useful thing to learn before funding anything.
"""

from __future__ import annotations

import pandas as pd

from futures.contracts import SPECS
from futures.engine import run_trend_portfolio
from forex.engine import metrics

# Bump when the frozen rules change; the runner re-stamps deployment_date so the
# forward record never silently mixes two rule sets.
RULES_VERSION = 2

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


def book_snapshot(frames: dict, cash: float, risk_pct: float) -> dict:
    """Joint replay of every sleeve against ONE `cash` account at `risk_pct` of
    current equity per trade, margin-capped across concurrent positions. Returns a
    JSON-ready snapshot: headline equity/return/drawdown, the open position per
    sleeve (the orders a real account would be holding right now), and a daily
    equity curve for the dashboard."""
    res = run_trend_portfolio(frames, specs=SPECS, risk_pct=risk_pct,
                              start_cash=cash, **TREND)

    trade_log = []
    for t in res["trades"]:
        tkr = t["ticker"]
        trade_log.append({
            "sleeve": SLEEVES[tkr], "ticker": tkr, "micro": SPECS[tkr][0],
            "side": "long" if t["side"] == 1 else "short",
            "contracts": t["contracts"],
            "entry_time": str(pd.Timestamp(t["entry_time"]).date()),
            "entry": round(t["entry"], 4),
            "exit_time": str(pd.Timestamp(t["exit_time"]).date()),
            "exit": round(t["exit"], 4),
            "pnl": round(t["pnl"], 2), "reason": t["reason"],
        })
    positions = []
    for tkr, op in res["open"].items():
        positions.append({
            "sleeve": SLEEVES[tkr], "ticker": tkr, "micro": SPECS[tkr][0],
            "side": "long" if op["side"] == 1 else "short",
            "contracts": op["contracts"], "entry": round(op["entry"], 4),
            "stop": round(op["stop"], 4), "unrealized": round(op["unrealized"], 2),
        })

    open_unreal = sum(p["unrealized"] for p in positions)
    realized_end = float(res["end"])
    m = metrics({"trades": res["trades"], "curve": res["curve"] or [cash],
                 "end": realized_end}, cash)
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
        "trade_log": sorted(trade_log, key=lambda t: t["exit_time"]),
        "equity_curve": [[pd.Timestamp(d).strftime("%Y-%m-%d"), round(float(e), 2)]
                         for d, e in zip(res["dates"], res["curve"])],
    }
