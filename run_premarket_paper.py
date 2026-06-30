#!/usr/bin/env python3
"""Live PAPER premarket gap-and-go bot on Alpaca.

Each morning it scans the WHOLE market for the top gappers ($1-$10), watches
them from premarket through the open + 2 hours, buys breakouts to new highs
above VWAP, and cuts instantly on weakness. Long only, one position at a time,
flat by ~11:35 ET. Paper account only.

Run on a schedule (see .github/workflows/premarket_paper.yml) or manually:
    python run_premarket_paper.py

Honest caveat: on Alpaca's free IEX feed, premarket prints for small caps are
thin, so live signals may be sparse until the regular session opens. The full
SIP feed (paid) fixes that. Validate forward over several sessions.
"""

import argparse
import json
import os
import sys
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from sim.config import CONFIG
from sim.engine import _parse_hhmm
from sim.state import build_live_state, write_state
from premarket.scanner import scan_live
from premarket.strategy import PMPosition, PremarketMomentum

VOL_LOOKBACK = 5
FALLBACK_WATCHLIST = ["SOFI", "MARA", "RIOT", "PLUG", "NIO", "F", "SOUN", "BBAI"]

# Durable forward record, committed back by the workflow so the track record
# survives the ephemeral runner. NOT under logs/ (which is gitignored). One row
# per trading day; the dashboard state in logs/ stays transient.
LEDGER = os.path.join("paper_ledger", "premarket_paper.json")


def _load_ledger(path: str) -> dict:
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                led = json.load(f)
            led.setdefault("history", [])
            return led
        except (OSError, json.JSONDecodeError):
            pass
    return {"history": []}


def _upsert_row(history: list, row: dict) -> list:
    """Append today's row, replacing any existing row for the same date (re-runs)."""
    history = [r for r in history if r.get("date") != row["date"]]
    history.append(row)
    history.sort(key=lambda r: r["date"])
    return history


