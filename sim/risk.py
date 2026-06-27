"""The two-layer risk engine.

Layer 1 (per trade): position sizing from a fixed % equity risk, a tight stop,
and scale-out exits (breakeven at +1R, half off at +2R, trailing stop on the
runner).

Layer 2 (per day): a hard 5% account drawdown circuit-breaker that halts new
trades and flattens open positions for the rest of the day.
"""

import math
from dataclasses import dataclass
from typing import List, Optional

from .config import Config
from .portfolio import LONG, SHORT, Position


@dataclass
class EntryPlan:
    side: str
    shares: float
    stop: float
    risk_per_share: float


class RiskManager:
    def __init__(self, config: Config):
        self.cfg = config
        self.day_start_equity: float = config.start_cash
        self.halted: bool = False

    # --- Layer 2: daily circuit-breaker -----------------------------------
    def start_day(self, equity: float) -> None:
        self.day_start_equity = equity
        self.halted = False

    def check_breaker(self, equity: float) -> bool:
        """Return True (and latch halted) once equity is >=5% below day start."""
        floor = self.day_start_equity * (1.0 - self.cfg.daily_max_loss_pct)
        if equity <= floor:
            self.halted = True
        return self.halted

    # --- Layer 1: sizing & stop placement ---------------------------------
    def plan_entry(self, equity: float, cash: float, side: str,
                   entry_price: float, or_high: float, or_low: float) -> Optional[EntryPlan]:
        """Size a new position. Returns None if it cannot be taken."""
        # Stop = the *tighter* of opposite OR side and the max stop %.
        pct_dist = entry_price * self.cfg.max_stop_pct
        if side == LONG:
            or_dist = entry_price - or_low
            risk_per_share = min(d for d in (or_dist, pct_dist) if d > 0)
            stop = entry_price - risk_per_share
        else:
            or_dist = or_high - entry_price
            risk_per_share = min(d for d in (or_dist, pct_dist) if d > 0)
            stop = entry_price + risk_per_share

        if risk_per_share <= 0:
            return None

        risk_dollars = equity * self.cfg.risk_per_trade_pct
        shares = math.floor(risk_dollars / risk_per_share)

        # Cap by buying power (no leverage by default).
        max_affordable = math.floor((cash * self.cfg.max_leverage) / entry_price)
        shares = min(shares, max_affordable)
        if shares < 1:
            return None

        return EntryPlan(side=side, shares=shares, stop=stop,
                         risk_per_share=risk_per_share)

    # --- Layer 1: manage an open position bar-by-bar ----------------------
    def manage(self, pos: Position, bar: dict) -> List[dict]:
        """Inspect one bar and return the exit fills to apply, in order.

        Conservative intrabar model: the stop (as it stood entering the bar) is
        checked first against bar low/high, so a bar that touches both target and
        stop is assumed to have hit the stop.

        Each returned fill: {"qty", "price", "reason"}.
        """
        cfg = self.cfg
        fills: List[dict] = []
        rps = pos.risk_per_share
        sign = pos.signed()

        # 1) Stop hit? (pre-bar stop level)
        stop_hit = (bar["low"] <= pos.stop) if pos.is_long else (bar["high"] >= pos.stop)
        if stop_hit:
            reason = "breakeven_stop" if abs(pos.stop - pos.entry_price) < 1e-9 else (
                "trail_stop" if pos.half_taken else "stop_loss")
            fills.append({"qty": pos.shares, "price": pos.stop, "reason": reason})
            return fills

        target_1r = pos.entry_price + sign * 1.0 * rps
        target_2r = pos.entry_price + sign * cfg.reward_risk * rps
        reached_1r = (bar["high"] >= target_1r) if pos.is_long else (bar["low"] <= target_1r)
        reached_2r = (bar["high"] >= target_2r) if pos.is_long else (bar["low"] <= target_2r)

        # 2) Move stop to breakeven once +1R is reached.
        if reached_1r:
            pos.stop = (max(pos.stop, pos.entry_price) if pos.is_long
                        else min(pos.stop, pos.entry_price))

        # 3) Scale half off at +2R (once).
        if reached_2r and not pos.half_taken:
            half = math.floor(pos.shares / 2)
            if half >= 1:
                fills.append({"qty": half, "price": target_2r, "reason": "scale_2R"})
            pos.half_taken = True

        # 4) Trail the runner: ratchet the stop toward price by trail_pct.
        if pos.is_long:
            pos.high_water = max(pos.high_water, bar["high"])
            if pos.half_taken:
                trail = pos.high_water * (1.0 - cfg.trail_pct)
                pos.stop = max(pos.stop, trail)
        else:
            pos.high_water = min(pos.high_water, bar["low"])
            if pos.half_taken:
                trail = pos.high_water * (1.0 + cfg.trail_pct)
                pos.stop = min(pos.stop, trail)

        return fills
