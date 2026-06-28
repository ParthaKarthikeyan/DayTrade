#!/usr/bin/env python3
"""Diversified trend-following across asset classes — the managed-futures approach.

    python run_futures_research.py

Runs the same Donchian/EMA trend engine on DAILY bars across uncorrelated markets
(equities, gold, oil, bonds, FX, crypto — each deployable via a micro future), then
combines them equal-risk into a PORTFOLIO. The whole point of futures here is (a) low
cost vs notional and (b) diversification: many small, individually-unreliable trend
edges that don't move together can sum to a smoother aggregate. We test that honestly
with per-sleeve results, a combined portfolio curve, a walk-forward across time, and a
risk-sizing table so you can see what targets a given max-drawdown budget.

Data caveat: continuous-contract / proxy daily series (roll artefacts not modelled);
futures cost is ~tiny, modelled at 5bps round-trip. Research-grade, forward-test before
real money. Writes a markdown report to the GitHub Actions job summary.
"""

import argparse
import os

import pandas as pd

from futures.data import get_daily
from crypto.engine import run_trend, metrics

# ticker -> (label, asset class) — one micro future per sleeve
SLEEVES = {
    "ES=F": ("S&P 500", "equity"),
    "GC=F": ("Gold", "metal"),
    "CL=F": ("Crude oil", "energy"),
    "ZN=F": ("10Y T-Note", "rates"),
    "6E=F": ("Euro FX", "fx"),
    "BTC-USD": ("Bitcoin", "crypto"),
}
TREND = dict(entry_lookback=20, exit_lookback=10, atr_period=14, atr_stop_mult=3.0,
             trend_fast=50, trend_slow=200, cost_bps=5.0, max_leverage=10.0)
YEARS = 12
RISK_LEVELS = [0.005, 0.01, 0.02]    # per-trade risk; the knob that scales return & DD


def fmt_pf(pf):
    return "inf" if pf == float("inf") else f"{pf:.2f}"


def daily_pnl_series(trades):
    """Realized PnL summed by exit date (UTC, day-normalized)."""
    if not trades:
        return pd.Series(dtype=float)
    s = pd.Series([t["pnl"] for t in trades],
                  index=[pd.Timestamp(t["exit_time"]).normalize() for t in trades])
    return s.groupby(level=0).sum()


