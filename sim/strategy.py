"""Entry logic: Opening Range Breakout filtered by EMA crossover and VWAP.

This module is intentionally pure (no I/O, no broker, no clock). It turns a bar
plus the day's opening range into an entry decision, so the exact same code runs
in the backtest simulator and in the live paper loop.
"""

from dataclasses import dataclass
from typing import Mapping, Optional

from .config import Config

NONE = "none"
ENTER_LONG = "enter_long"
ENTER_SHORT = "enter_short"


@dataclass
class OpeningRange:
    high: float
    low: float


class ORBStrategy:
    def __init__(self, config: Config):
        self.cfg = config

    def entry_signal(self, bar: Mapping, opening_range: Optional[OpeningRange]) -> str:
        """Decide whether the bar triggers a long/short entry.

        bar must expose: close, ema_fast, ema_slow, vwap.
        Returns one of: ENTER_LONG, ENTER_SHORT, NONE.
        """
        if opening_range is None:
            return NONE  # opening range not yet established

        close = bar["close"]
        ema_fast = bar["ema_fast"]
        ema_slow = bar["ema_slow"]
        vwap_val = bar["vwap"]

        # Skip until indicators are warm (NaN during the first few bars).
        if any(v != v for v in (ema_fast, ema_slow, vwap_val)):  # NaN check
            return NONE

        long_ok = (
            close > opening_range.high
            and ema_fast > ema_slow
            and close > vwap_val
        )
        if long_ok:
            return ENTER_LONG

        short_ok = (
            self.cfg.allow_short
            and close < opening_range.low
            and ema_fast < ema_slow
            and close < vwap_val
        )
        if short_ok:
            return ENTER_SHORT

        return NONE
