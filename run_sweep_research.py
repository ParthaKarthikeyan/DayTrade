#!/usr/bin/env python3
"""Sweep-reversal research — does the "1-minute scalping" video survive costs?

    python run_sweep_research.py            # needs ALPACA_API_KEY/SECRET_KEY

The video's mechanical core: premark S/R levels (prior-day H/L/C, premarket
range, 15m swings), then fade FAILED BREAKOUTS of those levels on 1-minute
bars in the opening session — stop beyond the sweep extreme, fixed-R target.
Tested here on QQQ (the tradable twin of his NQ futures — we have no futures
broker, and QQQ costs are futures-grade) over ~14 months of Alpaca IEX
1-minute history.

Judgement is at STRESSED costs (2x the realistic per-side estimate) with a
5-fold contiguous walk-forward, per house rules:
  CANDIDATE only if stressed net > 0, >= 3/5 folds positive, the LAST fold
  positive, >= 40 closed trades, and >= 1 grid-neighbor also passing (the
  cell must not be an island).

Writes the verdict to paper_ledger/sweep_research.json and a markdown report
to stdout + $GITHUB_STEP_SUMMARY.
"""

import json
import os
from datetime import date, timedelta
from itertools import product

from sweep.data import get_minute_bars
from sweep.levels import build_day_frames, levels_for_all_days
from sweep.strategy import SweepParams
from sweep.backtest import run_book

SYMBOL = "QQQ"
LOOKBACK_DAYS = 420                  # ~14 months -> ~290 sessions
START_CASH = 10_000.0
COST_BASE = 1.0                      # bps/side: ~0.2bp half-spread + slippage headroom
COST_STRESS = 2.0                    # judged here (2x house stress)
FOLDS = 5
MIN_TRADES = 40

GRID = {
    "rr": [1.5, 2.0, 3.0],
    "reclaim_bars": [3, 5],
    "body_mult": [0.75, 1.25],
    "session_end": ["10:30", "11:30"],
    "be_at_1r": [False, True],
    "allow_short": [True, False],
}
VERDICT_PATH = os.path.join("paper_ledger", "sweep_research.json")


def fold_slices(days: list, k: int) -> list:
    n = len(days)
    edges = [round(i * n / k) for i in range(k + 1)]
    return [days[edges[i]:edges[i + 1]] for i in range(k)]


def run_cell(params: dict, cost: float, prebuilt, day_subset=None) -> dict:
    """One replay. Levels always come from the full-history precompute (they
    only use PAST data, so fold replays stay look-ahead-free)."""
    p = SweepParams(cost_bps_side=cost, **params)
    days, frames, lvls = prebuilt
    if day_subset is not None:
        days = day_subset
    out = run_book(None, p, START_CASH, prebuilt=(days, frames, lvls))
    out.pop("days", None)
    out.pop("trade_log", None)
    return out


def evaluate(params: dict, prebuilt, folds) -> dict:
    base = run_cell(params, COST_BASE, prebuilt)
    stress = run_cell(params, COST_STRESS, prebuilt)
    fold_nets = [run_cell(params, COST_STRESS, prebuilt, day_subset=f)["net"]
                 for f in folds]
    pos = sum(1 for x in fold_nets if x > 0)
    passed = (stress["net"] > 0 and pos >= 3 and fold_nets[-1] > 0
              and stress["trades"] >= MIN_TRADES)
    return {"params": params, "base": base, "stress": stress,
            "fold_nets": [round(x, 2) for x in fold_nets],
            "folds_positive": pos, "passed": passed}


def neighbors(params: dict) -> list:
    """Cells one step away in rr or reclaim_bars (same everything else)."""
    out = []
    for key in ("rr", "reclaim_bars"):
        vals = GRID[key]
        i = vals.index(params[key])
        for j in (i - 1, i + 1):
            if 0 <= j < len(vals):
                q = dict(params)
                q[key] = vals[j]
                out.append(q)
    return out


