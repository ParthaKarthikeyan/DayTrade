#!/usr/bin/env python3
"""Backtest the 'top-down + area-of-interest' discretionary method, mechanized.

    python run_forex_structure.py --source oanda

This is a faithful, RULE-BASED translation of the popular price-action lesson:
multi-timeframe trend alignment (EMA50>EMA200 proxy) + a horizontal level with >=3
touches (the 'area of interest') + a retest-with-reaction entry, ATR stop, trail to
the next area. The honest point of mechanizing it: on a static chart every drawn level
looks like it worked (hindsight). A causal backtest with real spread costs and an
out-of-sample split is the only way to tell an edge from a good-looking whiteboard.

All parameters are FIXED at textbook values (pivot width 3, 3 touches, ATRx2, EMA
50/200) — no sweep, nothing to curve-fit. We judge it the same way as every other
strategy this session:
  - chronological TRAIN / TEST split on the majors (must work on BOTH slices),
  - a 5-fold walk-forward across time on EUR/USD + GBP/USD,
  - and a generalisation check on pairs it was never examined on.
Writes a markdown report to the GitHub Actions job summary.
"""

import argparse
import os

from forex.data import get_oanda, get_fx, to_oanda
from forex.research import (run_structure_retest, split_train_test, walk_forward,
                            metrics)

PAIRS = ["EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X", "USDCAD=X"]
UNSEEN_PAIRS = ["USDCHF=X", "NZDUSD=X", "EURGBP=X"]
FOCUS = ["EURUSD=X", "GBPUSD=X"]                 # walk-forward focus pairs
# granularity -> (oanda code, years, yfinance (interval, period) fallback)
TIMEFRAMES = {"H4": ("H4", 5.0, ("1h", "2y")), "D": ("D", 10.0, ("1d", "10y"))}
PARAMS = dict(trend_fast=50, trend_slow=200, atr_stop_mult=2.0, pivot_w=3,
              min_touches=3, cluster_bps=10.0, zone_bps=10.0, level_window=120)


def fmt_pf(pf):
    return "inf" if pf == float("inf") else f"{pf:.2f}"


def fetch(pair, source, oanda_gran, years, yf_fb):
    if source == "oanda":
        return get_oanda(to_oanda(pair), years=years, granularity=oanda_gran)
    interval, period = yf_fb
    return get_fx(pair, period, interval)


def consistent(tr, te):
    return tr["ret"] > 0 and te["ret"] > 0 and te["profit_factor"] > 1.0


def evaluate_pair(df, pair, cash, train_frac):
    tr_df, te_df = split_train_test(df, train_frac)
    tr = metrics(run_structure_retest(tr_df, to_oanda(pair), start_cash=cash, **PARAMS), cash)
    te = metrics(run_structure_retest(te_df, to_oanda(pair), start_cash=cash, **PARAMS), cash)
    return tr, te


