"""Pure premarket momentum logic (no I/O), shared by backtest and live bot.

Entry: a bar makes a NEW session high (breakout) while above VWAP on strong
volume. Exit (instant cut): the first of — stop hit, a close back below the
prior bar's low (momentum break), or a close back below VWAP. Long only.
"""

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class PMPosition:
    symbol: str
    entry: float
    entry_time: object
    shares: float
    stop: float
    high_water: float
    realized: float = 0.0
    exits: List[dict] = field(default_factory=list)


class PremarketMomentum:
    def __init__(self, cfg):
        self.cfg = cfg

    def entry_signal(self, window, vwap: Optional[float],
                     vol_avg: Optional[float]) -> Optional[float]:
        """Ross-style bull-flag breakout. Returns the stop price if it triggers.

        `window` is the recent bars with the current bar last. The bars before it
        form the "flag" (a small consolidation/pullback after a move up). We enter
        when the current bar breaks to a NEW HIGH above the flag, while above VWAP
        and on strong volume. Stop = the low of the flag (capped at pm_stop_pct).
        Returns None if no entry.
        """
        cfg = self.cfg
        L = cfg.pm_pullback_lookback
        if vwap is None or vol_avg is None or vol_avg <= 0 or len(window) < L + 1:
            return None
        cur = window[-1]
        flag = window[-(L + 1):-1]            # the L bars before the current one
        flag_high = max(b["h"] for b in flag)
        flag_low = min(b["l"] for b in flag)

        if cur["h"] <= flag_high:             # not a new high over the flag yet
            return None
        if cur["c"] <= vwap:                  # must hold above VWAP
            return None
        if cur["v"] < cfg.pm_min_breakout_vol_mult * vol_avg:   # needs volume
            return None

        stop = max(flag_low, cur["c"] * (1.0 - cfg.pm_stop_pct))  # cap risk
        return stop if stop < cur["c"] else None

    def exit_reason(self, pos: PMPosition, bar: dict, prev_bar: Optional[dict],
                    vwap: Optional[float]) -> Optional[str]:
        """First exit trigger for an open long, in priority order."""
        if bar["l"] <= pos.stop:
            return "stop"
        if prev_bar is not None and bar["c"] < prev_bar["l"]:
            return "momentum_break"   # close back under the prior bar's low
        if vwap is not None and bar["c"] < vwap:
            return "lost_vwap"
        return None
