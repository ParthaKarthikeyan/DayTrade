#!/usr/bin/env python3
"""Live PAPER trading loop on Alpaca, using the same ORB strategy + risk engine.

Subscribes to 1-minute bars, runs the identical entry logic and scale-out/stop
management as the backtester, and submits (paper) market orders to realise each
fill. Requires ALPACA_API_KEY / ALPACA_SECRET_KEY in .env (paper keys).

    python run_paper.py --symbols AAPL,MSFT

Notes / limitations:
- Runs only during US regular hours; outside them the bar stream is idle.
- Scale-out and trailing stops are managed client-side (one market order per
  fill) rather than via native bracket orders, so behaviour matches the sim.
- This is paper trading. Review and test before ever pointing at a live account.
"""

import argparse

from sim.config import CONFIG
from sim.indicators import add_indicators
from sim.portfolio import LONG, SHORT, Position
from sim.risk import RiskManager
from sim.strategy import ENTER_LONG, ENTER_SHORT, ORBStrategy, OpeningRange
from sim.engine import _parse_hhmm

import pandas as pd


class PaperTrader:
    def __init__(self, cfg, symbols):
        from alpaca.trading.client import TradingClient
        from alpaca.data.live import StockDataStream

        self.cfg = cfg
        self.symbols = symbols
        self.trading = TradingClient(cfg.alpaca_key, cfg.alpaca_secret, paper=True)
        self.stream = StockDataStream(cfg.alpaca_key, cfg.alpaca_secret)
        self.strategy = ORBStrategy(cfg)
        self.risk = RiskManager(cfg)
        self.risk.start_day(self._equity())

        self.bars = {s: [] for s in symbols}          # list of dict rows
        self.orange = {s: None for s in symbols}      # OpeningRange or None
        self.pos = {s: None for s in symbols}         # local Position mirror

        self.open_t = _parse_hhmm(cfg.market_open)
        self.no_new_t = _parse_hhmm(cfg.no_new_trades_after)
        self.flatten_t = _parse_hhmm(cfg.flatten_at)

    # --- broker helpers ---------------------------------------------------
    def _equity(self) -> float:
        return float(self.trading.get_account().equity)

    def _submit(self, symbol, qty, side):
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        order = MarketOrderRequest(
            symbol=symbol, qty=int(qty), time_in_force=TimeInForce.DAY,
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL)
        self.trading.submit_order(order)
        print(f"[order] {side} {int(qty)} {symbol}")

    def _close_all(self, symbol, price, reason):
        pos = self.pos[symbol]
        if not pos:
            return
        self._submit(symbol, pos.shares, "sell" if pos.is_long else "buy")
        print(f"[exit ] {symbol} all ({reason}) @~{price:.2f}")
        self.pos[symbol] = None

    # --- per-bar logic (mirrors SimEngine.run) ----------------------------
    async def on_bar(self, bar):
        s = bar.symbol
        ts = pd.Timestamp(bar.timestamp).tz_convert(self.cfg.timezone)
        self.bars[s].append({"open": bar.open, "high": bar.high, "low": bar.low,
                             "close": bar.close, "volume": bar.volume,
                             "timestamp": ts})
        df = pd.DataFrame(self.bars[s]).set_index("timestamp")
        df = add_indicators(df, self.cfg.ema_fast, self.cfg.ema_slow)
        row = df.iloc[-1]
        d = {k: float(row[k]) for k in
             ("open", "high", "low", "close", "ema_fast", "ema_slow", "vwap")}
        close = d["close"]
        minute = ts.hour * 60 + ts.minute
        open_min = self.open_t.hour * 60 + self.open_t.minute

        # Establish opening range once the window has elapsed.
        if self.orange[s] is None and minute >= open_min + self.cfg.or_minutes:
            window = df[df.index.map(
                lambda t: open_min <= t.hour * 60 + t.minute < open_min + self.cfg.or_minutes)]
            if not window.empty:
                self.orange[s] = OpeningRange(float(window["high"].max()),
                                              float(window["low"].min()))

        # 1) Manage / force-close existing position.
        if self.pos[s] is not None:
            if ts.time() >= self.flatten_t or self.risk.halted:
                self._close_all(s, close, "breaker" if self.risk.halted else "time_stop")
            else:
                for f in self.risk.manage(self.pos[s], d):
                    self._submit(s, f["qty"], "sell" if self.pos[s].is_long else "buy")
                    self.pos[s].shares -= f["qty"]
                    print(f"[exit ] {s} {int(f['qty'])} ({f['reason']}) @~{f['price']:.2f}")
                    if self.pos[s].shares <= 0:
                        self.pos[s] = None
                        break

        # 2) Daily circuit-breaker from real account equity.
        if self.risk.check_breaker(self._equity()):
            self._close_all(s, close, "breaker")

        # 3) Entries.
        can_enter = (self.pos[s] is None and not self.risk.halted
                     and minute >= open_min + self.cfg.or_minutes
                     and ts.time() <= self.no_new_t and self.orange[s])
        if can_enter:
            sig = self.strategy.entry_signal(d, self.orange[s])
            if sig in (ENTER_LONG, ENTER_SHORT):
                side = LONG if sig == ENTER_LONG else SHORT
                plan = self.risk.plan_entry(
                    self._equity(), self._equity(), side, close,
                    self.orange[s].high, self.orange[s].low)
                if plan:
                    self._submit(s, plan.shares, "buy" if side == LONG else "sell")
                    self.pos[s] = Position(
                        symbol=s, side=side, entry_price=close, entry_time=ts,
                        initial_shares=plan.shares, shares=plan.shares,
                        stop=plan.stop, risk_per_share=plan.risk_per_share,
                        high_water=close)
                    print(f"[enter] {side} {int(plan.shares)} {s} @~{close:.2f} "
                          f"stop {plan.stop:.2f}")

    def run(self):
        for s in self.symbols:
            self.stream.subscribe_bars(self.on_bar, s)
        print(f"Paper trading {self.symbols}  equity=${self._equity():,.2f}  (Ctrl-C to stop)")
        self.stream.run()


def main():
    p = argparse.ArgumentParser(description="Live Alpaca paper trader (ORB bot)")
    p.add_argument("--symbols", required=True, help="comma-separated tickers, e.g. AAPL,MSFT")
    args = p.parse_args()
    if not CONFIG.has_alpaca:
        raise SystemExit("Set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env first.")
    symbols = [x.strip().upper() for x in args.symbols.split(",") if x.strip()]
    PaperTrader(CONFIG, symbols).run()


if __name__ == "__main__":
    main()
