"""The simulation engine: replay a day's 1-minute bars through the bot.

Deterministic and offline-friendly: feed it a DataFrame of OHLCV bars and it
runs the exact strategy + risk logic used live, producing a Portfolio with the
trade log and equity curve for reporting.
"""

from datetime import time as dtime
from typing import Optional

import pandas as pd

from .config import Config
from .indicators import add_indicators
from .portfolio import LONG, SHORT, Portfolio, Position
from .risk import RiskManager
from .strategy import ENTER_LONG, ENTER_SHORT, NONE, ORBStrategy, OpeningRange


def _parse_hhmm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


class SimEngine:
    def __init__(self, config: Config):
        self.cfg = config
        self.strategy = ORBStrategy(config)
        self.risk = RiskManager(config)

    def _slip(self, price: float, *, buying: bool) -> float:
        adj = self.cfg.slippage_bps / 10_000.0
        return price * (1 + adj) if buying else price * (1 - adj)

    def run(self, df: pd.DataFrame, symbol: str) -> Portfolio:
        cfg = self.cfg
        df = add_indicators(df, cfg.ema_fast, cfg.ema_slow)

        open_t = _parse_hhmm(cfg.market_open)
        no_new_t = _parse_hhmm(cfg.no_new_trades_after)
        flatten_t = _parse_hhmm(cfg.flatten_at)

        # Opening range = first `or_minutes` of bars from the open.
        bars_time = df.index
        or_mask = [(t.time() >= open_t) and
                   (t.hour * 60 + t.minute) < (open_t.hour * 60 + open_t.minute + cfg.or_minutes)
                   for t in bars_time]
        or_df = df[or_mask]
        opening_range: Optional[OpeningRange] = None
        if not or_df.empty:
            opening_range = OpeningRange(high=float(or_df["high"].max()),
                                         low=float(or_df["low"].min()))
        or_ready_minute = open_t.hour * 60 + open_t.minute + cfg.or_minutes

        port = Portfolio(cfg.start_cash)
        self.risk.start_day(cfg.start_cash)

        for t, row in df.iterrows():
            bar = {k: float(row[k]) for k in
                   ("open", "high", "low", "close", "ema_fast", "ema_slow", "vwap")}
            minute = t.hour * 60 + t.minute
            close = bar["close"]

            # 1) Manage / force-close any open position.
            if port.position is not None:
                if t.time() >= flatten_t or self.risk.halted:
                    self._close_all(port, close, t,
                                    "breaker" if self.risk.halted else "time_stop")
                else:
                    for f in self.risk.manage(port.position, bar):
                        port.close_shares(f["qty"], f["price"], f["reason"], t)

            # 2) Daily circuit-breaker on mark-to-market equity.
            equity = port.equity(close)
            if self.risk.check_breaker(equity) and port.position is not None:
                self._close_all(port, close, t, "breaker")

            # 3) New entries (only after OR is set, before the cutoff, not halted).
            can_enter = (port.position is None and not self.risk.halted
                         and minute >= or_ready_minute and t.time() <= no_new_t)
            if can_enter:
                sig = self.strategy.entry_signal(bar, opening_range)
                if sig in (ENTER_LONG, ENTER_SHORT):
                    self._open(port, symbol, sig, close, opening_range, t)

            port.record_equity(t, close)

        # Safety: never hold overnight.
        if port.position is not None and len(df):
            last_t = df.index[-1]
            self._close_all(port, float(df.iloc[-1]["close"]), last_t, "eod_safety")

        return port

    def _open(self, port, symbol, sig, price, orange, t) -> None:
        side = LONG if sig == ENTER_LONG else SHORT
        plan = self.risk.plan_entry(
            equity=port.equity(price), cash=port.cash, side=side,
            entry_price=price, or_high=orange.high, or_low=orange.low)
        if plan is None:
            return
        fill_price = self._slip(price, buying=(side == LONG))
        port.open(Position(
            symbol=symbol, side=side, entry_price=fill_price, entry_time=t,
            initial_shares=plan.shares, shares=plan.shares, stop=plan.stop,
            risk_per_share=plan.risk_per_share))

    def _close_all(self, port, price, t, reason) -> None:
        pos = port.position
        if pos is None:
            return
        fill_price = self._slip(price, buying=(pos.side == SHORT))
        port.close_shares(pos.shares, fill_price, reason, t)
