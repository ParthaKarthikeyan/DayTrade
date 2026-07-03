#!/usr/bin/env python3
"""Live PAPER premarket gap-and-go bot on Alpaca — trading a notional $10k book.

Each morning it scans the WHOLE market for the top gappers ($1-$10), watches
them from premarket through the open + 2 hours, buys bull-flag breakouts above
VWAP, and cuts instantly on weakness. Long only, one position at a time,
flat by ~11:35 ET. Paper account only.

The account is shared by several experiments, so the bot does NOT trade the
account balance. It runs a $10k book (pm_bankroll) that compounds with its own
committed ledger P&L; every size, cap and result references the book.

The day is shaped around the $100/day goal by a governor:
  - bank the day (flatten + stop) at +pm_daily_target_pct of the book;
  - hard stop at -pm_daily_max_loss_pct;
  - at most pm_max_trades_per_day round-trips, with a cooldown after
    pm_loss_streak_pause consecutive losers.

Execution honesty on thin gappers: every order is a marketable LIMIT tagged
with a client_order_id prefix (so fills attribute to this bot on the shared
account), never a market order. Unfilled entries are cancelled, not chased;
unfilled exits are re-submitted deeper until they fill.

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
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from sim.config import CONFIG
from sim.engine import _parse_hhmm
from sim.state import build_live_state, write_state
from sim.gist import GistPublisher
from premarket.scanner import scan_live, is_common_stock
from premarket.strategy import PMPosition, PremarketMomentum

VOL_LOOKBACK = 5
FALLBACK_WATCHLIST = ["SOFI", "MARA", "RIOT", "PLUG", "NIO", "F", "SOUN", "BBAI"]

# Durable forward record, committed back by the workflow so the track record
# survives the ephemeral runner. NOT under logs/ (which is gitignored). One row
# per trading day; the dashboard state in logs/ stays transient.
LEDGER = os.path.join("paper_ledger", "premarket_paper.json")
# Per-trade log: every completed trade, grouped by session date and accumulated
# across days (this bot trades live, so unlike the futures replay it appends).
TRADES = os.path.join("paper_ledger", "premarket_trades.json")

# Order states that end an order's life (nothing more will fill).
TERMINAL = {"filled", "canceled", "cancelled", "expired", "rejected", "done_for_day"}


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


def book_seed(ledger_path: str, bankroll: float) -> float:
    """The book compounds across sessions: seed today from the last committed
    book_end; fall back to the configured bankroll on the first run (or for
    ledger rows written before the book existed)."""
    led = _load_ledger(ledger_path)
    for row in reversed(led["history"]):
        if row.get("book_end") is not None:
            return float(row["book_end"])
    return bankroll


def pair_fills_fifo(fills: list):
    """Pair raw broker fills into long-only round-trip trades using FIFO per symbol,
    so entry/exit prices and P&L reflect ACTUAL executions (incl. slippage), not the
    bot's intended prices. Each fill: {time, symbol, side ('buy'/'sell'), qty, price}.
    Returns (trades, realized_pnl). Buys open lots; sells close them oldest-first."""
    from collections import defaultdict, deque
    lots = defaultdict(deque)          # symbol -> deque of [qty, price, time]
    trades = []
    for f in sorted(fills, key=lambda x: x["time"]):
        sym, qty, px = f["symbol"], float(f["qty"]), float(f["price"])
        if "buy" in f["side"].lower():
            lots[sym].append([qty, px, f["time"]])
            continue
        remaining, cost, matched, entry_time = qty, 0.0, 0.0, None
        while remaining > 1e-9 and lots[sym]:
            lot = lots[sym][0]
            take = min(remaining, lot[0])
            if entry_time is None:
                entry_time = lot[2]
            cost += take * lot[1]; matched += take
            lot[0] -= take; remaining -= take
            if lot[0] <= 1e-9:
                lots[sym].popleft()
        if matched > 1e-9:
            avg_entry = cost / matched
            sh = int(matched) if abs(matched - round(matched)) < 1e-9 else round(matched, 4)
            trades.append({"symbol": sym, "side": "long", "shares": sh,
                           "entry_time": entry_time, "entry_price": round(avg_entry, 4),
                           "exit_time": f["time"], "exit_price": round(px, 4),
                           "pnl": round(matched * (px - avg_entry), 2)})
    return trades, round(sum(t["pnl"] for t in trades), 2)


class PremarketPaperBot:
    def __init__(self, cfg, state_path="logs/state.json", ledger_path=LEDGER,
                 trades_path=TRADES):
        from alpaca.trading.client import TradingClient
        from alpaca.data.live import StockDataStream
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.enums import DataFeed

        self.cfg = cfg
        self.state_path = state_path
        self.ledger_path = ledger_path
        self.trades_path = trades_path
        self.trading = TradingClient(cfg.alpaca_key, cfg.alpaca_secret, paper=True)
        # The live stream needs a DataFeed enum, not the raw string (it reads feed.value).
        self.stream = StockDataStream(cfg.alpaca_key, cfg.alpaca_secret,
                                      feed=DataFeed(cfg.data_feed))
        self.quotes = StockHistoricalDataClient(cfg.alpaca_key, cfg.alpaca_secret)
        self.strat = PremarketMomentum(cfg)
        self.gist = GistPublisher()

        # The $10k book: compounds session to session via the committed ledger.
        self.book_start = book_seed(ledger_path, cfg.pm_bankroll)
        self.acct_start = self._account_equity()   # reference only, never for sizing
        # Every order this bot places today carries this prefix so its fills can
        # be attributed on the shared paper account.
        self.tag = "pm" + datetime.now(ZoneInfo(cfg.timezone)).strftime("%Y%m%d")
        self._order_seq = 0

        self.watch = []                # today's scanned watchlist (for the recap)
        self.bars = {}                 # symbol -> list of bar dicts
        self.cum = {}                  # symbol -> [cum_pv, cum_vol]
        self.session_high = {}         # symbol -> float
        self.pos = None                # single active PMPosition (max 1)
        self.pending_entry = None      # working entry order (state-machine, polled per bar)
        self.pending_exit = None       # working exit order
        self.stopped = None            # None | 'target' | 'loss_cap' — governor verdict
        self.trades_taken = 0          # round-trips opened today
        self.loss_streak = 0
        self.cooldown_until = None     # ET datetime — no entries until then
        self.last_close = {}           # symbol -> last close (marks the open position)
        self.equity_curve = []         # book equity over the session
        self.completed = []            # today's round-trips (actual fills where known)
        self.fill_trades = []          # round-trips from broker fills (end-of-day recon)
        self.fills_realized = 0.0

        self.no_new = _parse_hhmm(cfg.pm_no_new_after)
        self.flatten = _parse_hhmm(cfg.pm_flatten_at)
        self._session_open = datetime.now(ZoneInfo(cfg.timezone))

    # --- broker helpers ---------------------------------------------------
    def _account_equity(self):
        try:
            return float(self.trading.get_account().equity)
        except Exception:
            return 0.0

    def _now_et(self):
        return datetime.now(ZoneInfo(self.cfg.timezone))

    def _next_order_id(self) -> str:
        self._order_seq += 1
        return f"{self.tag}-{self._order_seq:02d}-{uuid.uuid4().hex[:6]}"

    def _submit_limit(self, symbol, shares, side, limit_price):
        """Marketable LIMIT tagged with the bot's prefix. extended_hours=True makes
        it eligible premarket (where market orders would just queue for the open)."""
        from alpaca.trading.requests import LimitOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        oid = self._next_order_id()
        o = self.trading.submit_order(LimitOrderRequest(
            symbol=symbol, qty=int(shares),
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY, limit_price=round(float(limit_price), 2),
            extended_hours=True, client_order_id=oid))
        print(f"[{side:4s}] {int(shares)} {symbol} lmt {float(limit_price):.2f} ({oid})")
        return o

    def _poll(self, order_id):
        try:
            return self.trading.get_order_by_id(order_id)
        except Exception as e:
            print(f"[poll] order {order_id} unreadable: {e}")
            return None

    def _cancel(self, order_id):
        try:
            self.trading.cancel_order_by_id(order_id)
        except Exception:
            pass                        # already terminal / racing a fill — poll decides

    def _spread_ok(self, symbol) -> bool:
        """Skip entries when the quoted spread is wider than the cap (thin book)."""
        try:
            from alpaca.data.requests import StockLatestQuoteRequest
            from alpaca.data.enums import DataFeed
            q = self.quotes.get_stock_latest_quote(StockLatestQuoteRequest(
                symbol_or_symbols=symbol, feed=DataFeed(self.cfg.data_feed)))[symbol]
            bid, ask = float(q.bid_price or 0), float(q.ask_price or 0)
            if bid <= 0 or ask <= 0 or ask < bid:
                return False            # no two-sided quote -> don't trade it
            mid = (bid + ask) / 2.0
            return (ask - bid) / mid <= self.cfg.pm_max_spread_pct
        except Exception as e:
            print(f"[quote] {symbol} unavailable ({e}); skipping entry")
            return False

    # --- book accounting ----------------------------------------------------
    def _unrealized(self):
        if self.pos is None:
            return 0.0
        last = self.last_close.get(self.pos.symbol, self.pos.entry)
        return self.pos.shares * (last - self.pos.entry)

    def _book_realized(self):
        return sum(t["pnl"] for t in self.completed)

    def _book_equity(self):
        return self.book_start + self._book_realized() + self._unrealized()

    # --- trade lifecycle (pending-order state machine, advanced per bar) ---
    def _try_enter(self, symbol, signal_price, stop, ts):
        # enforce a minimum stop distance so sizing can't go near-all-in
        stop = min(stop, signal_price * (1.0 - self.cfg.pm_min_stop_pct))
        limit = signal_price * (1.0 + self.cfg.pm_limit_slip_pct)
        rps = limit - stop
        book = self._book_equity()
        shares = int((book * self.cfg.risk_per_trade_pct) // rps)
        shares = min(shares, int(book * self.cfg.pm_max_position_pct // limit))
        if shares < 1:
            return
        try:
            o = self._submit_limit(symbol, shares, "buy", limit)
        except Exception as e:
            print(f"[buy ] {symbol} rejected: {e}")
            return
        self.trades_taken += 1
        self.pending_entry = {"id": o.id, "symbol": symbol, "stop": stop,
                              "signal": signal_price, "submitted": ts}

    def _advance_entry(self, ts):
        pe = self.pending_entry
        o = self._poll(pe["id"])
        if o is None:
            return
        status = str(getattr(o, "status", "")).split(".")[-1].lower()
        filled = float(o.filled_qty or 0)
        if status == "filled" or (status in TERMINAL and filled >= 1):
            px = float(o.filled_avg_price or pe["signal"])
            self.pos = PMPosition(symbol=pe["symbol"], entry=px, entry_time=ts,
                                  shares=int(filled), stop=pe["stop"], high_water=px)
            print(f"[fill] entry {int(filled)} {pe['symbol']} @ {px:.2f}")
            self.pending_entry = None
        elif status in TERMINAL:
            print(f"[entry] {pe['symbol']} {status} unfilled — not chasing")
            self.pending_entry = None
        elif (ts - pe["submitted"]).total_seconds() >= self.cfg.pm_fill_wait_s:
            self._cancel(pe["id"])     # poll again next bar; a racing fill is adopted

    def _start_exit(self, price, reason, ts):
        """Submit a limit sell just below the trigger so it's marketable but bounded."""
        limit = price * (1.0 - self.cfg.pm_exit_slip_pct)
        try:
            o = self._submit_limit(self.pos.symbol, self.pos.shares, "sell", limit)
        except Exception as e:
            print(f"[sell] {self.pos.symbol} rejected: {e}")
            return
        self.pending_exit = {"id": o.id, "reason": reason, "trigger": price,
                             "submitted": ts, "attempts": 1}

    def _record_close(self, shares, exit_px, reason, ts):
        pos = self.pos
        pnl = shares * (exit_px - pos.entry)
        self.completed.append({"symbol": pos.symbol, "entry_time": str(pos.entry_time),
                               "exit_time": str(ts), "side": "long", "shares": int(shares),
                               "entry_price": round(pos.entry, 4),
                               "exit_price": round(exit_px, 4), "pnl": round(pnl, 2),
                               "exits": [{"reason": reason, "price": round(exit_px, 4),
                                          "qty": int(shares), "pnl": round(pnl, 2)}]})
        print(f"[exit] {pos.symbol} ({reason}) @ {exit_px:.2f}  pnl {pnl:+.2f}")
        if pnl < 0:
            self.loss_streak += 1
            if self.loss_streak >= self.cfg.pm_loss_streak_pause:
                self.cooldown_until = ts + pd.Timedelta(minutes=self.cfg.pm_cooldown_min)
                print(f"[cool] {self.loss_streak} losers in a row — no entries until "
                      f"{self.cooldown_until:%H:%M} ET")
        else:
            self.loss_streak = 0

    def _advance_exit(self, bar, ts):
        px = self.pending_exit
        o = self._poll(px["id"])
        if o is None:
            return
        status = str(getattr(o, "status", "")).split(".")[-1].lower()
        filled = float(o.filled_qty or 0)
        if status == "filled":
            self._record_close(filled, float(o.filled_avg_price or px["trigger"]),
                               px["reason"], ts)
            self.pos, self.pending_exit = None, None
        elif status in TERMINAL:
            if filled >= 1:            # partial fill before the cancel landed
                self._record_close(filled, float(o.filled_avg_price or px["trigger"]),
                                   px["reason"], ts)
                self.pos.shares -= int(filled)
            if self.pos and self.pos.shares >= 1:
                # escalate: re-price off the latest close, deeper each attempt
                deeper = min(px["trigger"], bar["c"]) * \
                    (1.0 - self.cfg.pm_exit_slip_pct * (px["attempts"] + 1))
                try:
                    o2 = self._submit_limit(self.pos.symbol, self.pos.shares, "sell", deeper)
                    self.pending_exit = {"id": o2.id, "reason": px["reason"],
                                         "trigger": px["trigger"], "submitted": ts,
                                         "attempts": px["attempts"] + 1}
                except Exception as e:
                    print(f"[sell] resubmit failed: {e}")
                    self.pending_exit = None
            else:
                self.pos, self.pending_exit = None, None
        elif (ts - px["submitted"]).total_seconds() >= self.cfg.pm_fill_wait_s:
            self._cancel(px["id"])

    # --- per-bar logic ----------------------------------------------------
    async def on_bar(self, bar):
        s = bar.symbol
        ts = pd.Timestamp(bar.timestamp).tz_convert(self.cfg.timezone)
        b = {"o": float(bar.open), "h": float(bar.high), "l": float(bar.low),
             "c": float(bar.close), "v": float(bar.volume), "t": ts}
        self.bars.setdefault(s, []).append(b)
        self.last_close[s] = b["c"]
        cum = self.cum.setdefault(s, [0.0, 0.0])
        cum[0] += (b["h"] + b["l"] + b["c"]) / 3.0 * b["v"]; cum[1] += b["v"]
        vwap = cum[0] / cum[1] if cum[1] > 0 else None
        hist = self.bars[s]
        i = len(hist) - 1
        vol_avg = (sum(x["v"] for x in hist[max(0, i - VOL_LOOKBACK):i]) /
                   min(i, VOL_LOOKBACK)) if i >= VOL_LOOKBACK else None
        prev_bar = hist[-2] if len(hist) >= 2 else None
        sh = self.session_high.get(s, -float("inf"))

        # advance any working orders for this symbol first
        if self.pending_entry is not None and self.pending_entry["symbol"] == s:
            self._advance_entry(ts)
        if (self.pos is not None and self.pos.symbol == s
                and self.pending_exit is not None):
            self._advance_exit(b, ts)

        # manage the open position (only its own symbol's bars matter)
        if self.pos is not None and self.pos.symbol == s and self.pending_exit is None:
            if ts.time() >= self.flatten or self.stopped:
                self._start_exit(b["c"], self.stopped or "time_stop", ts)
            else:
                reason = self.strat.exit_reason(self.pos, b, prev_bar, vwap)
                if reason == "stop":
                    self._start_exit(min(self.pos.stop, b["c"]), "stop", ts)
                elif reason:
                    self._start_exit(b["c"], reason, ts)
                elif b["c"] > self.pos.entry and prev_bar is not None:
                    self.pos.stop = max(self.pos.stop, prev_bar["l"])  # trail

        # daily governor on the BOOK: bank the target, cap the loss
        book = self._book_equity()
        if self.stopped is None:
            if book >= self.book_start * (1 + self.cfg.pm_daily_target_pct):
                self.stopped = "target"
                print(f"[bank] book +{(book / self.book_start - 1) * 100:.2f}% — "
                      "target reached, done for the day")
            elif book <= self.book_start * (1 - self.cfg.pm_daily_max_loss_pct):
                self.stopped = "loss_cap"
                print(f"[halt] book {(book / self.book_start - 1) * 100:.2f}% — "
                      "daily loss cap hit")
            if self.stopped and self.pos is not None and self.pending_exit is None:
                self._start_exit(b["c"], self.stopped, ts)

        # entry (one position; before the cutoff) — bull-flag breakout
        can_enter = (self.pos is None and self.pending_entry is None
                     and self.stopped is None and ts.time() <= self.no_new
                     and self.trades_taken < self.cfg.pm_max_trades_per_day
                     and (self.cooldown_until is None or ts >= self.cooldown_until)
                     and vol_avg is not None)
        if can_enter:
            window = hist[-(self.cfg.pm_pullback_lookback + 1):]
            sig_stop = self.strat.entry_signal(window, vwap, vol_avg)
            if sig_stop is not None:
                # liquidity gates: recent $ volume and a sane quoted spread
                recent = hist[max(0, i - VOL_LOOKBACK):i + 1]
                dollar_vol = sum(x["c"] * x["v"] for x in recent) / len(recent)
                if dollar_vol < self.cfg.pm_min_bar_dollar_vol:
                    print(f"[skip] {s} avg ${dollar_vol:,.0f}/bar — too thin")
                elif self._spread_ok(s):
                    self._try_enter(s, b["c"], sig_stop, ts)
                else:
                    print(f"[skip] {s} spread too wide")

        self.session_high[s] = max(sh, b["h"])
        self.equity_curve.append([ts.strftime("%Y-%m-%d %H:%M:%S"),
                                  round(self._book_equity(), 2)])
        self._write_state(ts)

    def _state_dict(self, ts):
        p = self.pos
        pos_dict = None if p is None else {"side": "long", "shares": p.shares,
                                           "entry_price": round(p.entry, 4),
                                           "stop": round(p.stop, 4), "half_taken": False}
        st = build_live_state(
            mode="paper", symbol="premarket ($10k book)",
            updated_at=ts.strftime("%Y-%m-%d %H:%M:%S"),
            start_cash=self.book_start, equity=self._book_equity(),
            halted=self.stopped == "loss_cap",
            position=pos_dict, equity_curve=self.equity_curve, trades=self.completed)
        st["stopped"] = self.stopped
        st["trades_taken"] = self.trades_taken
        st["watchlist"] = self.watch
        return st

    def _write_state(self, ts):
        st = self._state_dict(ts)
        write_state(self.state_path, st)
        # near-live view for the hosted dashboard (throttled inside the publisher)
        self.gist.push("premarket_live.json", st)

    # --- daily recap ------------------------------------------------------
    def _recap_md(self) -> str:
        book_end = self.book_start + self._book_realized()
        pnl = book_end - self.book_start
        pct = pnl / self.book_start * 100 if self.book_start else 0.0
        acct_pnl = self._account_equity() - self.acct_start
        trades = self.fill_trades
        wins = [t for t in trades if t["pnl"] > 0]
        wr = len(wins) / len(trades) * 100 if trades else 0.0
        day = self._now_et().strftime("%Y-%m-%d")
        badge = {"target": "  🎯 TARGET BANKED", "loss_cap": "  ⛔ LOSS CAP HIT"}.get(
            self.stopped, "")
        gap = self._book_realized() - self.fills_realized
        out = [
            f"# Premarket paper bot ($10k book) — {day}", "",
            f"**Book:** ${self.book_start:,.2f} → ${book_end:,.2f}  "
            f"(**{pnl:+,.2f}**, {pct:+.2f}%){badge}",
            f"**Watchlist:** {', '.join(self.watch) if self.watch else '—'}",
            f"**Trades:** {len(trades)}  ·  **Win rate:** {wr:.0f}%", "",
            "**Reconciliation** (bot's book vs the broker's fills):",
            f"- Book realized P&L: **{self._book_realized():+,.2f}**",
            f"- Realized from actual broker fills: {self.fills_realized:+,.2f}",
            f"- Gap (should be ~0 now that fills price the book): {gap:+,.2f}",
            f"- Whole-account move (shared, for reference only): {acct_pnl:+,.2f}", "",
        ]
        if trades:
            out += ["| Exit | Symbol | Shares | Entry $ | Exit $ | P&L $ |",
                    "|---|---|--:|--:|--:|--:|"]
            for t in trades:
                ex = f"{t['exit_price']:.2f}" if t.get("exit_price") is not None else "—"
                out.append(f"| {str(t['exit_time'])[:5]} | {t['symbol']} | "
                           f"{int(t['shares'])} | {t['entry_price']:.2f} | {ex} | "
                           f"{t['pnl']:+.2f} |")
        else:
            out.append("_No fills today._")
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
        book_end = self.book_start + self._book_realized()
        pnl = book_end - self.book_start
        wins = [t for t in self.completed if t["pnl"] > 0]
        wr = len(wins) / len(self.completed) * 100 if self.completed else 0.0
        row = {
            "date": self._now_et().strftime("%Y-%m-%d"),
            # the $10k book — this is what the goal is judged on
            "book_start": round(self.book_start, 2),
            "book_end": round(book_end, 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl / self.book_start * 100, 2) if self.book_start else 0.0,
            "trades": len(self.completed),
            "win_rate": round(wr, 1),
            "stopped": self.stopped,          # 'target' | 'loss_cap' | None
            "halted": self.stopped == "loss_cap",
            "watchlist": self.watch,
            # whole shared account, for reference/debugging only
            "account_pnl": round(self._account_equity() - self.acct_start, 2),
        }
        try:
            led = _load_ledger(self.ledger_path)
            led["history"] = _upsert_row(led["history"], row)
            led["last_run"] = row["date"]
            led["bankroll"] = self.cfg.pm_bankroll
            os.makedirs(os.path.dirname(self.ledger_path) or ".", exist_ok=True)
            tmp = self.ledger_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(led, f, indent=2)
            os.replace(tmp, self.ledger_path)
            print(f"[ledger] wrote {self.ledger_path} ({len(led['history'])} day(s))")
        except OSError as e:
            print(f"[ledger] failed to write ledger: {e}")

    def _fetch_fills(self, day: str) -> list:
        """Reconstruct today's ACTUAL executions from this bot's own orders (matched by
        the client_order_id prefix), so the record reflects real fill prices even on a
        shared account. Uses get_orders — the activities endpoint isn't exposed by the
        installed alpaca-py."""
        fills = []
        try:
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus
            req = GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=500,
                                   after=self._session_open)
            for o in self.trading.get_orders(req):
                coid = str(getattr(o, "client_order_id", "") or "")
                if not coid.startswith(self.tag):
                    continue
                qty = float(o.filled_qty or 0)
                if qty <= 0:
                    continue
                t = str(getattr(o, "filled_at", "") or "")
                fills.append({
                    "time": (t[11:19] if len(t) >= 19 else t), "symbol": o.symbol,
                    "side": str(o.side).split(".")[-1].lower(), "qty": qty,
                    "price": float(o.filled_avg_price or 0),
                })
        except Exception as e:                     # noqa: BLE001 — fall back to the mirror
            print(f"[fills] could not fetch Alpaca orders ({e}); using local mirror")
        return sorted(fills, key=lambda f: f["time"])

    def _write_trades(self):
        """Append today's trades to the durable per-trade log using ACTUAL fills from
        this bot's tagged orders, and record how they reconcile against the book."""
        day = self._now_et().strftime("%Y-%m-%d")
        fills = self._fetch_fills(day)
        if fills:
            self.fill_trades, self.fills_realized = pair_fills_fifo(fills)
            source, trades = "alpaca_fills", self.fill_trades
        else:
            source = "local_mirror"
            trades = [{"symbol": t["symbol"], "side": t.get("side", "long"),
                       "shares": t["shares"], "entry_time": str(t["entry_time"])[11:19],
                       "entry_price": t["entry_price"], "exit_time": str(t["exit_time"])[11:19],
                       "exit_price": t.get("exit_price"), "pnl": t["pnl"]}
                      for t in self.completed]
            self.fill_trades = trades
            self.fills_realized = round(sum(t["pnl"] for t in trades), 2)

        for t in trades:
            t["date"] = day
        book_pnl = round(self._book_realized(), 2)
        block = {
            "date": day, "source": source,
            "book_pnl": book_pnl,                       # what the $10k book recorded
            "fills_realized_pnl": self.fills_realized,  # from real tagged fills
            "reconciliation_gap": round(book_pnl - self.fills_realized, 2),
            "account_pnl": round(self._account_equity() - self.acct_start, 2),
            "stopped": self.stopped,
            "fill_count": len(fills), "trade_count": len(trades),
            "trades": trades, "fills": fills,
        }
        try:
            log = _load_ledger(self.trades_path)
            log["history"] = _upsert_row(log["history"], block)
            log["last_run"] = day
            os.makedirs(os.path.dirname(self.trades_path) or ".", exist_ok=True)
            tmp = self.trades_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(log, f, indent=2)
            os.replace(tmp, self.trades_path)
            print(f"[trades] wrote {self.trades_path} — {len(trades)} trade(s) from "
                  f"{source}; fills-realized ${self.fills_realized:,.2f} vs book "
                  f"${book_pnl:,.2f}")
        except OSError as e:
            print(f"[trades] failed to write trade log: {e}")

    # --- session control --------------------------------------------------
    def _flatten_own(self):
        """Close ONLY this bot's position/orders — the paper account is shared, so
        close_all_positions would clobber other experiments' books."""
        for pend in (self.pending_entry, self.pending_exit):
            if pend is not None:
                self._cancel(pend["id"])
        if self.pos is not None:
            try:
                # 11:35 ET is regular hours; a tagged market-equivalent close is fine.
                o = self.trading.close_position(self.pos.symbol)
                px = float(getattr(o, "filled_avg_price", 0) or 0) or \
                    self.last_close.get(self.pos.symbol, self.pos.entry)
                self._record_close(self.pos.shares, px, "time_stop", self._now_et())
                self.pos = None
                print("[session] flattened the bot's position.")
            except Exception as e:
                print(f"[session] close_position failed: {e}")

    def _end_session(self):
        self._flatten_own()
        time.sleep(3)                  # let the final close settle before reconciling
        self._write_trades()           # fills first: the recap and gist show real prices
        self._write_ledger()
        self._write_recap()
        try:
            self.gist.push("premarket_live.json", self._state_dict(self._now_et()),
                           force=True)
        except Exception:
            pass
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
        print(f"Premarket paper bot  book=${self.book_start:,.2f} "
              f"(account ${self.acct_start:,.2f} shared)  watching {watch}")

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
    p.add_argument("--trades", default=TRADES)
    args = p.parse_args()
    if not CONFIG.has_alpaca:
        raise SystemExit("Set ALPACA_API_KEY and ALPACA_SECRET_KEY first.")

    tz = ZoneInfo(CONFIG.timezone)
    now_et = datetime.now(tz)
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

    # GitHub's scheduled runners boot at an unpredictable delay (minutes to hours).
    # Decouple boot time from trading start: if the runner came up before the
    # premarket session start, idle until then so the watchlist scan and entries
    # begin at a consistent ET time regardless of when the cron actually fired.
    start_t = _parse_hhmm(CONFIG.pm_session_start)
    if now_et.time() < start_t:
        target = now_et.replace(hour=start_t.hour, minute=start_t.minute,
                                second=0, microsecond=0)
        wait_s = (target - now_et).total_seconds()
        print(f"[wait] runner up at {now_et:%H:%M} ET; holding "
              f"{int(wait_s // 60)} min until session start {CONFIG.pm_session_start} ET")
        sys.stdout.flush()
        time.sleep(wait_s)

    PremarketPaperBot(CONFIG, state_path=args.state, ledger_path=args.ledger,
                      trades_path=args.trades).run()


if __name__ == "__main__":
    main()
