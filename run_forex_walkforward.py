#!/usr/bin/env python3
"""Walk-forward validation on a focused 1-2 pair set.

    python run_forex_walkforward.py --source oanda --pairs EURUSD=X,GBPUSD=X

For a single-pair focus, the honest fresh-data test is OTHER TIME PERIODS on the same
pair (not other pairs). This chops each pair's H4 history into several sequential
out-of-sample chunks and checks whether each strategy stays profitable across most of
them. A strategy is a CANDIDATE for a pair only if it is profitable in a majority of
the time folds (a real edge is temporally consistent; a fluke shows up in one fold).
Writes a markdown report to the GitHub Actions job summary.
"""

import argparse
import os

from forex.data import get_oanda, get_fx, to_oanda
from forex.research import STRATEGIES, walk_forward

H4_YEARS = 7.0   # deeper history = more, longer folds


def fmt_pf(pf):
    return "inf" if pf == float("inf") else f"{pf:.2f}"


def fetch(pair, source):
    if source == "oanda":
        return get_oanda(to_oanda(pair), years=H4_YEARS, granularity="H4")
    return get_fx(pair, "2y", "1h")


def main():
    p = argparse.ArgumentParser(description="Walk-forward forex validation (focused pairs)")
    p.add_argument("--source", choices=["yfinance", "oanda"], default="oanda")
    p.add_argument("--pairs", default="EURUSD=X,GBPUSD=X")
    p.add_argument("--cash", type=float, default=2000.0)
    p.add_argument("--folds", type=int, default=5)
    args = p.parse_args()
    pairs = [s.strip() for s in args.pairs.split(",") if s.strip()]

    md = ["# Forex walk-forward — focused pairs", "",
          f"${args.cash:,.0f} start · {', '.join(pairs)} · H4 · {args.folds} sequential "
          f"out-of-sample time folds (~{H4_YEARS/args.folds:.1f}y each). Fixed params, no "
          "fitting. CANDIDATE = profitable in a majority of folds (temporal consistency).",
          ""]
    verdicts = []
    for pair in pairs:
        try:
            df = fetch(pair, args.source)
        except Exception as e:
            md.append(f"**{pair}: data error — {e}**")
            continue
        md += [f"## {pair}", "",
               "| Strategy | " + " | ".join(f"Fold {i+1}" for i in range(args.folds)) +
               " | Folds profitable |",
               "|---|" + "---|" * args.folds + "--:|"]
        for strat, (fn, params) in STRATEGIES.items():
            folds = walk_forward(df, pair, fn, params, n_folds=args.folds, start_cash=args.cash)
            cells = [f"{f['ret']:+.1f}%<br><sub>pf {fmt_pf(f['profit_factor'])} "
                     f"n{f['trades']}</sub>" for f in folds]
            wins = sum(1 for f in folds if f["ret"] > 0 and f["profit_factor"] > 1.0)
            n = len(folds)
            cells += [""] * (args.folds - n)
            ok = wins > n / 2
            verdicts.append((pair, strat, wins, n, ok))
            md.append(f"| {strat} | " + " | ".join(cells) +
                      f" | {wins}/{n} {'✅' if ok else '❌'} |")
            print(f"{pair} {strat}: {wins}/{n} folds profitable "
                  f"({[round(f['ret'],1) for f in folds]})")
        md.append("")

    md += ["## Verdict", ""]
    for pair, strat, wins, n, ok in verdicts:
        md.append(f"- **{pair} / {strat}**: profitable in {wins}/{n} folds → "
                  f"{'✅ CANDIDATE — earns a forward paper test' if ok else '❌ not consistent'}")
    md += ["", "> Walk-forward across time is the right validity check for a focused "
           "pair set. A candidate here still must be forward-paper-tested on OANDA "
           "practice before any real money. No backtest is a promise."]

    report = "\n".join(md)
    sp = os.environ.get("GITHUB_STEP_SUMMARY")
    if sp:
        with open(sp, "a", encoding="utf-8") as f:
            f.write(report + "\n")
    print("\n" + report)


if __name__ == "__main__":
    main()