def main():
    p = argparse.ArgumentParser(description="Diversified futures trend research")
    p.add_argument("--cash", type=float, default=2000.0)
    p.add_argument("--folds", type=int, default=5)
    p.add_argument("--max-dd-budget", type=float, default=25.0, help="user DD tolerance %")
    args = p.parse_args()

    frames, missing = {}, []
    for tkr in SLEEVES:
        try:
            frames[tkr] = get_daily(tkr, YEARS)
        except Exception as e:
            missing.append(f"{tkr} ({e})")
    if not frames:
        print("No data fetched:", missing)
        return

    # Per-sleeve detail + combined portfolio trades, at the middle risk level.
    mid = 0.01
    sub = args.cash / len(frames)
    per, all_pnl, all_trades, span_years = {}, [], [], {}
    for tkr, df in frames.items():
        res = run_trend(df, start_cash=sub, risk_pct=mid, **TREND)
        per[tkr] = metrics(res, sub)
        all_trades.extend(res["trades"])
        all_pnl.append(daily_pnl_series(res["trades"]))
        span_years[tkr] = max((df.index[-1] - df.index[0]).days / 365.0, 0.01)

    combined = pd.concat(all_pnl).groupby(level=0).sum().sort_index()
    idx = pd.date_range(combined.index.min(), combined.index.max(), freq="D")
    curve = args.cash + combined.reindex(idx, fill_value=0.0).cumsum()
    port = metrics({"trades": all_trades, "curve": list(curve), "end": float(curve.iloc[-1])}, args.cash)
    years = (idx[-1] - idx[0]).days / 365.0
    cagr = (curve.iloc[-1] / args.cash) ** (1 / years) - 1 if years > 0 else 0.0

    md = ["# Diversified futures trend — the managed-futures approach", "",
          f"${args.cash:,.0f} start · {len(frames)} asset classes · daily Donchian/EMA "
          f"trend · ~{years:.0f}y · equal-risk sleeves, combined. Deployable via micro "
          "futures (MES, MGC, MCL, M2K, M6E, MBT).", ""]
    if missing:
        md.append(f"_Sleeves with no data, skipped: {', '.join(missing)}._\n")

    md += ["## Per-sleeve (each on 1/N of capital, 1% risk/trade)", "",
           "| Sleeve | Class | Return | CAGR | PF | Max DD | Trades |", "|---|---|--:|--:|--:|--:|--:|"]
    for tkr, (label, cls) in SLEEVES.items():
        if tkr not in per:
            continue
        m = per[tkr]
        c = (1 + m["ret"] / 100) ** (1 / span_years[tkr]) - 1
        md.append(f"| {label} | {cls} | {m['ret']:+.1f}% | {c*100:+.1f}%/yr | "
                  f"{fmt_pf(m['profit_factor'])} | {m['max_dd']:.1f}% | {m['trades']} |")

    md += ["", "## Portfolio (all sleeves combined, 1% risk/trade)", "",
           f"- **Total return:** {port['ret']:+.1f}% over ~{years:.0f}y · **CAGR {cagr*100:+.1f}%/yr**",
           f"- **Max drawdown:** {port['max_dd']:.1f}%   ·   **Profit factor:** {fmt_pf(port['profit_factor'])}",
           f"- **Trades:** {port['trades']} (~{port['trades']/years:.0f}/yr) · Win rate {port['win_rate']:.0f}%",
           "",
           f"_Diversification check: portfolio max DD ({port['max_dd']:.1f}%) vs the average "
           f"single-sleeve DD ({sum(per[t]['max_dd'] for t in per)/len(per):.1f}%) — lower = "
           "the sleeves don't all draw down together._"]

    # Walk-forward across time on the portfolio curve.
    md += ["", "## Walk-forward — portfolio return per time fold", ""]
    seg = len(curve) // args.folds
    fold_cells, wins = [], 0
    for i in range(args.folds):
        a, b = i * seg, ((i + 1) * seg if i < args.folds - 1 else len(curve))
        e0, e1 = curve.iloc[a], curve.iloc[b - 1]
        fr = (e1 / e0 - 1) * 100 if e0 else 0.0
        wins += 1 if fr > 0 else 0
        fold_cells.append(f"{fr:+.1f}%")
    md.append("- " + " · ".join(f"Fold {i+1}: {c}" for i, c in enumerate(fold_cells)))
    md.append(f"- **Profitable in {wins}/{args.folds} folds.**")

    # Risk-sizing table: what per-trade risk targets the user's DD budget.
    md += ["", f"## Risk sizing — finding your {args.max_dd_budget:.0f}% DD budget", "",
           "| Risk/trade | Portfolio CAGR | Max DD |", "|--:|--:|--:|"]
    for rp in RISK_LEVELS:
        apnl = []
        for tkr, df in frames.items():
            res = run_trend(df, start_cash=sub, risk_pct=rp, **TREND)
            apnl.append(daily_pnl_series(res["trades"]))
        comb = pd.concat(apnl).groupby(level=0).sum().sort_index()
        cv = args.cash + comb.reindex(idx, fill_value=0.0).cumsum()
        mm = metrics({"trades": [], "curve": list(cv), "end": float(cv.iloc[-1])}, args.cash)
        cg = (cv.iloc[-1] / args.cash) ** (1 / years) - 1 if years > 0 else 0.0
        flag = " ←≈budget" if abs(mm["max_dd"] - args.max_dd_budget) < 6 else ""
        md.append(f"| {rp*100:.1f}% | {cg*100:+.1f}%/yr | {mm['max_dd']:.1f}%{flag} |")

    md += ["", "> Diversified daily trend is the strategy with the most durable evidence "
           "(managed-futures / time-series momentum), and futures give low cost + the "
           "leverage to run it on a small account. But: returns are LUMPY (long flat "
           "stretches), diversification thins in a crash (correlations →1), and continuous-"
           "contract data has roll artefacts. Forward paper-test before real money."]

    report = "\n".join(md)
    sp = os.environ.get("GITHUB_STEP_SUMMARY")
    if sp:
        with open(sp, "a", encoding="utf-8") as f:
            f.write(report + "\n")
    print("\n" + report)


if __name__ == "__main__":
    main()
