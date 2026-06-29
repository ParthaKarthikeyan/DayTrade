"""Configuration for the forex session-breakout bot.

Times are in UTC minutes-of-day (FX is 24h, no single exchange). Defaults anchor
on the London open / London-NY overlap, the most liquid window for majors.
"""

from dataclasses import dataclass, field
from typing import Tuple


@dataclass
class ForexConfig:
    # Instruments (yfinance FX symbols; majors = tight spreads)
    pairs: Tuple[str, ...] = ("EURUSD=X", "GBPUSD=X")
    interval: str = "15m"

    # Account / risk
    start_cash: float = 2000.0
    risk_per_trade_pct: float = 0.01     # risk 1% of equity per trade
    reward_risk: float = 2.0             # take profit at 2R
    max_leverage: float = 30.0           # OANDA retail-ish cap
    daily_max_loss_pct: float = 0.05     # 5% daily circuit-breaker

    # Costs
    pip: float = 0.0001                  # majors; 1 pip = 0.0001
    spread_pips: float = 1.0             # round-trip spread modeled on majors

    # Session ORB (UTC minutes from midnight)
    session_start_utc: int = 7           # ~London open (07:00 UTC)
    or_minutes: int = 60                 # opening-range length
    no_new_min: int = 240                # no new entries after +4h
    flatten_min: int = 480               # flatten by +8h (end of overlap)
    max_stop_pips: float = 30.0          # cap the OR-based stop

    # Trend
    trend_ema: int = 200                 # higher-timeframe trend proxy (on 15m)
    fast_ema: int = 20                   # for the pure-trend (EMA cross) variant
    slow_ema: int = 50


DEFAULT = ForexConfig()
