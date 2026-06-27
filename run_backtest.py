#!/usr/bin/env python3
"""Backtest the ORB bot on one real trading day, starting from $2000.

Examples:
    python run_backtest.py --symbol AAPL --date 2026-06-26
    python run_backtest.py --symbol AAPL --date 2026-06-26 --csv data/aapl.csv
    python run_backtest.py --symbol SPY  --date 2026-06-26 --risk 0.01 --max-stop 0.01
"""

import argparse
import os
from datetime import datetime

from sim.config import CONFIG
from sim.data import get_day_bars
from sim.engine import SimEngine
from sim.report import format_report
from sim.state import build_state, write_state


def main() -> None:
    p = argparse.ArgumentParser(description="ORB stock-bot backtester")
    p.add_argument("--symbol", required=True, help="ticker, e.g. AAPL")
    p.add_argument("--date", required=True, help="YYYY-MM-DD (a trading day)")
    p.add_argument("--csv", help="load bars from CSV instead of a live data source")
    p.add_argument("--cash", type=float, default=CONFIG.start_cash)
    p.add_argument("--risk", type=float, default=CONFIG.risk_per_trade_pct,
                   help="risk fraction per trade (0.01 = 1%%)")
    p.add_argument("--max-stop", type=float, default=CONFIG.max_stop_pct,
                   help="max per-trade stop as fraction of entry")
    p.add_argument("--daily-max-loss", type=float, default=CONFIG.daily_max_loss_pct,
                   help="daily drawdown circuit-breaker (0.05 = 5%%)")
    p.add_argument("--no-short", action="store_true", help="disable short entries")
    p.add_argument("--state", default=os.path.join("logs", "state.json"),
                   help="where to write the dashboard state file")
    args = p.parse_args()

    cfg = CONFIG
    cfg.start_cash = args.cash
    cfg.risk_per_trade_pct = args.risk
    cfg.max_stop_pct = args.max_stop
    cfg.daily_max_loss_pct = args.daily_max_loss
    if args.no_short:
        cfg.allow_short = False

    df = get_day_bars(args.symbol, args.date, cfg, csv=args.csv)
    port = SimEngine(cfg).run(df, args.symbol)
    print(format_report(port, args.symbol, args.date))

    state = build_state(port, mode="backtest", symbol=args.symbol,
                        updated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        halted=any(f["reason"] == "breaker"
                                   for t in port.trades for f in t.exits))
    write_state(args.state, state)
    print(f"\nDashboard state written to {args.state} "
          f"-> run `python dashboard.py` and open http://localhost:5000")


if __name__ == "__main__":
    main()
