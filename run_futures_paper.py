#!/usr/bin/env python3
"""Forward paper-trading bot for the diversified $10k futures trend system.

Run daily (see .github/workflows/futures_paper.yml) or manually:

    python run_futures_paper.py --cash 10000

Each run fetches fresh daily bars for every sleeve, replays the exact deployment rules
in WHOLE micro contracts, and records what two paper books (5% and 8% risk/trade) are
doing right now — today's target positions and the running equity. It appends one row
per calendar day to a persistent ledger (logs/futures_paper.json) so a real forward
track record accumulates over time; the workflow commits that ledger back to the repo.

No broker, no real money. This is the validation gate that must hold up forward before
any real capital is considered. The deployment date is stamped on the first run; bars
after it are the genuine out-of-sample record (everything before is the backtest the
rules were frozen on, shown only for continuity).
"""

import argparse
import json
import os
from datetime import datetime, timezone

from futures.data import get_daily
from futures.paper import SLEEVES, HISTORY, RISK_BOOKS, book_snapshot
from sim.state import build_live_state, write_state

# The durable forward record. NOT under logs/ (which is gitignored) — this ledger is
# the track record we want to keep and commit back across the ephemeral runners.
LEDGER = os.path.join("paper_ledger", "futures_paper.json")
# The full trade-by-trade log (every closed trade + today's open positions, per book).
# Rewritten each run — it's a deterministic replay, so this is the authoritative log.
TRADES = os.path.join("paper_ledger", "futures_trades.json")


def load_ledger(path: str, cash: float, today: str) -> dict:
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                led = json.load(f)
            led.setdefault("deployment_date", today)
            led.setdefault("cash", cash)
            led.setdefault("history", [])
            return led
        except (OSError, json.JSONDecodeError):
            pass
    return {"deployment_date": today, "cash": cash, "history": []}


def upsert_today(history: list, row: dict) -> list:
    """Append today's row, replacing any existing row for the same date (re-runs)."""
    history = [r for r in history if r.get("date") != row["date"]]
    history.append(row)
    history.sort(key=lambda r: r["date"])
    return history


def fmt_pos(positions: list) -> str:
    if not positions:
        return "_flat — no open positions_"
    parts = []
    for p in positions:
        parts.append(f"{p['side']} {p['contracts']}× {p['micro']} ({p['sleeve']}) "
                     f"@ {p['entry']}, stop {p['stop']}, unreal ${p['unrealized']:+,.0f}")
    return "<br>".join(parts)


