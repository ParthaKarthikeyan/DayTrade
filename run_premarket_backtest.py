#!/usr/bin/env python3
"""Free premarket gap-and-go backtest (yfinance, recent ~30 days).

    python run_premarket_backtest.py --start 2026-06-15 --end 2026-06-26

Writes a markdown summary to the GitHub Actions job summary when run in CI.
1-minute data (incl. premarket) is only available for ~the last month on Yahoo.
"""

import argparse
import os
from datetime import date, timedelta

from sim.config import CONFIG
from premarket.backtest import run
from premarket.universe import DEFAULT_UNIVERSE


def format_markdown(s: dict) -> str:
    n = s["notes"]
    out = [f"# Premarket gap-and-go backtest ({s['start']} → {s['end']})", "",
           f"$ {s['start_cash']:,.0f} start · **{s.get('strategy', 'flag')}** entry · "
           f"long-only · $1–$10 gappers · premarket→open+2h · instant-cut exits.", "",
           "| Metric | Value |", "|---|--:|",
           f"| Final equity | ${s['final_equity']:,.2f} |",
           f"| Total return | {s['total_return']:+.2f}% |",
           f"| Days (traded) | {s['days']} ({s['days_traded']}) |",
           f"| Green / red days | {s['green_days']} / {s['red_days']} |",
           f"| Trades | {s['trades']} |",
           f"| Win rate | {s['win_rate']:.0f}% |",
           f"| Best / worst day | {s['best_day']:+.2f}% / {s['worst_day']:+.2f}% |",
           f"| Max drawdown | {s['max_drawdown']:.2f}% |",
           f"| Daily-cap halts | {s['breaker_days']} |", ""]
    out.append("### Per-day")
    out.append("| Date | Symbol | Gap% | Trades | P&L | Day% |")
    out.append("|---|---|--:|--:|--:|--:|")
    for p in s["per_day"]:
        sym = p["symbol"] or "—"
        gap = f"{p.get('gap', 0):+.1f}" if p["symbol"] else "—"
        out.append(f"| {p['date']} | {sym} | {gap} | {p['trades']} | "
                   f"${p['pnl']:+,.2f} | {p['ret']:+.2f}% |")
    out += ["",
            f"> Data quality: {n['thin_or_missing']} thin/missing names skipped, "
            f"{n['days_no_candidate']} day(s) had no usable candidate. "
            "Yahoo premarket small-cap coverage is patchy and the universe is a fixed "
            "list, so this is **indicative, not exhaustive** — slippage modeled at 25bps "
            "is still optimistic for real small-cap spreads."]
    return "\n".join(out)


def main():
    p = argparse.ArgumentParser(description="Premarket gap-and-go backtester (yfinance)")
    default_end = date.today()
    default_start = default_end - timedelta(days=14)
    p.add_argument("--start", default=default_start.isoformat())
    p.add_argument("--end", default=default_end.isoformat())
    p.add_argument("--cash", type=float, default=CONFIG.start_cash)
    p.add_argument("--min-gap", type=float, default=CONFIG.pm_min_gap_pct)
    p.add_argument("--strategy", choices=["flag", "first_pullback"],
                   default=CONFIG.pm_strategy)
    p.add_argument("--universe-file", help="optional: one ticker per line")
    args = p.parse_args()

    cfg = CONFIG
    cfg.start_cash = args.cash
    cfg.pm_min_gap_pct = args.min_gap
    cfg.pm_strategy = args.strategy

    universe = DEFAULT_UNIVERSE
    if args.universe_file:
        with open(args.universe_file) as f:
            universe = [ln.strip().upper() for ln in f if ln.strip()]

    s = run(cfg, universe, args.start, args.end)
    md = format_markdown(s)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(md + "\n")
    print(md)


if __name__ == "__main__":
    main()
