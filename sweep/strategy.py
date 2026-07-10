"""Sweep-reversal trigger as an explicit per-level state machine.

The video's signal, made mechanical. For a LONG at a support level L:
  1. price pierces BELOW L (the "breakout" that will fail),
  2. within `reclaim_bars` bars a candle CLOSES back above L with a strong
     bullish body (>= body_mult x the average body) — the "snap back",
  3. enter at the NEXT bar's open; stop below the sweep extreme; target a
     fixed multiple `rr` of the risk.
A close more than `max_depth_bps` below L is a genuine breakdown -> that
level is dead for the day (the breakout did NOT fail; do not fade it).
Shorts mirror the same states at levels approached from below.
"""

from dataclasses import dataclass, field
from typing import List, Optional

IDLE, ARMED_LONG, ARMED_SHORT, DEAD = "idle", "armed_long", "armed_short", "dead"


@dataclass
class SweepParams:
    # Levels
    swing_sessions: int = 3
    swing_k: int = 2
    merge_bps: float = 5.0
    # Trigger
    min_sweep_bps: float = 3.0      # pierce depth below/above the level to arm
    max_depth_bps: float = 25.0     # close deeper than this = real breakout, level dead
    reclaim_bars: int = 5           # bars allowed between pierce and reclaim
    body_mult: float = 0.75         # reclaim body vs 20-bar average body
    # Position
    buffer_bps: float = 2.0         # stop distance beyond the sweep extreme
    rr: float = 2.0                 # target = rr x risk
    be_at_1r: bool = False          # move stop to entry once +1R trades (next bar)
    allow_short: bool = True
    # Session / governors
    session_end: str = "11:30"      # force flat; no entries in the last 15 min
    risk_pct: float = 0.01          # risk per trade, fraction of book equity
    max_lev: float = 2.0            # notional cap as multiple of book equity
    max_trades_per_day: int = 3
    daily_stop_r: float = 2.0       # lock out after losing this many R on the day
    cost_bps_side: float = 1.0      # spread/2 + slippage, charged per side


@dataclass
class Signal:
    side: str                       # "long" | "short"
    level: float
    sweep_extreme: float            # stop anchor


@dataclass
class LevelMachine:
    level: float
    state: str = IDLE
    sweep_extreme: float = 0.0
    bars_armed: int = 0

    def step(self, o: float, h: float, l: float, c: float,
             prev_close: float, avg_body: float, p: SweepParams) -> Optional[Signal]:
        L = self.level
        pierce = L * p.min_sweep_bps / 10_000.0
        depth = L * p.max_depth_bps / 10_000.0

        if self.state == DEAD:
            return None

        if self.state == IDLE:
            # Arm only when the level is approached from the far side: the prior
            # bar closed beyond it, this bar trades through it.
            if prev_close > L and l < L - pierce:
                self.state, self.sweep_extreme, self.bars_armed = ARMED_LONG, l, 0
            elif prev_close < L and h > L + pierce:
                self.state, self.sweep_extreme, self.bars_armed = ARMED_SHORT, h, 0
            # The arming bar itself may already close back through -> fall
            # through to the reclaim check below on the SAME bar.

        if self.state == ARMED_LONG:
            self.sweep_extreme = min(self.sweep_extreme, l)
            if c < L - depth:
                self.state = DEAD          # genuine breakdown, not a failed one
                return None
            if c > L:                      # reclaimed
                strong = (c > o) and (c - o) >= p.body_mult * avg_body
                if strong:
                    self.state = DEAD      # one shot per level per day
                    return Signal("long", L, self.sweep_extreme)
                self.state = IDLE          # weak reclaim: reset, may re-arm later
                return None
            self.bars_armed += 1
            if self.bars_armed > p.reclaim_bars:
                self.state = IDLE          # sweep fizzled sideways
            return None

        if self.state == ARMED_SHORT:
            self.sweep_extreme = max(self.sweep_extreme, h)
            if c > L + depth:
                self.state = DEAD
                return None
            if c < L:
                strong = (c < o) and (o - c) >= p.body_mult * avg_body
                if strong:
                    self.state = DEAD
                    return Signal("short", L, self.sweep_extreme)
                self.state = IDLE
                return None
            self.bars_armed += 1
            if self.bars_armed > p.reclaim_bars:
                self.state = IDLE
            return None

        return None


@dataclass
class Position:
    side: str
    entry: float
    stop: float
    target: float
    shares: float
    risk_ps: float                  # per-share risk at entry (entry - stop)
    at_breakeven: bool = False
    bump_next_bar: bool = False     # BE trigger seen this bar; applies next bar


def open_position(sig: Signal, entry: float, equity: float, p: SweepParams) -> Optional[Position]:
    """Size a position off the signal; None if the geometry is degenerate."""
    if sig.side == "long":
        stop = sig.sweep_extreme * (1 - p.buffer_bps / 10_000.0)
        risk_ps = entry - stop
        target = entry + p.rr * risk_ps
    else:
        stop = sig.sweep_extreme * (1 + p.buffer_bps / 10_000.0)
        risk_ps = stop - entry
        target = entry - p.rr * risk_ps
    if risk_ps <= 0:
        return None
    shares = min((equity * p.risk_pct) / risk_ps, (equity * p.max_lev) / entry)
    if shares <= 0:
        return None
    return Position(sig.side, entry, stop, target, shares, risk_ps)


def step_position(pos: Position, o: float, h: float, l: float, c: float,
                  is_last_bar: bool, p: SweepParams) -> Optional[float]:
    """Advance one bar; return the exit price if the position closed.

    Conservative sequencing inside a bar: the stop is checked FIRST (a bar that
    touches both stop and target counts as a stop-out), gaps fill at the open,
    and a break-even bump only protects from the NEXT bar onward."""
    if pos.bump_next_bar and not pos.at_breakeven:
        pos.stop = pos.entry
        pos.at_breakeven = True

    if pos.side == "long":
        if l <= pos.stop:
            return min(o, pos.stop)
        if h >= pos.target:
            return pos.target
        if p.be_at_1r and not pos.at_breakeven and h >= pos.entry + pos.risk_ps:
            pos.bump_next_bar = True
    else:
        if h >= pos.stop:
            return max(o, pos.stop)
        if l <= pos.target:
            return pos.target
        if p.be_at_1r and not pos.at_breakeven and l <= pos.entry - pos.risk_ps:
            pos.bump_next_bar = True

    if is_last_bar:
        return c
    return None


def make_machines(levels: List[float]) -> List[LevelMachine]:
    return [LevelMachine(lv) for lv in levels]
