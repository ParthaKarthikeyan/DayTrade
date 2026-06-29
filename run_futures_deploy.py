#!/usr/bin/env python3
"""Realistic $10k deployment model for the diversified futures trend system.

    python run_futures_deploy.py --cash 10000

Unlike run_futures_research.py (fractional sizing, proof-of-concept), this models what
a real account actually does: WHOLE micro contracts, real point values, a flat $/contract
commission, and a per-trade risk budget as a fixed % of the account. It then sweeps the
risk knob to find which setting lands near your max-drawdown budget, and reports per-sleeve
contract affordability — because at $10k the binding constraint is often "can I afford even
one contract at this risk?". Writes a markdown report to the GitHub Actions job summary.
"""

import argparse
import os

import pandas as pd

from futures.data import get_daily
from futures.contracts import SPECS
from futures.engine import run_trend_contracts
from forex.engine import metrics

SLEEVES = {
    "ES=F": ("S&P 500", "equity"), "GC=F": ("Gold", "metal"),
    "CL=F": ("Crude oil", "energy"),
    "6E=F": ("Euro FX", "fx"), "BTC-USD": ("Bitcoin", "crypto"),
}
# The rates sleeve (10Y note) is intentionally EXCLUDED: there is no affordable micro,
# and one full ZN contract risks more than a $10k account's per-trade budget allows
# (it traded 0 times in testing). So a $10k micro account can't get bond diversification.
TREND = dict(entry_lookback=20, exit_lookback=10, atr_period=14, atr_stop_mult=3.0,
             trend_fast=50, trend_slow=200, cost_per_contract=2.0)
YEARS = 12
# Realistically tradable history: CME micros (MES/MGC/MCL/M6E) launched May 2019;
# Micro Bitcoin (MBT) launched May 2021. Backtesting earlier overstates what a $10k
# micro account could have held, so each sleeve is clamped to its instrument's life.
HISTORY = {"ES=F": 7, "GC=F": 7, "CL=F": 7, "6E=F": 7, "BTC-USD": 5}
RISK_LEVELS = [0.01, 0.02, 0.03, 0.05, 0.08]   # per-trade risk as % of the account


def fmt_pf(pf):
    return "inf" if pf == float("inf") else f"{pf:.2f}"


def daily_pnl(trades):
    if not trades:
        return pd.Series(dtype=float)
    s = pd.Series([t["pnl"] for t in trades],
                  index=[pd.Timestamp(t["exit_time"]).normalize() for t in trades])
    return s.groupby(level=0).sum()


def portfolio_curve(frames, cash, risk_pct):
    """Run every sleeve at the given per-trade risk %, combine realized $ PnL into one
    equity curve on a shared `cash` account. Returns (curve_series, per_sleeve_results)."""
    risk_dollars = cash * risk_pct
    pnls, per = [], {}
    for tkr, df in frames.items():
        pv = SPECS[tkr][1]
        res = run_trend_contracts(df, point_value=pv, risk_dollars=risk_dollars,
                                  start_cash=cash, **TREND)
        per[tkr] = res
        pnls.append(daily_pnl(res["trades"]))
    combined = pd.concat(pnls).groupby(level=0).sum().sort_index() if pnls else pd.Series(dtype=float)
    if combined.empty:                       # no sleeve could afford a trade at this risk
        span = [min(df.index[0] for df in frames.values()),
                max(df.index[-1] for df in frames.values())]
        return pd.Series([cash, cash], index=span), per
    idx = pd.date_range(combined.index.min(), combined.index.max(), freq="D")
    curve = cash + combined.reindex(idx, fill_value=0.0).cumsum()
    return curve, per


