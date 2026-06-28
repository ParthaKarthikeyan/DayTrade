#!/usr/bin/env python3
"""Out-of-sample forex strategy search (lower-frequency trend following).

    python run_forex_research.py --source oanda

Tests FIXED, textbook trend-following configs across several majors on H4 and Daily
bars, with a chronological train/test split. The point is NOT to find a green number
by trying things until one works — it is to see whether a *non-curve-fit* strategy is
robustly profitable on data it never saw. A strategy is flagged a CANDIDATE only if it
is profitable out-of-sample (test return > 0 and profit factor > 1) on a majority of
pairs. Writes a markdown report to the GitHub Actions job summary.
"""

import argparse
import os

from forex.data import get_oanda, get_fx, to_oanda
from forex.research import STRATEGIES, evaluate

PAIRS = ["EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X", "USDCAD=X"]

# granularity -> (oanda code, history years, yfinance interval/period fallback)
TIMEFRAMES = {
    "H4": ("H4", 5.0, ("1h", "2y")),
    "D":  ("D", 10.0, ("1d", "10y")),
}


def fmt_pf(pf):
    return "inf" if pf == float("inf") else f"{pf:.2f}"


def fetch(pair, source, oanda_gran, years, yf_fallback):
    if source == "oanda":
        return get_oanda(to_oanda(pair), years=years, granularity=oanda_gran)
    interval, period = yf_fallback
    return get_fx(pair, period, interval)


def main():
    p = argparse.ArgumentParser(description="Out-of-sample forex trend-following search")
    p.add_argument("--source", choices=["yfinance", "oanda"], default="oanda")
    p.add_argument("--cash", type=float, default=2000.0)
    p.add_argument("--train-frac", type=float, default=0.65)
    args = p.parse_args()

    # results[(tf, strat)] = list of (pair, train_metrics, test_metrics)
    results = {}
    lines = []
    for tf, (oanda_gran, years, yf_fb) in TIMEFRAMES.items():
        for pair in PAIRS:
            try:
                df = fetch(pair, args.source, oanda_gran, years, yf_fb)
            except Exception as e:
                print(f"{tf} {pair}: data error — {e}")
                continue
            ev = evaluate(df, pair, start_cash=args.cash, train_frac=args.train_frac)
            for strat, md in ev.items():
                tr, te = md["train"], md["test"]
                results.setdefault((tf, strat), []).append((pair, tr, te))
                line = (f"{tf:3} {pair:10} {strat:16} "
                        f"train: {tr['ret']:+7.2f}% pf={fmt_pf(tr['profit_factor'])} "
                        f"n={tr['trades']:<3}  "
                        f"TEST: {te['ret']:+7.2f}% pf={fmt_pf(te['profit_factor'])} "
                        f"n={te['trades']:<3} dd={te['max_dd']:.1f}%")
                print(line)
                lines.append(line)

    # --- honest verdict: count out-of-sample winners per (tf, strategy) ----
    src_label = (f"OANDA {','.join(t for t in TIMEFRAMES)}" if args.source == "oanda"
                 else "yfinance (shallow)")
    md = ["# Forex research — lower-frequency trend following (out-of-sample)", "",
          f"${args.cash:,.0f} start · {len(PAIRS)} majors · {src_label} · "
          f"train={args.train_frac:.0%} / test={1-args.train_frac:.0%} held out.", "",
          "Fixed textbook params — **no parameter sweep**, nothing curve-fit. A "
          "strategy is a CANDIDATE only if it is profitable in BOTH the train slice "
          "AND the held-out test slice on a majority of pairs (consistency = signal; "
          "winning only out-of-sample = noise).", "",
          "| Timeframe | Strategy | Pair | Train ret | Train PF | Test ret | Test PF | Test trades | Test maxDD |",
          "|---|---|---|--:|--:|--:|--:|--:|--:|"]
    verdict = []
    for (tf, strat), rows in sorted(results.items()):
        consistent = 0   # profitable in BOTH train and test (a real edge is, noise isn't)
        for pair, tr, te in rows:
            win = (tr["ret"] > 0 and te["ret"] > 0 and te["profit_factor"] > 1.0)
            consistent += 1 if win else 0
            md.append(f"| {tf} | {strat} | {pair} | {tr['ret']:+.2f}% | "
                      f"{fmt_pf(tr['profit_factor'])} | {te['ret']:+.2f}% | "
                      f"{fmt_pf(te['profit_factor'])} | {te['trades']} | {te['max_dd']:.1f}% |")
        n = len(rows)
        status = "✅ CANDIDATE" if consistent > n / 2 else "❌ no robust edge"
        verdict.append(f"- **{tf} / {strat}**: profitable in BOTH train & test on "
                       f"{consistent}/{n} pairs → {status}")

    md += ["", "## Verdict (out-of-sample)", ""] + verdict
    md += ["", "> Trend following trades infrequently, so spread drag is small — but "
           "that only matters if the directional edge is real. Only an out-of-sample "
           "CANDIDATE earns a forward paper test. No backtest result is a promise."]
    report = "\n".join(md)
    sp = os.environ.get("GITHUB_STEP_SUMMARY")
    if sp:
        with open(sp, "a", encoding="utf-8") as f:
            f.write(report + "\n")
    print("\n" + report)


if __name__ == "__main__":
    main()