class PremarketPaperBot:
    def __init__(self, cfg, state_path="logs/state.json", ledger_path=LEDGER):
        from alpaca.trading.client import TradingClient
        from alpaca.data.live import StockDataStream
        from alpaca.data.enums import DataFeed

        self.cfg = cfg
        self.state_path = state_path
        self.ledger_path = ledger_path
        self.trading = TradingClient(cfg.alpaca_key, cfg.alpaca_secret, paper=True)
        # The live stream needs a DataFeed enum, not the raw string (it reads feed.value).
        self.stream = StockDataStream(cfg.alpaca_key, cfg.alpaca_secret,
                                      feed=DataFeed(cfg.data_feed))
        self.strat = PremarketMomentum(cfg)

        self.start_cash = self._equity()
        self.watch = []                # today's scanned watchlist (for the recap)
        self.bars = {}                 # symbol -> list of bar dicts
        self.cum = {}                  # symbol -> [cum_pv, cum_vol]
        self.session_high = {}         # symbol -> float
        self.pos = None                # single active PMPosition (max 1)
        self.halted = False
        self.equity_curve = []
        self.completed = []

        self.no_new = _parse_hhmm(cfg.pm_no_new_after)
        self.flatten = _parse_hhmm(cfg.pm_flatten_at)

    # --- broker helpers ---------------------------------------------------
    def _equity(self):
        return float(self.trading.get_account().equity)

    def _now_et(self):
        return datetime.now(ZoneInfo(self.cfg.timezone))

    def _buy(self, symbol, shares):
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        self.trading.submit_order(MarketOrderRequest(
            symbol=symbol, qty=int(shares), side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY))
        print(f"[buy ] {int(shares)} {symbol}")

    def _sell(self, symbol, shares):
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        self.trading.submit_order(MarketOrderRequest(
            symbol=symbol, qty=int(shares), side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY))
        print(f"[sell] {int(shares)} {symbol}")

    def _close(self, price, reason, ts):
        pos = self.pos
        self._sell(pos.symbol, pos.shares)
        pnl = pos.shares * (price - pos.entry)
        self.completed.append({"symbol": pos.symbol, "entry_time": str(pos.entry_time),
                               "exit_time": str(ts), "side": "long", "shares": pos.shares,
                               "entry_price": round(pos.entry, 4), "pnl": round(pnl, 2),
                               "exits": [{"reason": reason, "price": round(price, 4),
                                          "qty": pos.shares, "pnl": round(pnl, 2)}]})
        print(f"[exit] {pos.symbol} ({reason}) ~{price:.2f}  pnl {pnl:+.2f}")
        self.pos = None

    # --- per-bar logic ----------------------------------------------------
    async def on_bar(self, bar):
        s = bar.symbol
        ts = pd.Timestamp(bar.timestamp).tz_convert(self.cfg.timezone)
        b = {"o": float(bar.open), "h": float(bar.high), "l": float(bar.low),
             "c": float(bar.close), "v": float(bar.volume), "t": ts}
        self.bars.setdefault(s, []).append(b)
        cum = self.cum.setdefault(s, [0.0, 0.0])
        cum[0] += (b["h"] + b["l"] + b["c"]) / 3.0 * b["v"]; cum[1] += b["v"]
        vwap = cum[0] / cum[1] if cum[1] > 0 else None
        hist = self.bars[s]
        i = len(hist) - 1
        vol_avg = (sum(x["v"] for x in hist[max(0, i - VOL_LOOKBACK):i]) /
                   min(i, VOL_LOOKBACK)) if i >= VOL_LOOKBACK else None
        prev_bar = hist[-2] if len(hist) >= 2 else None
        sh = self.session_high.get(s, -float("inf"))

        # manage the open position (only its own symbol's bars matter)
        if self.pos is not None and self.pos.symbol == s:
            if ts.time() >= self.flatten or self.halted:
                self._close(b["c"], "breaker" if self.halted else "time_stop", ts)
            else:
                reason = self.strat.exit_reason(self.pos, b, prev_bar, vwap)
                if reason == "stop":
                    self._close(self.pos.stop, "stop", ts)
                elif reason:
                    self._close(b["c"], reason, ts)
                elif b["c"] > self.pos.entry and prev_bar is not None:
                    self.pos.stop = max(self.pos.stop, prev_bar["l"])  # trail

        # daily loss cap
        eq = self._equity()
        if not self.halted and eq <= self.start_cash * (1 - self.cfg.daily_max_loss_pct):
            self.halted = True
            if self.pos is not None:
                self._close(b["c"], "breaker", ts)

        # entry (one position; before the cutoff) — bull-flag breakout
        sig_stop = None
        if (self.pos is None and not self.halted and ts.time() <= self.no_new
                and vol_avg is not None):
            window = hist[-(self.cfg.pm_pullback_lookback + 1):]
            sig_stop = self.strat.entry_signal(window, vwap, vol_avg)
        if sig_stop is not None:
            entry = b["c"]
            stop = sig_stop
            rps = entry - stop
            if rps > 0:
                shares = int((eq * self.cfg.risk_per_trade_pct) // rps)
                shares = min(shares, int(eq // entry))
                if shares >= 1:
                    self._buy(s, shares)
                    self.pos = PMPosition(symbol=s, entry=entry, entry_time=ts,
                                          shares=shares, stop=stop, high_water=entry)

        self.session_high[s] = max(sh, b["h"])
        self.equity_curve.append([ts.strftime("%Y-%m-%d %H:%M:%S"), round(eq, 2)])
        self._write_state(ts)

    def _write_state(self, ts):
        p = self.pos
        pos_dict = None if p is None else {"side": "long", "shares": p.shares,
                                           "entry_price": round(p.entry, 4),
                                           "stop": round(p.stop, 4), "half_taken": False}
        write_state(self.state_path, build_live_state(
            mode="paper", symbol="premarket", updated_at=ts.strftime("%Y-%m-%d %H:%M:%S"),
            start_cash=self.start_cash, equity=self._equity(), halted=self.halted,
            position=pos_dict, equity_curve=self.equity_curve, trades=self.completed))

    # --- daily recap ------------------------------------------------------
    def _recap_md(self) -> str:
        end_eq = self._equity()
        pnl = end_eq - self.start_cash
        pct = pnl / self.start_cash * 100 if self.start_cash else 0.0
        wins = [t for t in self.completed if t["pnl"] > 0]
        wr = len(wins) / len(self.completed) * 100 if self.completed else 0.0
        day = self._now_et().strftime("%Y-%m-%d")
        out = [
            f"# Premarket paper bot — {day}", "",
            f"**Equity:** ${self.start_cash:,.2f} → ${end_eq:,.2f}  "
            f"(**{pnl:+,.2f}**, {pct:+.2f}%)"
            + ("  ⛔ HALTED (5% daily cap)" if self.halted else ""),
            f"**Watchlist:** {', '.join(self.watch) if self.watch else '—'}",
            f"**Trades:** {len(self.completed)}  ·  **Win rate:** {wr:.0f}%", "",
        ]
        if self.completed:
            out += ["| Exit | Symbol | Shares | Entry $ | Reason | P&L $ |",
                    "|---|---|--:|--:|---|--:|"]
            for t in self.completed:
                tm = str(t["exit_time"])[11:16]
                reason = t["exits"][0]["reason"] if t.get("exits") else ""
                out.append(f"| {tm} | {t['symbol']} | {int(t['shares'])} | "
                           f"{t['entry_price']:.2f} | {reason} | {t['pnl']:+.2f} |")
        else:
            out.append("_No valid setups triggered today._")
        return "\n".join(out)

    def _write_recap(self):
        md = self._recap_md()
        print("\n" + md)
        path = os.environ.get("GITHUB_STEP_SUMMARY")
        if path:
            try:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(md + "\n")
            except OSError:
                pass

    def _write_ledger(self):
        """Append today's result to the durable, committed ledger (one row/day)."""
        end_eq = self._equity()
        pnl = end_eq - self.start_cash
        wins = [t for t in self.completed if t["pnl"] > 0]
        wr = len(wins) / len(self.completed) * 100 if self.completed else 0.0
        row = {
            "date": self._now_et().strftime("%Y-%m-%d"),
            "start_equity": round(self.start_cash, 2),
            "end_equity": round(end_eq, 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl / self.start_cash * 100, 2) if self.start_cash else 0.0,
            "trades": len(self.completed),
            "win_rate": round(wr, 1),
            "halted": self.halted,
            "watchlist": self.watch,
        }
        try:
            led = _load_ledger(self.ledger_path)
            led["history"] = _upsert_row(led["history"], row)
            led["last_run"] = row["date"]
            os.makedirs(os.path.dirname(self.ledger_path) or ".", exist_ok=True)
            tmp = self.ledger_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(led, f, indent=2)
            os.replace(tmp, self.ledger_path)
            print(f"[ledger] wrote {self.ledger_path} ({len(led['history'])} day(s))")
        except OSError as e:
            print(f"[ledger] failed to write ledger: {e}")

    # --- session control --------------------------------------------------
    def _end_session(self):
        try:
            self.trading.close_all_positions(cancel_orders=True)
            print("[session] flattened all positions, closing.")
        except Exception as e:
            print(f"[session] close_all_positions failed: {e}")
        self._write_ledger()
        self._write_recap()
        # os._exit() skips stdout flushing; on a CI pipe stdout is block-buffered,
        # so the recap + trade prints would be discarded. Flush first so the run
        # log is legible, then hard-exit to kill the still-running websocket thread.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)

    def run(self):
        try:
            watch = scan_live(self.cfg)
        except Exception as e:
            print(f"[scan] screener unavailable ({e}); using fallback watchlist")
            watch = FALLBACK_WATCHLIST
        if not watch:
            print("[scan] no gappers found; using fallback watchlist")
            watch = FALLBACK_WATCHLIST
        self.watch = watch
        print(f"Premarket paper bot  equity=${self.start_cash:,.2f}  watching {watch}")

        for s in watch:
            self.stream.subscribe_bars(self.on_bar, s)

        # Stop the session at the flatten time.
        now = self._now_et()
        flat_dt = now.replace(hour=self.flatten.hour, minute=self.flatten.minute,
                              second=0, microsecond=0)
        secs = max(30, (flat_dt - now).total_seconds())
        threading.Timer(secs, self._end_session).start()
        print(f"[session] will flatten and exit in {int(secs/60)} min")
        self.stream.run()


def main():
    p = argparse.ArgumentParser(description="Live Alpaca premarket paper bot")
    p.add_argument("--state", default=os.path.join("logs", "state.json"))
    p.add_argument("--ledger", default=LEDGER)
    args = p.parse_args()
    if not CONFIG.has_alpaca:
        raise SystemExit("Set ALPACA_API_KEY and ALPACA_SECRET_KEY first.")

    now_et = datetime.now(ZoneInfo(CONFIG.timezone))
    if now_et.time() > _parse_hhmm(CONFIG.pm_flatten_at):
        msg = (f"# Premarket paper bot — {now_et:%Y-%m-%d}\n\n"
               f"_Started {now_et:%H:%M} ET, past the {CONFIG.pm_flatten_at} window — "
               "no session today (the schedule runs premarket on weekdays)._")
        print(msg)
        path = os.environ.get("GITHUB_STEP_SUMMARY")
        if path:
            with open(path, "a", encoding="utf-8") as f:
                f.write(msg + "\n")
        return
    PremarketPaperBot(CONFIG, state_path=args.state, ledger_path=args.ledger).run()


if __name__ == "__main__":
    main()
