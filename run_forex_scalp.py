#!/usr/bin/env python3
"""Scalping test — does a short-term fade survive spread costs?

    python run_forex_scalp.py --source oanda

Runs the mean-reversion fade at SCALPING frequency (M5 and M1 bars) on GBP/USD +
EUR/USD, and reports GROSS (zero-cost) vs NET (realistic spread) side by side. The
gap between them is the spread tax. Scalping maximises trade count, so it maximises
that tax — this script exists to measure it honestly, not to assume the answer.
Also runs the same walk-forward time-fold check used for the slower strategies.
Writes a markdown report to the GitHub Actions job summary.
"""

import argparse
import os

from forex.data import get_oanda, get_fx, to_oanda
from forex.research import run_mean_reversion, metrics, walk_forward, SPREAD_PIPS

PAIRS = ["GBPUSD=X", "EURUSD=X"]

# timeframe -> (oanda granularity, history years, scalp params, yfinance fallback)
SCALP_TFS = {
    "M5": ("M5", 5.0, dict(sma_period=20, band_k=2.0, stop_k=3.0,
                           trend_ema=200, max_hold=24), ("5m", "60d")),
    "M1": ("M1", 1.5, dict(sma_period=20, band_k=2.0, stop_k=3.0,
                           trend_ema=200, max_hold=60), ("1m", "7d")),
}


def fmt_pf(pf):
    return "inf" if pf == float("inf") else f"{pf:.2f}"


def fetch(pair, source, gran, years, yf_fb):
    if source == "oanda":
        return get_oanda(to_oanda(pair), years=years, granularity=gran)
    interval, period = yf_fb
    return get_fx(pair, period, interval)


def main():
    p = argparse.ArgumentParser(description="Forex scalping cost test")
    p.add_argument("--source", choices=["yfinance", "oanda"], default="oanda")
    p.add_argument("--cash", type=float, default=2000.0)
    p.add_argument("--folds", type=int, default=5)
    args = p.parse_args()

    md = ["# Forex scalping — does a short-term fade survive the spread?", "",
          f"${args.cash:,.0f} start · {', '.join(PAIRS)} · Bollinger fade at M5/M1 · "
          "GROSS = zero cost, NET = realistic spread. The gap is the spread tax.", "",
          "| TF | Pair | Trades/yr | Spread | GROSS ret | GROSS PF | NET ret | NET PF | WF folds net |",
          "|---|---|--:|--:|--:|--:|--:|--:|--:|"]
    verdicts = []
    for tf, (gran, years, params, yf_fb) in SCALP_TFS.items():
        for pair in PAIRS:
            try:
                df = fetch(pair, args.source, gran, years, yf_fb)
            except Exception as e:
                md.append(f"| {tf} | {pair} | data error — {e} | | | | | | |")
                continue
            span_yrs = max((df.index[-1] - df.index[0]).days / 365.0, 0.01)
            oa = to_oanda(pair)
            gross = metrics(run_mean_reversion(df, oa, start_cash=args.cash,
                                               spread_pips=0.0, **params), args.cash)
            net = metrics(run_mean_reversion(df, oa, start_cash=args.cash, **params), args.cash)
            tpy = net["trades"] / span_yrs
            folds = walk_forward(df, oa, run_mean_reversion, params,
                                 n_folds=args.folds, start_cash=args.cash)
            wf_win = sum(1 for f in folds if f["ret"] > 0 and f["profit_factor"] > 1.0)
            verdicts.append((tf, pair, gross, net, tpy, wf_win, len(folds)))
            md.append(f"| {tf} | {pair} | {tpy:,.0f} | {SPREAD_PIPS.get(oa,1.5)}p | "
                      f"{gross['ret']:+.1f}% | {fmt_pf(gross['profit_factor'])} | "
                      f"**{net['ret']:+.1f}%** | {fmt_pf(net['profit_factor'])} | "
                      f"{wf_win}/{len(folds)} |")
            print(f"{tf} {pair}: {tpy:,.0f} trades/yr  gross {gross['ret']:+.1f}%/"
                  f"pf{fmt_pf(gross['profit_factor'])}  net {net['ret']:+.1f}%/"
                  f"pf{fmt_pf(net['profit_factor'])}  wf {wf_win}/{len(folds)}")

    md += ["", "## Verdict", ""]
    any_ok = False
    for tf, pair, gross, net, tpy, wf_win, nf in verdicts:
        ok = net["ret"] > 0 and net["profit_factor"] > 1.0 and wf_win > nf / 2
        any_ok = any_ok or ok
        tax = gross["ret"] - net["ret"]
        md.append(f"- **{tf} / {pair}**: spread tax ≈ {tax:.1f}pp of return "
                  f"({tpy:,.0f} trades/yr × cost). Net {'✅ survives' if ok else '❌ does not survive costs'}.")
    if not any_ok:
        md.append("\n**No scalping variant survived realistic spreads — exactly the cost "
                  "wall that sank the high-frequency strategies. The edge that survived "
                  "(GBP/USD H4 fade, ~22 trades/yr) did so *because* it trades rarely.**")
    md += ["", "> Net figures model a realistic round-trip spread per trade. Gross "
           "ignores all cost. Forward-validate any survivor on OANDA practice before "
           "real money — but a strategy that fails net here will fail live."]

    report = "\n".join(md)
    sp = os.environ.get("GITHUB_STEP_SUMMARY")
    if sp:
        with open(sp, "a", encoding="utf-8") as f:
            f.write(report + "\n")
    print("\n" + report)


if __name__ == "__main__":
    main()