def main():
    p = argparse.ArgumentParser(description="Structure/area-of-interest retest backtest")
    p.add_argument("--source", choices=["yfinance", "oanda"], default="oanda")
    p.add_argument("--cash", type=float, default=2000.0)
    p.add_argument("--train-frac", type=float, default=0.65)
    p.add_argument("--folds", type=int, default=5)
    args = p.parse_args()

    frames = {}
    src = (f"OANDA {','.join(TIMEFRAMES)}" if args.source == "oanda"
           else "yfinance (shallow)")
    md = ["# Forex research — structure + area-of-interest retest (mechanized)", "",
          f"${args.cash:,.0f} start · {len(PAIRS)} majors · {src} · spread-costed · "
          f"train={args.train_frac:.0%}/test held out · FIXED params (no sweep). "
          "CANDIDATE = profitable in BOTH train AND test on a majority of pairs.", "",
          "| TF | Pair | Train ret | Train PF | Test ret | Test PF | Test n | Test DD | Consistent? |",
          "|---|---|--:|--:|--:|--:|--:|--:|:--:|"]

    by_tf = {}
    for tf, (oanda_gran, years, yf_fb) in TIMEFRAMES.items():
        ok = 0
        rows = 0
        for pair in PAIRS:
            try:
                df = fetch(pair, args.source, oanda_gran, years, yf_fb)
            except Exception as e:
                md.append(f"| {tf} | {pair} | data error — {e} | | | | | | |")
                continue
            frames[(tf, pair)] = df
            tr, te = evaluate_pair(df, pair, args.cash, args.train_frac)
            c = consistent(tr, te)
            ok += 1 if c else 0
            rows += 1
            md.append(f"| {tf} | {pair} | {tr['ret']:+.2f}% | {fmt_pf(tr['profit_factor'])} | "
                      f"{te['ret']:+.2f}% | {fmt_pf(te['profit_factor'])} | {te['trades']} | "
                      f"{te['max_dd']:.1f}% | {'✅' if c else '❌'} |")
            print(f"{tf:3} {pair:10} train {tr['ret']:+6.2f}%/pf{fmt_pf(tr['profit_factor'])}"
                  f"  TEST {te['ret']:+6.2f}%/pf{fmt_pf(te['profit_factor'])} n={te['trades']}")
        by_tf[tf] = (ok, rows)

    md += ["", "## Verdict — main search", ""]
    candidate_tf = None
    for tf, (ok, rows) in by_tf.items():
        status = "✅ CANDIDATE" if rows and ok > rows / 2 else "❌ no robust edge"
        if rows and ok > rows / 2:
            candidate_tf = candidate_tf or tf
        md.append(f"- **{tf}**: consistent on {ok}/{rows} pairs → {status}")

    # ---- Walk-forward across time (focus pairs, both TFs) ------------------
    md += ["", "## Walk-forward — return per out-of-sample time fold", "",
           "| TF | Pair | Folds profitable | Per-fold returns |", "|---|---|--:|---|"]
    for tf in TIMEFRAMES:
        for pair in FOCUS:
            df = frames.get((tf, pair))
            if df is None:
                continue
            folds = walk_forward(df, to_oanda(pair), run_structure_retest, PARAMS,
                                 n_folds=args.folds, start_cash=args.cash)
            wins = sum(1 for f in folds if f["ret"] > 0 and f["profit_factor"] > 1.0)
            cells = " · ".join(f"{f['ret']:+.1f}%" for f in folds)
            md.append(f"| {tf} | {pair} | {wins}/{len(folds)} | {cells} |")

    # ---- Generalisation to UNSEEN pairs (H4) ------------------------------
    md += ["", "## Generalisation — unseen pairs (H4)", "",
           "Pairs never examined while building this. Edge should carry over if real.", "",
           "| Pair | Train ret | Train PF | Test ret | Test PF | Consistent? |",
           "|---|--:|--:|--:|--:|:--:|"]
    h4_gran, h4_years, h4_yf = TIMEFRAMES["H4"]
    unseen_ok = 0
    for pair in UNSEEN_PAIRS:
        try:
            df = fetch(pair, args.source, h4_gran, h4_years, h4_yf)
        except Exception as e:
            md.append(f"| {pair} | data error — {e} | | | | |")
            continue
        tr, te = evaluate_pair(df, pair, args.cash, args.train_frac)
        c = consistent(tr, te)
        unseen_ok += 1 if c else 0
        md.append(f"| {pair} | {tr['ret']:+.2f}% | {fmt_pf(tr['profit_factor'])} | "
                  f"{te['ret']:+.2f}% | {fmt_pf(te['profit_factor'])} | {'✅' if c else '❌'} |")
    md.append(f"\n**Unseen-pair result: consistent on {unseen_ok}/{len(UNSEEN_PAIRS)}.**")

    md += ["", "> This mechanizes a discretionary method as faithfully as fixed rules "
           "allow; a human drawing zones by eye may do better or worse. A candidate "
           "earns a forward paper test only if it works out-of-sample AND generalises "
           "to unseen pairs. No backtest is a promise — forward-validate before real money."]

    report = "\n".join(md)
    sp = os.environ.get("GITHUB_STEP_SUMMARY")
    if sp:
        with open(sp, "a", encoding="utf-8") as f:
            f.write(report + "\n")
    print("\n" + report)


if __name__ == "__main__":
    main()