def main():
    p = argparse.ArgumentParser(description="Forward paper bot for the $10k futures system")
    p.add_argument("--cash", type=float, default=10000.0)
    p.add_argument("--ledger", default=LEDGER)
    p.add_argument("--trades", default=TRADES)
    p.add_argument("--state", default=os.path.join("logs", "futures_state.json"))
    args = p.parse_args()

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    led = load_ledger(args.ledger, args.cash, today)
    cash = led["cash"]

    frames, missing = {}, []
    for tkr in SLEEVES:
        try:
            frames[tkr] = get_daily(tkr, HISTORY.get(tkr, 7))
        except Exception as e:                      # noqa: BLE001 — log & skip a dead feed
            missing.append(f"{tkr} ({e})")
    if not frames:
        print("No data fetched:", missing)
        return

    books = {name: book_snapshot(frames, cash, rp) for name, rp in RISK_BOOKS.items()}

    # --- append today's row to the persistent ledger --------------------------
    row = {"date": today}
    for name, b in books.items():
        row[name] = {"equity": b["equity"], "ret_pct": b["ret_pct"],
                     "max_dd": b["max_dd"], "open_positions": len(b["positions"])}
    led["history"] = upsert_today(led["history"], row)
    led["last_run"] = today
    os.makedirs(os.path.dirname(args.ledger) or ".", exist_ok=True)
    with open(args.ledger, "w", encoding="utf-8") as f:
        json.dump(led, f, indent=2)

    dep = led["deployment_date"]

    # --- durable trade-by-trade log (every closed trade + open positions) -----
    trades_doc = {"deployment_date": dep, "generated": today, "cash": cash, "books": {}}
    for name, b in books.items():
        trades_doc["books"][name] = {
            "risk_pct": b["risk_pct"],
            "closed": b["trade_log"],
            "open": b["positions"],
        }
    with open(args.trades, "w", encoding="utf-8") as f:
        json.dump(trades_doc, f, indent=2)

    # --- forward-only segment (bars since deployment) -------------------------
    fwd = [r for r in led["history"] if r["date"] >= dep]
    days_live = len(fwd)

    # --- dashboard state (primary = 8% book) ----------------------------------
    primary = books["8pct"]
    pos = None
    if primary["positions"]:
        p0 = primary["positions"][0]
        pos = {"side": p0["side"], "shares": p0["contracts"],
               "entry_price": p0["entry"], "stop": p0["stop"], "half_taken": False}
    write_state(args.state, build_live_state(
        mode="paper", symbol="futures-10k (8% book)", updated_at=today,
        start_cash=cash, equity=primary["equity"], halted=False, position=pos,
        equity_curve=primary["equity_curve"], trades=[]))

    # --- markdown recap -------------------------------------------------------
    md = [f"# Futures $10k forward paper bot — {today}", "",
          f"Diversified daily trend · {len(frames)} sleeves · whole micro contracts · "
          f"two paper books (5% / 8% risk per trade).",
          f"**Deployed {dep}** · **{days_live} day(s)** of forward record"
          + (f" · _no data: {', '.join(missing)}_" if missing else ""), "",
          "| Book | Equity | Return | Max DD | Trades | Win % | Open |",
          "|---|--:|--:|--:|--:|--:|--:|"]
    for name, b in books.items():
        md.append(f"| {b['risk_pct']*100:.0f}% risk | ${b['equity']:,.0f} | "
                  f"{b['ret_pct']:+.1f}% | {b['max_dd']:.1f}% | {b['trades']} | "
                  f"{b['win_rate']:.0f}% | {len(b['positions'])} |")

    for name, b in books.items():
        md += ["", f"## {b['risk_pct']*100:.0f}% book — today's target positions",
               "", fmt_pos(b["positions"])]

    if days_live >= 2:
        first8 = next((r["8pct"]["equity"] for r in fwd), cash)
        last8 = fwd[-1]["8pct"]["equity"]
        first5 = next((r["5pct"]["equity"] for r in fwd), cash)
        last5 = fwd[-1]["5pct"]["equity"]
        md += ["", "## Forward record since deployment", "",
               f"- 8% book: ${first8:,.0f} → ${last8:,.0f} "
               f"({(last8/first8-1)*100:+.1f}% over {days_live} days)",
               f"- 5% book: ${first5:,.0f} → ${last5:,.0f} "
               f"({(last5/first5-1)*100:+.1f}% over {days_live} days)"]
    else:
        md += ["", "_Forward record starts accumulating from the next run. Today is the "
               "baseline; come back after several sessions to compare live vs. backtest._"]

    # --- trade log: closed trades since deployment (8% primary book) ----------
    prim_trades = books["8pct"]["trade_log"]
    fwd_trades = [t for t in prim_trades if t["exit_time"] >= dep]
    md += ["", "## Trades since deployment (8% book)", ""]
    if fwd_trades:
        md += ["| Exit | Sleeve | Side | Qty | Entry | Exit | P&L $ | Reason |",
               "|---|---|---|--:|--:|--:|--:|---|"]
        for t in fwd_trades:
            md.append(f"| {t['exit_time']} | {t['sleeve']} | {t['side']} | "
                      f"{t['contracts']} | {t['entry']} | {t['exit']} | "
                      f"{t['pnl']:+,.0f} | {t['reason']} |")
    else:
        md.append("_No trades have closed since deployment yet — open positions are "
                  "still running (see today's target positions above). Full backtest "
                  "trade history is in `paper_ledger/futures_trades.json`._")
    md += ["", f"_Full per-trade log ({len(prim_trades)} closed trades incl. backtest, "
           "both books) committed to `paper_ledger/futures_trades.json`._"]

    md += ["", "> Whole-micro sizing at $10k is lumpy — many sleeves sit at 1 contract, so "
           "realised risk is whatever one micro costs, not the % set. Continuous-contract / "
           "proxy data carries roll artefacts. This is paper only; forward results must hold "
           "up before any real capital is considered."]

    report = "\n".join(md)
    print("\n" + report)
    sp = os.environ.get("GITHUB_STEP_SUMMARY")
    if sp:
        with open(sp, "a", encoding="utf-8") as f:
            f.write(report + "\n")


if __name__ == "__main__":
    main()
