"""Pure premarket momentum logic (no I/O), shared by backtest and live bot.

Two entry styles, selected by cfg.pm_strategy:

- "flag" (PremarketMomentum): a bar breaks the recent consolidation's high
  while above VWAP on strong volume.
- "first_pullback" (FirstPullback): the Ross Cameron setup — after a squeeze,
  wait for a 1-N red-candle pullback that HOLDS at least 50% of the up-leg,
  enter on the first candle to make a new high over the prior candle, stop at
  the pullback low, profit target at a retest of the session high, and only
  take the trade when that target is >= pm_fp_min_rr times the risk. MACD
  (12/26/9 on the session's bars) must not be crossed down.

Exits (both): stop hit, a close back below the prior bar's low (momentum
break / "breakout or bailout"), or a close back below VWAP; first-pullback
additionally sells into a touch of its target. Long only.
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
    target: Optional[float] = None
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


class FirstPullback(PremarketMomentum):
    """Ross Cameron's first-pullback entry (see module docstring).

    entry_signal takes the FULL session bar history (current bar last) and
    returns {"stop", "target"} or None. Selling the whole position into the
    target retest keeps the trade honest to the 2:1 rule it was sized on.
    """

    MACD_MIN_BARS = 30            # below this the MACD gate is skipped (too
                                  # few session bars to mean anything yet)

    def _macd_ok(self, hist: List[dict]) -> bool:
        if len(hist) < self.MACD_MIN_BARS:
            return True
        import pandas as pd
        closes = pd.Series([b["c"] for b in hist])
        macd = closes.ewm(span=12, adjust=False).mean() - \
            closes.ewm(span=26, adjust=False).mean()
        signal = macd.ewm(span=9, adjust=False).mean()
        return float(macd.iloc[-1]) > float(signal.iloc[-1])

    def entry_signal(self, hist: List[dict], vwap: Optional[float],
                     vol_avg: Optional[float]) -> Optional[dict]:
        cfg = self.cfg
        n = len(hist)
        if n < 4 or vwap is None or vol_avg is None or vol_avg <= 0:
            return None
        cur, prev = hist[-1], hist[-2]
        highs = [b["h"] for b in hist[:-1]]

        # the up-leg: session high before the current bar
        leg_idx = max(range(len(highs)), key=lambda i: highs[i])
        leg_high = highs[leg_idx]
        pull = hist[leg_idx + 1:-1]           # completed pullback bars
        if not (1 <= len(pull) <= cfg.pm_fp_max_pullback_bars):
            return None                        # no pullback yet, or gone stale
        if not any(b["c"] < b["o"] for b in pull):
            return None                        # needs at least one red candle
        leg_low = min(b["l"] for b in
                      hist[max(0, leg_idx - cfg.pm_fp_leg_lookback):leg_idx + 1])
        if leg_low <= 0 or leg_high / leg_low - 1 < cfg.pm_fp_min_leg_pct:
            return None                        # leg too small to mean anything
        pull_low = min(b["l"] for b in pull)
        if pull_low < leg_high - 0.5 * (leg_high - leg_low):
            return None                        # broke the 50% retracement

        if cur["h"] <= prev["h"]:              # first candle to make a new high
            return None
        if cur["c"] <= vwap:
            return None
        if cur["v"] < cfg.pm_min_breakout_vol_mult * vol_avg:
            return None
        if cfg.pm_fp_macd and not self._macd_ok(hist):
            return None

        entry = cur["c"]
        stop = max(pull_low, entry * (1.0 - cfg.pm_stop_pct))
        risk = entry - stop
        # target = retest of the session high; skip unless it pays >= min RR
        if risk <= 0 or leg_high - entry < cfg.pm_fp_min_rr * risk:
            return None
        return {"stop": stop, "target": leg_high}

    def exit_reason(self, pos: PMPosition, bar: dict, prev_bar: Optional[dict],
                    vwap: Optional[float]) -> Optional[str]:
        if pos.target is not None and bar["h"] >= pos.target:
            return "target"                    # sell into the HOD retest
        return super().exit_reason(pos, bar, prev_bar, vwap)


def make_strategy(cfg) -> PremarketMomentum:
    if getattr(cfg, "pm_strategy", "flag") == "first_pullback":
        return FirstPullback(cfg)
    return PremarketMomentum(cfg)


def split_signal(sig) -> tuple:
    """Normalize a strategy signal to (stop, target); flag returns a bare stop."""
    if isinstance(sig, dict):
        return sig["stop"], sig.get("target")
    return sig, None
