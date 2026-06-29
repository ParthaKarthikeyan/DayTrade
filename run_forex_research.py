#!/usr/bin/env python3
"""Out-of-sample forex strategy search + candidate stress-tests.

    python run_forex_research.py --source oanda

Tests FIXED, textbook strategy families (trend following + mean reversion) across
majors on H4 and Daily bars, with a chronological train/test split. A strategy is a
CANDIDATE only if it is profitable in BOTH the train slice AND the held-out test slice
on a majority of pairs (consistency = signal; winning only out-of-sample = noise).

The mean-reversion family cleared that bar on the original 5 pairs, so this script also
runs two stress-tests on it:
  1. UNSEEN PAIRS — pairs the family was never examined on (does the edge generalise?).
  2. PARAMETER ROBUSTNESS — a neighbourhood of params, not a sweep (is the edge stable,
     or fragile at one lucky point?).
Writes a markdown report to the GitHub Actions job summary.
"""

import argparse
import os

from forex.data import get_oanda, get_fx, to_oanda
from forex.research import STRATEGIES, MR_ROBUST_GRID, evaluate, split_train_test, metrics

# Pairs the families were developed against.
PAIRS = ["EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X", "USDCAD=X"]
# Fresh pairs held out entirely for the generalisation test.
UNSEEN_PAIRS = ["USDCHF=X", "NZDUSD=X", "EURGBP=X"]

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


def consistent(tr, te):
    """A pair 'works' if profitable in BOTH train and test (PF>1 out-of-sample)."""
    return tr["ret"] > 0 and te["ret"] > 0 and te["profit_factor"] > 1.0


