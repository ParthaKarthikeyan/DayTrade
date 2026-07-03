#!/usr/bin/env python3
"""Live PAPER crypto bot on Alpaca — the validated 6h Donchian trend, $10k book.

What deployed and why: the repo's own research (`run_crypto_research.py`,
Coinbase data, %-cost model, walk-forward) found the 6h Donchian/EMA trend on
BTC + ETH is the only family that survives realistic costs with a walk-forward
majority (BTC 4/5, ETH 4/5 folds at 20bps; still positive at 60bps). The 1h
variants die on costs — exactly like FX scalping did. So this bot trades the
6h rules, long/flat (Alpaca crypto can't short), on real Alpaca paper orders.

How it runs: stateless-per-run (cron hourly, see
.github/workflows/crypto_paper.yml). Each run fetches 6h candles, evaluates the
frozen rules on the last COMPLETED bar, reconciles the book's positions against
the Alpaca paper account, and orders the difference as tagged market orders
(BTC/ETH books are deep; fills come back with real prices). State lives in the
committed ledger, so the ephemeral runner doesn't matter.

The book: notional $10k (CRYPTO_BANKROLL), compounding via its own ledger —
never the shared account's balance. A daily loss brake pauses NEW entries when
the book drops 2% intraday. There is deliberately NO "+1% and stop" flatten:
this is a trend system whose edge (per the walk-forward) comes from letting
multi-day winners run — banking +1% would amputate exactly the trades that pay.
The premarket book plays the daily-target game; this book compounds.

Rules (frozen, rules_version below): EMA(50) > EMA(200) regime AND close > prior
20-bar high -> long; exit on 3xATR chandelier stop, close < prior 10-bar low, or
regime flip. Risk 1% of book equity per trade, spot only (no leverage).
"""

import argparse
import json
import os
import time
import uuid
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from crypto.data import get_coinbase
from sim.config import CONFIG
from sim.gist import GistPublisher
from sim.indicators import ema
from forex.research import atr

RULES_VERSION = 1
SYMBOLS = {"BTC/USD": "BTC-USD", "ETH/USD": "ETH-USD"}   # alpaca -> coinbase product
INTERVAL = "6h"
HISTORY_DAYS = 260          # ~1040 6h bars; EMA200 + Donchian20 need ~220
TREND = dict(entry_lookback=20, exit_lookback=10, atr_period=14, atr_stop_mult=3.0,
             trend_fast=50, trend_slow=200)
RISK_PCT = 0.01             # 1% of book equity risked per trade
BANKROLL = float(os.getenv("CRYPTO_BANKROLL", "10000"))
DAILY_BRAKE_PCT = 0.02      # book -2% on the day -> no NEW entries until tomorrow

LEDGER = os.path.join("paper_ledger", "crypto_paper.json")
TRADES = os.path.join("paper_ledger", "crypto_trades.json")


# ----------------------------- ledger ---------------------------------------
def load_ledger(path: str, today: str) -> dict:
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                led = json.load(f)
            if led.get("rules_version") != RULES_VERSION:
                led["rules_version"] = RULES_VERSION
                led["deployment_date"] = today       # rules changed: restart the clock
            led.setdefault("positions", {})
            led.setdefault("history", [])
            return led
        except (OSError, json.JSONDecodeError):
            pass
    return {"deployment_date": today, "rules_version": RULES_VERSION,
            "bankroll": BANKROLL, "cash": BANKROLL, "positions": {}, "history": []}


def save_json(path: str, doc: dict):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
    os.replace(tmp, path)


def upsert_row(history: list, row: dict) -> list:
    history = [r for r in history if r.get("date") != row["date"]]
    history.append(row)
    history.sort(key=lambda r: r["date"])
    return history


# ----------------------------- signals --------------------------------------
def prep(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["fast"] = ema(d["close"], TREND["trend_fast"])
    d["slow"] = ema(d["close"], TREND["trend_slow"])
    d["atr"] = atr(d, TREND["atr_period"])
    d["don_hi"] = d["high"].rolling(TREND["entry_lookback"]).max().shift(1)
    d["exit_lo"] = d["low"].rolling(TREND["exit_lookback"]).min().shift(1)
    return d


def completed(df: pd.DataFrame) -> pd.DataFrame:
    """Drop the still-forming candle: a 6h bar is complete once now >= open+6h."""
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=6)
    return df[df.index <= cutoff]


def trail_stop(d: pd.DataFrame, entry_time: str, entry: float) -> float:
    """Recompute the chandelier stop deterministically from the entry bar forward
    (same bar-by-bar max logic as the research engine)."""
    t0 = pd.Timestamp(entry_time)
    seg = d[d.index >= t0]
    peak, stop = entry, -np.inf
    for _, r in seg.iterrows():
        if np.isnan(r["atr"]):
            continue
        peak = max(peak, float(r["high"]))
        stop = max(stop, peak - TREND["atr_stop_mult"] * float(r["atr"]))
    return stop