def main():
    end = date.today().isoformat()
    start = (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    cache = os.path.join("logs", f"qqq_1m_{start}_{end}.pkl")
    print(f"Fetching {SYMBOL} 1m bars {start} -> {end} (IEX) ...")
    bars = get_minute_bars(SYMBOL, start, end, cache=cache)
    days, frames = build_day_frames(bars)
    print(f"{len(bars):,} bars across {len(days)} sessions "
          f"({days[0]} -> {days[-1]})")

    lvls = levels_for_all_days(days, frames)   # fixed level params, shared
    prebuilt = (days, frames, lvls)
    folds = fold_slices(days, FOLDS)

    keys = list(GRID)
    cells = [dict(zip(keys, combo)) for combo in product(*GRID.values())]
    print(f"Evaluating {len(cells)} grid cells at {COST_BASE}/{COST_STRESS} bps/side ...")
    results = [evaluate(c, prebuilt, folds) for c in cells]

    by_key = {tuple(r["params"][k] for k in keys): r for r in results}
    for r in results:
        r["neighbors_passing"] = sum(
            1 for q in neighbors(r["params"])
            if by_key.get(tuple(q[k] for k in keys), {}).get("passed"))

    passing = [r for r in results if r["passed"] and r["neighbors_passing"] >= 1]
    passing.sort(key=lambda r: r["stress"]["net"], reverse=True)
    results.sort(key=lambda r: r["stress"]["net"], reverse=True)

    md = [f"# Sweep-reversal research — {SYMBOL} 1m, {len(days)} sessions",
          "",
          f"Judged at {COST_STRESS} bps/side (2x realistic). Guards: stressed "
          f"net>0, >=3/{FOLDS} folds, last fold>0, >={MIN_TRADES} trades, "
          ">=1 passing neighbor.",
          "",
          f"**{len([r for r in results if r['passed']])} / {len(cells)} cells "
          f"pass the fold guards; {len(passing)} also have a passing neighbor.**",
          "",
          "| rr | reclaim | body | end | BE@1R | shorts | trades | win% | PF | "
          "net@base | net@stress | folds+ | last fold | nbrs |",
          "|--:|--:|--:|---|---|---|--:|--:|--:|--:|--:|--:|--:|--:|"]
    for r in results[:12]:
        p, s = r["params"], r["stress"]
        md.append(f"| {p['rr']} | {p['reclaim_bars']} | {p['body_mult']} "
                  f"| {p['session_end']} | {'Y' if p['be_at_1r'] else 'N'} "
                  f"| {'Y' if p['allow_short'] else 'N'} | {s['trades']} "
                  f"| {s['win_rate']} | {s['pf']} | {r['base']['net']:+,.0f} "
                  f"| {s['net']:+,.0f} | {r['folds_positive']}/{FOLDS} "
                  f"| {r['fold_nets'][-1]:+,.0f} | {r['neighbors_passing']} |")

    if passing:
        best = passing[0]
        s = best["stress"]
        md += ["", f"## CANDIDATE: {json.dumps(best['params'])}",
               f"Stressed: net ${s['net']:+,.0f} over {s['sessions']} sessions "
               f"(${s['per_session']:+,.2f}/session), {s['trades']} trades, "
               f"{s['win_rate']}% win, PF {s['pf']}, maxDD {s['max_dd_pct']}%. "
               f"Folds {best['fold_nets']}."]
        verdict = "CANDIDATE"
        frozen = best
    else:
        md += ["", "## NO CANDIDATE — no cell survives stressed costs + "
               "walk-forward + neighbor guards. The edge, if any, is thinner "
               "than 2x QQQ costs in the opening session."]
        verdict = "REJECTED"
        frozen = results[0] if results else None

    report = "\n".join(md)
    print("\n" + report)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(report + "\n")

    os.makedirs(os.path.dirname(VERDICT_PATH), exist_ok=True)
    with open(VERDICT_PATH, "w", encoding="utf-8") as f:
        json.dump({"strategy": "qqq_sweep_reversal", "symbol": SYMBOL,
                   "verdict": verdict,
                   "sessions": len(days),
                   "window": [days[0].isoformat(), days[-1].isoformat()],
                   "cost_bps_side_base": COST_BASE,
                   "cost_bps_side_stress": COST_STRESS,
                   "best": frozen, "top": results[:12]}, f, indent=1)
    print(f"\nVerdict written to {VERDICT_PATH}: {verdict}")


if __name__ == "__main__":
    main()
