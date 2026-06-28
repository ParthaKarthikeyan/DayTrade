#!/usr/bin/env python3
"""Free forex backtest: 3 variants on FX majors via yfinance (last ~60 days).

    python run_forex_backtest.py
    python run_forex_backtest.py --pairs EURUSD=X,GBPUSD=X --period 60d

Compares pure ORB vs pure trend vs trend-filtered ORB. Writes a markdown table
to the GitHub Actions job summary when run in CI. (15m FX history is ~60 days.)
"""

import argparse
import os

from forex.config import DEFAULT
from forex.data import get_fx, get_oanda, to_oanda, from_csv
from forex.engine import run_variants

VARIANTS = ["pure_orb", "trend_orb", "pure_trend"]


def fmt_pf(pf):
    return "inf" if pf == float("inf") else f"{pf:.2f}"


def main():
    p = argparse.ArgumentParser(description="Forex 3-variant backtester (yfinance)")
    p.add_argument("--pairs", default=",".join(DEFAULT.pairs))
    p.add_argument("--source", choices=["yfinance", "oanda"], default="yfinance")
    p.add_argument("--period", default="60d", help="yfinance period (15m caps ~60d)")
    p.add_argument("--years", type=float, default=2.0, help="OANDA history length")
    p.add_argument("--cash", type=float, default=DEFAULT.start_cash)
    p.add_argument("--risk", type=float, default=DEFAULT.risk_per_trade_pct)
    p.add_argument("--csv", help="load one pair's bars from CSV instead of a feed")
    args = p.parse_args()

    cfg = DEFAULT
    cfg.start_cash = args.cash
    cfg.risk_per_trade_pct = args.risk
    pairs = [s.strip() for s in args.pairs.split(",") if s.strip()]

    rows = []          # (pair, variant, metrics)
    for pair in pairs:
        try:
            if args.csv:
                df = from_csv(args.csv)
            elif args.source == "oanda":
                df = get_oanda(to_oanda(pair), years=args.years)
            else:
                df = get_fx(pair, args.period, cfg.interval)
        except Exception as e:
            print(f"{pair}: data error — {e}")
            continue
        res = run_variants(df, cfg)
        for v in VARIANTS:
            rows.append((pair, v, res[v]))
            m = res[v]
            print(f"{pair:10} {v:11} {m['ret']:+7.2f}%  trades={m['trades']:3}  "
                  f"win={m['win_rate']:.0f}%  pf={fmt_pf(m['profit_factor'])}  "
                  f"maxDD={m['max_dd']:.2f}%")

    window = (f"{args.years:g}y of OANDA M15" if args.source == "oanda"
              else f"{args.period} of yfinance 15m")
    md = ["# Forex backtest — pure ORB vs trend vs trend-filtered ORB", "",
          f"${cfg.start_cash:,.0f} start · majors · London-session ORB · {window}.", "",
          "| Pair | Variant | Return | Trades | Win% | Profit factor | Max DD |",
          "|---|---|--:|--:|--:|--:|--:|"]
    for pair, v, m in rows:
        md.append(f"| {pair} | {v} | {m['ret']:+.2f}% | {m['trades']} | "
                  f"{m['win_rate']:.0f}% | {fmt_pf(m['profit_factor'])} | {m['max_dd']:.2f}% |")
    md += ["", "> Free yfinance FX data (15m ≈ last 60 days), majors only, 1-pip spread "
           "modeled. Indicative — a longer/cleaner backtest would use OANDA history. "
           "Forward-validate on OANDA practice before any real money."]
    out = "\n".join(md)
    sp = os.environ.get("GITHUB_STEP_SUMMARY")
    if sp:
        with open(sp, "a", encoding="utf-8") as f:
            f.write(out + "\n")
    print("\n" + out)


if __name__ == "__main__":
    main()