def main():
    p = argparse.ArgumentParser(description="Out-of-sample forex strategy search")
    p.add_argument("--source", choices=["yfinance", "oanda"], default="oanda")
    p.add_argument("--cash", type=float, default=2000.0)
    p.add_argument("--train-frac", type=float, default=0.65)
    args = p.parse_args()

    frames = {}     # (tf, pair) -> df   (cache so stress-tests don't refetch)
    results = {}    # (tf, strat) -> [(pair, train_m, test_m)]
    for tf, (oanda_gran, years, yf_fb) in TIMEFRAMES.items():
        for pair in PAIRS:
            try:
                df = fetch(pair, args.source, oanda_gran, years, yf_fb)
            except Exception as e:
                print(f"{tf} {pair}: data error — {e}")
                continue
            frames[(tf, pair)] = df
            for strat, m in evaluate(df, pair, args.cash, args.train_frac).items():
                results.setdefault((tf, strat), []).append((pair, m["train"], m["test"]))
                print(f"{tf:3} {pair:10} {strat:16} "
                      f"train {m['train']['ret']:+6.2f}%/pf{fmt_pf(m['train']['profit_factor'])}  "
                      f"TEST {m['test']['ret']:+6.2f}%/pf{fmt_pf(m['test']['profit_factor'])} "
                      f"n={m['test']['trades']}")

    src_label = (f"OANDA {','.join(TIMEFRAMES)}" if args.source == "oanda"
                 else "yfinance (shallow)")
    md = ["# Forex research — out-of-sample strategy search", "",
          f"${args.cash:,.0f} start · {len(PAIRS)} majors · {src_label} · "
          f"train={args.train_frac:.0%} / test={1-args.train_frac:.0%} held out. "
          "Fixed params, no sweep. CANDIDATE = profitable in BOTH train AND test on a "
          "majority of pairs.", "",
          "| TF | Strategy | Pair | Train ret | Train PF | Test ret | Test PF | Test n | Test DD |",
          "|---|---|---|--:|--:|--:|--:|--:|--:|"]
    verdict = []
    for (tf, strat), rows in sorted(results.items()):
        c = 0
        for pair, tr, te in rows:
            c += 1 if consistent(tr, te) else 0
            md.append(f"| {tf} | {strat} | {pair} | {tr['ret']:+.2f}% | "
                      f"{fmt_pf(tr['profit_factor'])} | {te['ret']:+.2f}% | "
                      f"{fmt_pf(te['profit_factor'])} | {te['trades']} | {te['max_dd']:.1f}% |")
        n = len(rows)
        status = "✅ CANDIDATE" if c > n / 2 else "❌ no robust edge"
        verdict.append(f"- **{tf} / {strat}**: consistent on {c}/{n} pairs → {status}")
    md += ["", "## Verdict — main search", ""] + verdict

    # ---- STRESS TEST 1: bollinger_fade on UNSEEN pairs (H4) ----------------
    mr_fn, mr_params = STRATEGIES["bollinger_fade"]
    md += ["", "## Stress test 1 — Bollinger fade on UNSEEN pairs (H4)", "",
           "Pairs the family was never examined on. If the edge is real it should "
           "generalise; if it was pair-picking it won't.", "",
           "| Pair | Train ret | Train PF | Test ret | Test PF | Test n | Consistent? |",
           "|---|--:|--:|--:|--:|--:|:--:|"]
    h4_gran, h4_years, h4_yf = TIMEFRAMES["H4"]
    unseen_ok = 0
    for pair in UNSEEN_PAIRS:
        try:
            df = fetch(pair, args.source, h4_gran, h4_years, h4_yf)
        except Exception as e:
            md.append(f"| {pair} | data error — {e} | | | | | |")
            continue
        tr_df, te_df = split_train_test(df, args.train_frac)
        tr = metrics(mr_fn(tr_df, to_oanda(pair), start_cash=args.cash, **mr_params), args.cash)
        te = metrics(mr_fn(te_df, to_oanda(pair), start_cash=args.cash, **mr_params), args.cash)
        ok = consistent(tr, te)
        unseen_ok += 1 if ok else 0
        md.append(f"| {pair} | {tr['ret']:+.2f}% | {fmt_pf(tr['profit_factor'])} | "
                  f"{te['ret']:+.2f}% | {fmt_pf(te['profit_factor'])} | {te['trades']} | "
                  f"{'✅' if ok else '❌'} |")
    md.append(f"\n**Unseen-pair result: consistent on {unseen_ok}/{len(UNSEEN_PAIRS)}.**")

    # ---- STRESS TEST 2: parameter robustness neighbourhood (H4) ------------
    md += ["", "## Stress test 2 — parameter robustness (H4, original 5 pairs)", "",
           "A neighbourhood of params (SMA 15/20/25 × bands 1.5/2.0/2.5σ) — not a "
           "sweep to pick a winner, a check that the edge survives its neighbours.", "",
           "| Params | Consistent pairs (of 5) |", "|---|--:|"]
    robust_scores = []
    for name, (fn, params) in MR_ROBUST_GRID.items():
        c = 0
        for pair in PAIRS:
            df = frames.get(("H4", pair))
            if df is None:
                continue
            tr_df, te_df = split_train_test(df, args.train_frac)
            tr = metrics(fn(tr_df, to_oanda(pair), start_cash=args.cash, **params), args.cash)
            te = metrics(fn(te_df, to_oanda(pair), start_cash=args.cash, **params), args.cash)
            c += 1 if consistent(tr, te) else 0
        robust_scores.append(c)
        md.append(f"| {name} | {c} |")
    if robust_scores:
        avg = sum(robust_scores) / len(robust_scores)
        strong = sum(1 for s in robust_scores if s >= 3)
        md.append(f"\n**Robustness: {strong}/{len(robust_scores)} param sets are "
                  f"consistent on ≥3 pairs; average {avg:.1f}/5 across the neighbourhood.**")

    md += ["", "> A candidate earns a forward paper test only if it generalises to "
           "unseen pairs AND is stable across its parameter neighbourhood. No backtest "
           "result is a promise — forward-validate on practice before any real money."]
    report = "\n".join(md)
    sp = os.environ.get("GITHUB_STEP_SUMMARY")
    if sp:
        with open(sp, "a", encoding="utf-8") as f:
            f.write(report + "\n")
    print("\n" + report)


if __name__ == "__main__":
    main()
