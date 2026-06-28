#!/usr/bin/env python3
"""Crypto research — does higher activity survive costs where FX couldn't?

    python run_crypto_research.py --source coinbase

Crypto's pitch is a better volatility-to-cost ratio than FX. We test it honestly: the
same fade + Donchian-trend families, on BTC/ETH at 1h / 6h / 1d, with a PERCENTAGE cost
model shown at GROSS (0), LOW (20bps ≈ 0.1%/side, low-fee venue) and MID (60bps ≈
0.3%/side) round-trip cost — plus a walk-forward across 5 out-of-sample time folds at
the LOW cost. A strategy is a CANDIDATE only if it is net-profitable at LOW cost AND
holds across a majority of folds. Writes a markdown report to the job summary.
"""

import argparse
import os

from crypto.data import get_coinbase, get_yf
from crypto.engine import STRATEGIES, split_train_test, walk_forward, metrics

PRODUCTS = ["BTC-USD", "ETH-USD"]
# interval -> (history years, fade max_hold for that timeframe)
TFS = {"1h": (4.0, 24), "6h": (8.0, 10), "1d": (8.0, 8)}
COSTS = [0.0, 20.0, 60.0]                     # round-trip bps: gross, low-fee, mid-fee
LOW = 20.0                                    # the realistic level we judge on


def fmt_pf(pf):
    return "inf" if pf == float("inf") else f"{pf:.2f}"


def fetch(product, source, interval, years):
    if source == "coinbase":
        return get_coinbase(product, interval=interval, years=years)
    return get_yf(product, period="60d", interval=interval)


def main():
    p = argparse.ArgumentParser(description="Crypto strategy research (cost-aware)")
    p.add_argument("--source", choices=["coinbase", "yfinance"], default="coinbase")
    p.add_argument("--cash", type=float, default=2000.0)
    p.add_argument("--folds", type=int, default=5)
    args = p.parse_args()

    md = ["# Crypto research — does activity survive costs?", "",
          f"${args.cash:,.0f} start · {', '.join(PRODUCTS)} · fade + Donchian trend · "
          "cost shown GROSS / LOW 20bps / MID 60bps round-trip · walk-forward at LOW.",
          "", "| TF | Asset | Strat | Trades/yr | Gross ret | Net@20 ret/PF | Net@20 maxDD | Net@60 ret | WF@20 |",
          "|---|---|---|--:|--:|--:|--:|--:|--:|"]
    verdicts = []
    for interval, (years, max_hold) in TFS.items():
        for product in PRODUCTS:
            try:
                df = fetch(product, args.source, interval, years)
            except Exception as e:
                md.append(f"| {interval} | {product} | — | data error — {e} | | | | |")
                continue
            span = max((df.index[-1] - df.index[0]).days / 365.0, 0.01)
            for strat, (fn, base) in STRATEGIES.items():
                params = dict(base)
                if strat == "fade":
                    params["max_hold"] = max_hold
                g = metrics(fn(df, start_cash=args.cash, cost_bps=0.0, **params), args.cash)
                n20 = metrics(fn(df, start_cash=args.cash, cost_bps=20.0, **params), args.cash)
                n60 = metrics(fn(df, start_cash=args.cash, cost_bps=60.0, **params), args.cash)
                tpy = n20["trades"] / span
                folds = walk_forward(df, fn, params, LOW, n_folds=args.folds,
                                     start_cash=args.cash)
                wf = sum(1 for f in folds if f["ret"] > 0 and f["profit_factor"] > 1.0)
                ok = n20["ret"] > 0 and n20["profit_factor"] > 1.0 and wf > len(folds) / 2
                verdicts.append((interval, product, strat, ok, n20, wf, len(folds), tpy, folds))
                md.append(f"| {interval} | {product} | {strat} | {tpy:,.0f} | "
                          f"{g['ret']:+.0f}% | **{n20['ret']:+.0f}%**/{fmt_pf(n20['profit_factor'])} | "
                          f"{n20['max_dd']:.0f}% | {n60['ret']:+.0f}% | {wf}/{len(folds)} |")
                print(f"{interval} {product} {strat}: {tpy:,.0f}/yr gross {g['ret']:+.0f}% "
                      f"net20 {n20['ret']:+.0f}%/pf{fmt_pf(n20['profit_factor'])} "
                      f"net60 {n60['ret']:+.0f}% wf {wf}/{len(folds)}")

    md += ["", "## Verdict (net of LOW 20bps cost + walk-forward)", ""]
    cands = [v for v in verdicts if v[3]]
    for interval, product, strat, ok, n20, wf, nf, tpy, folds in verdicts:
        md.append(f"- **{interval} / {product} / {strat}**: {tpy:,.0f} trades/yr · "
                  f"net@20 {n20['ret']:+.0f}% (PF {fmt_pf(n20['profit_factor'])}, "
                  f"maxDD {n20['max_dd']:.0f}%) · WF {wf}/{nf} → "
                  f"{'✅ CANDIDATE' if ok else '❌ no'}")

    # Per-fold detail for trend candidates — reveals bear-market behaviour (fold ~3-4
    # spans the 2022 crypto bear). A long-only bull-bias artefact would show big losses
    # there; surviving it is what separates a real trend edge from sample luck.
    md += ["", "### Per-fold returns — trend candidates (chronological, fold 1 = oldest)", ""]
    for interval, product, strat, ok, n20, wf, nf, tpy, folds in verdicts:
        if strat == "trend" and ok:
            cells = " · ".join(f"{f['ret']:+.0f}% (dd{f['max_dd']:.0f}%)" for f in folds)
            md.append(f"- **{interval} {product}**: {cells}")
    if cands:
        md.append(f"\n**{len(cands)} candidate(s) survive low-fee costs AND walk-forward — "
                  "worth a forward paper test on a low-fee venue. Mid-fee (60bps) results "
                  "show how sensitive each is to the venue you actually trade on.**")
    else:
        md.append("\n**Nothing survived even low-fee costs + walk-forward. Crypto's better "
                  "volatility-to-cost ratio wasn't enough for these strategies.**")
    md += ["", "> Net columns model fees+spread as round-trip basis points of notional. "
           "Costs vary hugely by venue (low-fee ~0.1%/side, retail Coinbase ~0.6%/side). "
           "Forward-validate any candidate on a paper/testnet account before real money."]

    report = "\n".join(md)
    sp = os.environ.get("GITHUB_STEP_SUMMARY")
    if sp:
        with open(sp, "a", encoding="utf-8") as f:
            f.write(report + "\n")
    print("\n" + report)


if __name__ == "__main__":
    main()
