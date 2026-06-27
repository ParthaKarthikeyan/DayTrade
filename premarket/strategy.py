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

    def entry_long(self, bar: dict, session_high: float,
                   vwap: Optional[float], vol_avg: Optional[float]) -> bool:
        """True when the bar breaks the session high with VWAP + volume support.

        `session_high` is the high of all *prior* bars this session.
        """
        if vwap is None or vol_avg is None or vol_avg <= 0:
            return False
        return (
            bar["c"] > session_high
            and bar["c"] > vwap
            and bar["v"] >= self.cfg.pm_min_breakout_vol_mult * vol_avg
        )

    def initial_stop(self, entry: float, breakout_low: float) -> float:
        """Tightest sensible stop: the breakout bar's low, capped at pm_stop_pct."""
        pct_stop = entry * (1.0 - self.cfg.pm_stop_pct)
        return max(breakout_low, pct_stop)

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
