#!/usr/bin/env python3
"""Backtest the ORB bot across a RANGE of real trading days.

The account compounds day to day (each day starts with the prior day's ending
equity), so you see the real multi-day trajectory, not isolated days. The 5%
circuit-breaker resets each morning (it's a *daily* cap).

    python run_multiday.py --symbols AAPL --start 2026-04-01 --end 2026-06-26

On GitHub Actions the summary is also written to the job summary page so you can
read it from the GitHub mobile app.
"""

import argparse
import os
from datetime import date, timedelta

from sim.config import CONFIG
from sim.data import get_day_bars
from sim.engine import SimEngine
from sim.report import compute_metrics


def weekdays(start: date, end: date):
    d = start
    while d <= end:
        if d.weekday() < 5:          # Mon–Fri (holidays just return no data)
            yield d
        d += timedelta(days=1)


def _parse_date(s: str) -> date:
    y, m, d = (int(x) for x in s.split("-"))
    return date(y, m, d)


def run_symbol(symbol: str, days, cfg, start_cash: float, data_fn=get_day_bars):
    """Compound the ORB bot over `days`. Returns a list of per-day result dicts."""
    equity = start_cash
    per_day = []
    for d in days:
        cfg.start_cash = equity
        try:
            bars = data_fn(symbol, d.isoformat(), cfg)
        except Exception:
            continue                 # weekend / holiday / no data — skip silently
        port = SimEngine(cfg).run(bars, symbol)
        end_eq = port.equity_curve[-1][1] if port.equity_curve else equity
        halted = any(f["reason"] == "breaker" for t in port.trades for f in t.exits)
        m = compute_metrics(port)
        per_day.append({
            "date": d.isoformat(), "start": equity, "end": end_eq,
            "pnl": end_eq - equity, "ret": (end_eq / equity - 1) * 100 if equity else 0,
            "trades": m["num_trades"], "wins": m["wins"], "halted": halted,
        })
        equity = end_eq
    return per_day


def summarize(symbol: str, per_day, start_cash: float) -> dict:
    if not per_day:
        return {"symbol": symbol, "days": 0}
    equities = [start_cash] + [p["end"] for p in per_day]
    peak, mdd = equities[0], 0.0
    for e in equities:
        peak = max(peak, e)
        mdd = max(mdd, (peak - e) / peak if peak else 0.0)
    total_trades = sum(p["trades"] for p in per_day)
    total_wins = sum(p["wins"] for p in per_day)
    active = [p for p in per_day if p["trades"] > 0]
    final = equities[-1]
    return {
        "symbol": symbol,
        "days": len(per_day),
        "active_days": len(active),
        "final_equity": final,
        "total_return": (final / start_cash - 1) * 100 if start_cash else 0,
        "green_days": sum(1 for p in active if p["pnl"] > 0),
        "red_days": sum(1 for p in active if p["pnl"] < 0),
        "total_trades": total_trades,
        "win_rate": (total_wins / total_trades * 100) if total_trades else 0,
        "best_day": max((p["ret"] for p in per_day), default=0),
        "worst_day": min((p["ret"] for p in per_day), default=0),
        "max_drawdown": mdd * 100,
        "breaker_days": sum(1 for p in per_day if p["halted"]),
    }


def format_markdown(summaries, per_days, start_cash: float, rng: str) -> str:
    out = [f"# Multi-day backtest ({rng})", "",
           f"Each symbol is its own ${start_cash:,.0f} account, compounding daily.", ""]
    out.append("| Symbol | Final | Return | Days(traded) | Green/Red | Trades | Win% | Best | Worst | MaxDD | Breaker |")
    out.append("|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|")
    for s in summaries:
        if not s.get("days"):
            out.append(f"| {s['symbol']} | — | no data | 0 | — | — | — | — | — | — | — |")
            continue
        out.append(
            f"| **{s['symbol']}** | ${s['final_equity']:,.2f} | {s['total_return']:+.2f}% | "
            f"{s['days']}({s['active_days']}) | {s['green_days']}/{s['red_days']} | "
            f"{s['total_trades']} | {s['win_rate']:.0f}% | {s['best_day']:+.2f}% | "
            f"{s['worst_day']:+.2f}% | {s['max_drawdown']:.2f}% | {s['breaker_days']} |")
    out.append("")
    out.append("> Reality check: synthetic demos look great; real days are choppier. "
               "A small or negative edge here means **do not** risk real money.")
    return "\n".join(out)


def main():
    p = argparse.ArgumentParser(description="Multi-day ORB backtester")
    p.add_argument("--symbols", required=True, help="comma-separated tickers")
    p.add_argument("--start", required=True, help="YYYY-MM-DD")
    p.add_argument("--end", required=True, help="YYYY-MM-DD")
    p.add_argument("--cash", type=float, default=CONFIG.start_cash)
    p.add_argument("--risk", type=float, default=CONFIG.risk_per_trade_pct)
    p.add_argument("--no-short", action="store_true")
    args = p.parse_args()

    cfg = CONFIG
    cfg.risk_per_trade_pct = args.risk
    if args.no_short:
        cfg.allow_short = False

    days = list(weekdays(_parse_date(args.start), _parse_date(args.end)))
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    rng = f"{args.start} → {args.end}"

    summaries, per_days = [], {}
    for sym in symbols:
        per = run_symbol(sym, days, cfg, args.cash)
        per_days[sym] = per
        s = summarize(sym, per, args.cash)
        summaries.append(s)
        if s.get("days"):
            print(f"{sym}: ${s['final_equity']:,.2f}  {s['total_return']:+.2f}%  "
                  f"over {s['days']} days, {s['total_trades']} trades, "
                  f"win {s['win_rate']:.0f}%, worst day {s['worst_day']:+.2f}%, "
                  f"maxDD {s['max_drawdown']:.2f}%")
        else:
            print(f"{sym}: no data in range")

    md = format_markdown(summaries, per_days, args.cash, rng)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(md + "\n")
    print("\n" + md)


if __name__ == "__main__":
    main()