def exit_reason(d: pd.DataFrame, pos: dict) -> str | None:
    """Frozen exit rules evaluated on the last completed bar."""
    r = d.iloc[-1]
    if np.isnan(r["slow"]) or np.isnan(r["atr"]):
        return None
    stop = trail_stop(d, pos["entry_time"], pos["entry"])
    if float(r["low"]) <= stop:
        return "stop"
    if not np.isnan(r["exit_lo"]) and float(r["close"]) < float(r["exit_lo"]):
        return "exit_break"
    if float(r["fast"]) < float(r["slow"]):
        return "regime_flip"
    return None


def entry_signal(d: pd.DataFrame) -> bool:
    """Long entry on the last completed bar only (no chasing stale signals)."""
    r = d.iloc[-1]
    if np.isnan(r["slow"]) or np.isnan(r["atr"]) or np.isnan(r["don_hi"]):
        return False
    return float(r["fast"]) > float(r["slow"]) and float(r["close"]) > float(r["don_hi"])


# ----------------------------- broker ---------------------------------------
class Broker:
    """Thin Alpaca paper wrapper: tagged market orders + fill polling."""

    def __init__(self, cfg):
        from alpaca.trading.client import TradingClient
        self.trading = TradingClient(cfg.alpaca_key, cfg.alpaca_secret, paper=True)
        self.tag = "cx" + datetime.now(timezone.utc).strftime("%Y%m%d%H")

    def position_qty(self, alp_symbol: str) -> float:
        try:
            p = self.trading.get_open_position(alp_symbol.replace("/", ""))
            return float(p.qty)
        except Exception:
            return 0.0

    def market(self, alp_symbol: str, qty: float, side: str):
        """Submit and poll to fill; returns (filled_qty, avg_price) or (0, 0)."""
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        oid = f"{self.tag}-{uuid.uuid4().hex[:8]}"
        o = self.trading.submit_order(MarketOrderRequest(
            symbol=alp_symbol, qty=round(qty, 6),
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.GTC, client_order_id=oid))
        for _ in range(20):
            time.sleep(1)
            o = self.trading.get_order_by_id(o.id)
            status = str(o.status).split(".")[-1].lower()
            if status == "filled":
                return float(o.filled_qty), float(o.filled_avg_price)
            if status in {"canceled", "cancelled", "expired", "rejected"}:
                break
        print(f"[order] {alp_symbol} {side} did not fill cleanly (status {o.status})")
        return float(o.filled_qty or 0), float(o.filled_avg_price or 0)


