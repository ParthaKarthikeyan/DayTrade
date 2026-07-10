"""Mechanical level extraction — the "mark up the chart" step, made rule-based.

For a given session the level set is built ONLY from information available
before 09:30 ET that day:
  - prior session's regular-hours HIGH, LOW and CLOSE,
  - today's premarket HIGH and LOW (08:00-09:30 ET on IEX),
  - 15-minute swing highs/lows over the last few sessions (a local extreme
    with `k` bars on each side — the video's "areas where price has touched
    and sold off multiple times", without the eyeball).

Levels closer together than `merge_bps` collapse into one (the video's point
that "the market reacts to a general area, not a perfect price").
"""

from dataclasses import dataclass
from typing import Dict, List, Tuple

import pandas as pd

RTH_START = "09:30"
RTH_END = "16:00"
PM_START = "08:00"          # IEX premarket open — nothing prints before this


@dataclass(frozen=True)
class Level:
    price: float
    source: str             # "prior_high" | "prior_low" | "prior_close" | "pm_high" | "pm_low" | "swing"


def build_day_frames(minute_bars: pd.DataFrame) -> Tuple[list, Dict]:
    """Split a continuous ET-indexed minute frame into per-session frames.
    Returns (ordered session dates, {date: frame})."""
    frames = {d: g for d, g in minute_bars.groupby(minute_bars.index.date)}
    return sorted(frames), frames


def swing_levels(bars_15m: pd.DataFrame, k: int = 2) -> List[Level]:
    """Local extremes with k bars on each side, on 15-minute bars."""
    out: List[Level] = []
    highs, lows = bars_15m["high"].values, bars_15m["low"].values
    n = len(bars_15m)
    for i in range(k, n - k):
        if highs[i] == max(highs[i - k:i + k + 1]):
            out.append(Level(float(highs[i]), "swing"))
        if lows[i] == min(lows[i - k:i + k + 1]):
            out.append(Level(float(lows[i]), "swing"))
    return out


def merge_levels(levels: List[Level], ref_price: float, merge_bps: float) -> List[float]:
    """Collapse levels within merge_bps of each other into one zone price."""
    if not levels:
        return []
    tol = ref_price * merge_bps / 10_000.0
    prices = sorted(lv.price for lv in levels)
    zones: List[List[float]] = [[prices[0]]]
    for p in prices[1:]:
        if p - zones[-1][-1] <= tol:
            zones[-1].append(p)
        else:
            zones.append([p])
    return [sum(z) / len(z) for z in zones]


def session_levels(days: list, frames: Dict, i: int, *,
                   swing_sessions: int = 3, swing_k: int = 2,
                   merge_bps: float = 5.0) -> List[float]:
    """Level set for session days[i], using only data before that open."""
    if i <= 0:
        return []                       # no prior session -> no levels
    prior_days = days[max(0, i - swing_sessions):i]

    levels: List[Level] = []

    # Prior session RTH high / low / close.
    prev_rth = frames[prior_days[-1]].between_time(RTH_START, RTH_END)
    if len(prev_rth):
        levels.append(Level(float(prev_rth["high"].max()), "prior_high"))
        levels.append(Level(float(prev_rth["low"].min()), "prior_low"))
        levels.append(Level(float(prev_rth["close"].iloc[-1]), "prior_close"))

    # Today's premarket range (08:00 -> 09:29 ET).
    pm = frames[days[i]].between_time(PM_START, "09:29")
    if len(pm):
        levels.append(Level(float(pm["high"].max()), "pm_high"))
        levels.append(Level(float(pm["low"].min()), "pm_low"))

    # 15-minute swings across the recent prior sessions (RTH only).
    hist_rth = pd.concat([frames[d] for d in prior_days]).between_time(RTH_START, RTH_END)
    if len(hist_rth):
        b15 = (hist_rth.resample("15min")
               .agg({"open": "first", "high": "max", "low": "min",
                     "close": "last", "volume": "sum"}).dropna())
        levels.extend(swing_levels(b15, k=swing_k))

    ref = float(pm["close"].iloc[-1]) if len(pm) else (
        float(prev_rth["close"].iloc[-1]) if len(prev_rth) else 0.0)
    if not ref:
        return []
    return merge_levels(levels, ref, merge_bps)


def levels_for_all_days(days: list, frames: Dict, *, swing_sessions: int = 3,
                        swing_k: int = 2, merge_bps: float = 5.0) -> Dict:
    """Precompute {date: [levels]} once — shared across a parameter sweep."""
    return {days[i]: session_levels(days, frames, i, swing_sessions=swing_sessions,
                                    swing_k=swing_k, merge_bps=merge_bps)
            for i in range(len(days))}