def main():
    p = argparse.ArgumentParser(description="Realistic $10k futures deployment model")
    p.add_argument("--cash", type=float, default=10000.0)
    p.add_argument("--max-dd-budget", type=float, default=25.0)
    p.add_argument("--folds", type=int, default=5)
    args = p.parse_args()

    frames, missing = {}, []
    for tkr in SLEEVES:
        try:
            frames[tkr] = get_daily(tkr, HISTORY.get(tkr, YEARS))
        except Exception as e:
            missing.append(f"{tkr} ({e})")
    if not frames:
        print("No data fetched:", missing)
        return

    span = max((max(df.index[-1] for df in frames.values())
                - min(df.index[0] for df in frames.values())).days / 365.0, 0.01)
    md = ["# Futures deployment model — realistic $%d, whole micro contracts" % args.cash, "",
          f"Daily Donchian/EMA trend · {len(frames)} sleeves · micro-era history "
          f"(MES/MGC/MCL/M6E from 2019, MBT from 2021; ~{span:.0f}y) · integer micros, real "
          "point values, $2/contract round-trip commission. Rates sleeve excluded (no "
          "affordable micro at $10k). Risk = fixed % of account per trade.", ""]
    if missing:
        md.append(f"_Sleeves with no data, skipped: {', '.join(missing)}._\n")

    # --- Contract affordability: the granularity wall at this account size ----
    md += ["## Contract affordability at $%d" % args.cash, "",
           "Median stop-risk for ONE contract, as a share of the account. If this exceeds "
           "your per-trade risk %, you can only take a position by risking more than planned.",
           "", "| Sleeve | Micro | Pt value | Margin | 1-contract risk (median) | % of acct |",
           "|---|---|--:|--:|--:|--:|"]
    total_margin = 0.0
    from forex.research import atr as _atr
    for tkr, (label, _cls) in SLEEVES.items():
        if tkr not in frames:
            continue
        sym, pv, margin = SPECS[tkr]
        total_margin += margin
        a = _atr(frames[tkr], TREND["atr_period"])
        med_risk = float((TREND["atr_stop_mult"] * a * pv).dropna().median())
        md.append(f"| {label} | {sym} | ${pv:,.2f} | ${margin:,.0f} | ${med_risk:,.0f} | "
                  f"{med_risk/args.cash*100:.1f}% |")
    fit_note = "fits" if total_margin < args.cash else "TOO MUCH to hold all at once"
    md.append(f"\n_Holding one contract in every sleeve at once needs ~${total_margin:,.0f} "
              f"of margin (of ${args.cash:,.0f}) — {fit_note}._")

    # --- Risk sweep: map return & drawdown vs the risk knob -------------------
    md += ["", "## Risk sweep — return & drawdown vs per-trade risk", "",
           "| Risk/trade | Total ret | CAGR | Max DD | Trades | Unaffordable signals |",
           "|--:|--:|--:|--:|--:|--:|"]
    best = None
    detail = {}
    for rp in RISK_LEVELS:
        curve, per = portfolio_curve(frames, args.cash, rp)
        all_tr = [t for res in per.values() for t in res["trades"]]
        skipped = sum(res["skipped_unaffordable"] for res in per.values())
        m = metrics({"trades": all_tr, "curve": list(curve), "end": float(curve.iloc[-1])}, args.cash)
        years = (curve.index[-1] - curve.index[0]).days / 365.0
        cagr = (curve.iloc[-1] / args.cash) ** (1 / years) - 1 if years > 0 else 0.0
        detail[rp] = (curve, per, m, cagr)
        flag = " ←≈budget" if abs(m["max_dd"] - args.max_dd_budget) < 6 else ""
        md.append(f"| {rp*100:.0f}% | {m['ret']:+.0f}% | {cagr*100:+.1f}%/yr | "
                  f"{m['max_dd']:.1f}%{flag} | {m['trades']} | {skipped} |")
        if best is None or abs(m["max_dd"] - args.max_dd_budget) < abs(detail[best][2]["max_dd"] - args.max_dd_budget):
            best = rp

    # --- Per-sleeve breakdown at the budget-matching risk level ---------------
    curve, per, m, cagr = detail[best]
    years = (curve.index[-1] - curve.index[0]).days / 365.0
    md += ["", f"## Per-sleeve at {best*100:.0f}% risk/trade (closest to {args.max_dd_budget:.0f}% DD budget)",
           "", "| Sleeve | Class | $ P&L | Return | Trades | Avg contracts |",
           "|---|---|--:|--:|--:|--:|"]
    for tkr, (label, cls) in SLEEVES.items():
        if tkr not in per:
            continue
        tr = per[tkr]["trades"]
        pnl = sum(t["pnl"] for t in tr)
        avg_c = (sum(t["contracts"] for t in tr) / len(tr)) if tr else 0
        md.append(f"| {label} | {cls} | ${pnl:+,.0f} | {pnl/args.cash*100:+.1f}% | "
                  f"{len(tr)} | {avg_c:.1f} |")
    md += ["", f"**Portfolio at {best*100:.0f}% risk:** {m['ret']:+.0f}% total · "
           f"**CAGR {cagr*100:+.1f}%/yr** · max DD {m['max_dd']:.1f}% · PF {fmt_pf(m['profit_factor'])} "
           f"· {m['trades']} trades (~{m['trades']/years:.0f}/yr)."]

    # --- Walk-forward on the chosen portfolio curve ---------------------------
    md += ["", "## Walk-forward — portfolio return per time fold (chosen risk)", ""]
    seg = len(curve) // args.folds
    cells, wins = [], 0
    for i in range(args.folds):
        a, b = i * seg, ((i + 1) * seg if i < args.folds - 1 else len(curve))
        e0, e1 = curve.iloc[a], curve.iloc[b - 1]
        fr = (e1 / e0 - 1) * 100 if e0 else 0.0
        wins += 1 if fr > 0 else 0
        cells.append(f"{fr:+.1f}%")
    md.append("- " + " · ".join(f"Fold {i+1}: {c}" for i, c in enumerate(cells)))
    md.append(f"- **Profitable in {wins}/{args.folds} folds.**")

    md += ["", f"> Integer-contract sizing at ${args.cash:,.0f} makes the risk knob LUMPY — "
           "many sleeves sit at 1 contract, so realised risk is whatever one micro costs, not "
           "the % you set. Margins/point values are approximate (verify with your broker). "
           "This is a backtest; forward paper-test before funding real money."]

    report = "\n".join(md)
    sp = os.environ.get("GITHUB_STEP_SUMMARY")
    if sp:
        with open(sp, "a", encoding="utf-8") as f:
            f.write(report + "\n")
    print("\n" + report)


if __name__ == "__main__":
    main()