# ----------------------------- main -----------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Alpaca crypto paper bot (6h trend)")
    ap.add_argument("--ledger", default=LEDGER)
    ap.add_argument("--trades", default=TRADES)
    ap.add_argument("--dry-run", action="store_true",
                    help="evaluate signals but place no orders (no keys needed)")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    led = load_ledger(args.ledger, today)
    trades_log = load_ledger(args.trades, today) if os.path.exists(args.trades) else \
        {"history": []}
    trades_log.setdefault("history", [])

    broker = None
    if not args.dry_run:
        if not CONFIG.has_alpaca:
            raise SystemExit("Set ALPACA_API_KEY and ALPACA_SECRET_KEY first.")
        broker = Broker(CONFIG)

    frames, last_px, notes = {}, {}, []
    for alp, product in SYMBOLS.items():
        try:
            df = completed(prep(get_coinbase(product, interval=INTERVAL,
                                             years=HISTORY_DAYS / 365.0)))
            if len(df) < TREND["trend_slow"] + TREND["entry_lookback"] + 5:
                raise RuntimeError(f"only {len(df)} bars")
            frames[alp] = df
            last_px[alp] = float(df["close"].iloc[-1])
        except Exception as e:                       # noqa: BLE001 — skip a dead feed
            notes.append(f"{alp}: no data ({e})")

    def book_equity():
        return led["cash"] + sum(p["units"] * last_px.get(s, p["entry"])
                                 for s, p in led["positions"].items())

    # daily brake anchor: first run of each UTC day snapshots the book
    anchor = led.get("day_anchor") or {}
    if anchor.get("date") != today:
        anchor = {"date": today, "equity": round(book_equity(), 2)}
        led["day_anchor"] = anchor
    braked = book_equity() <= anchor["equity"] * (1 - DAILY_BRAKE_PCT)

    session_trades, actions = [], []

    for alp, d in frames.items():
        pos = led["positions"].get(alp)
        px = last_px[alp]

        # broker-vs-book drift check (shared account: crypto should be only ours)
        if broker is not None and pos is not None:
            broker_qty = broker.position_qty(alp)
            if broker_qty + 1e-9 < pos["units"]:
                actions.append(f"{alp}: broker holds {broker_qty} < book "
                               f"{pos['units']:.6f} — adopting broker qty")
                pos["units"] = broker_qty
                if broker_qty <= 0:
                    led["positions"].pop(alp, None)
                    pos = None

        if pos is not None:
            reason = exit_reason(d, pos)
            if reason:
                units = pos["units"]
                fill_px = px
                if broker is not None and units > 0:
                    fq, fp = broker.market(alp, units, "sell")
                    if fq > 0:
                        fill_px = fp
                pnl = units * (fill_px - pos["entry"])
                led["cash"] += units * fill_px
                led["positions"].pop(alp, None)
                tr = {"date": today, "symbol": alp, "side": "long",
                      "units": round(units, 6),
                      "entry_time": pos["entry_time"], "entry_price": pos["entry"],
                      "exit_time": now.strftime("%Y-%m-%d %H:%M"),
                      "exit_price": round(fill_px, 2),
                      "pnl": round(pnl, 2), "reason": reason}
                session_trades.append(tr)
                actions.append(f"{alp}: EXIT {reason} @ {fill_px:,.2f} "
                               f"pnl {pnl:+,.2f}")
        elif entry_signal(d) and not braked:
            r = d.iloc[-1]
            dist = TREND["atr_stop_mult"] * float(r["atr"])
            eq = book_equity()
            units = min(eq * RISK_PCT / dist,        # risk sizing
                        led["cash"] / px)            # spot: can't buy beyond cash
            if units * px >= 10:                     # Alpaca min order ~$10 notional
                fill_px = px
                if broker is not None:
                    fq, fp = broker.market(alp, units, "buy")
                    if fq <= 0:
                        continue
                    units, fill_px = fq, fp
                led["cash"] -= units * fill_px
                led["positions"][alp] = {
                    "units": round(units, 6), "entry": round(fill_px, 2),
                    "entry_time": str(d.index[-1]),
                    "signal_price": round(px, 2)}
                actions.append(f"{alp}: ENTER long {units:.6f} @ {fill_px:,.2f} "
                               f"(stop dist {dist:,.0f})")
        elif entry_signal(d) and braked:
            actions.append(f"{alp}: entry signal SKIPPED — daily -2% brake active")

    # --- ledger row for today (upsert) ----------------------------------------
    eq = round(book_equity(), 2)
    day_start = anchor["equity"]
    row = {"date": today,
           "book_start": round(day_start, 2), "book_end": eq,
           "pnl": round(eq - day_start, 2),
           "pnl_pct": round((eq / day_start - 1) * 100, 2) if day_start else 0.0,
           "trades": len([t for t in trades_log["history"] if t["date"] == today])
                     + len(session_trades),
           "open_positions": len(led["positions"]),
           "braked": braked}
    material = bool(session_trades) or not any(r["date"] == today
                                               for r in led["history"])
    led["history"] = upsert_row(led["history"], row)
    led["last_run"] = now.strftime("%Y-%m-%d %H:%M")
    led.setdefault("bankroll", BANKROLL)

    trades_log["history"].extend(session_trades)
    trades_log["last_run"] = led["last_run"]

    # Only rewrite the committed files when something material happened (a trade,
    # or the first run of a new day) — hourly mark-to-market noise lives in the
    # gist, not in git history.
    if material:
        save_json(args.ledger, led)
        save_json(args.trades, trades_log)
        print(f"[ledger] wrote {args.ledger}")
    else:
        # still refresh today's row in memory for the gist below
        print("[ledger] no material change — not rewriting committed files")

    # --- near-live state for the dashboard ------------------------------------
    dep = led["deployment_date"]
    fwd = [r for r in led["history"] if r["date"] >= dep]
    state = {
        "mode": "paper", "symbol": "crypto 6h trend ($10k book)",
        "updated_at": led["last_run"],
        "start_cash": led.get("bankroll", BANKROLL),
        "equity": eq, "pnl": round(eq - led.get("bankroll", BANKROLL), 2),
        "pnl_pct": round((eq / led.get("bankroll", BANKROLL) - 1) * 100, 2),
        "halted": braked, "stopped": "brake" if braked else None,
        "deployment_date": dep, "days_live": len(fwd),
        "positions": [{"symbol": s, **p} for s, p in led["positions"].items()],
        "last_prices": {s: round(p, 2) for s, p in last_px.items()},
        "equity_curve": [[r["date"], r["book_end"]] for r in led["history"]],
        "trades": trades_log["history"][-20:],
        "notes": notes,
    }
    GistPublisher().push("crypto_live.json", state, force=True)

    # --- recap -----------------------------------------------------------------
    md = [f"# Crypto paper bot (6h trend) — {led['last_run']} UTC", "",
          f"**Book:** ${day_start:,.2f} → ${eq:,.2f} today "
          f"({row['pnl']:+,.2f}, {row['pnl_pct']:+.2f}%)"
          + ("  ⛔ daily brake on" if braked else ""),
          f"**Since deployment ({dep}):** ${led.get('bankroll', BANKROLL):,.2f} → "
          f"${eq:,.2f} over {len(fwd)} day(s)",
          f"**Open:** " + (", ".join(
              f"{s} {p['units']:.6f} @ {p['entry']:,.2f}"
              for s, p in led["positions"].items()) or "flat"), ""]
    md += [f"- {a}" for a in actions] or ["- no action this run"]
    if notes:
        md += ["", f"_Data issues: {'; '.join(notes)}_"]
    report = "\n".join(md)
    print("\n" + report)
    sp = os.environ.get("GITHUB_STEP_SUMMARY")
    if sp:
        with open(sp, "a", encoding="utf-8") as f:
            f.write(report + "\n")


if __name__ == "__main__":
    main()
