"""Central configuration for the bot and simulator.

All tunables live here so the strategy, risk engine, simulator and live paper
loop read from one source of truth. Secrets come from the environment (.env);
everything else has a sensible default you can override per run.
"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    # --- Account ---
    start_cash: float = 2000.0          # the $2000 the simulator starts with

    # --- Session (US/Eastern, regular hours) ---
    timezone: str = "America/New_York"
    market_open: str = "09:30"
    market_close: str = "16:00"
    or_minutes: int = 15                # opening-range window length
    no_new_trades_after: str = "15:30"  # stop opening new positions
    flatten_at: str = "15:55"           # force-close everything (no overnight)

    # --- Strategy (ORB + EMA/VWAP) ---
    ema_fast: int = 9
    ema_slow: int = 20
    allow_short: bool = True

    # --- Risk / money management ---
    risk_per_trade_pct: float = 0.01    # risk 1% of equity per trade
    max_stop_pct: float = 0.01          # per-trade stop cap: 1% of entry (<= 2%)
    daily_max_loss_pct: float = 0.05    # 5% daily drawdown circuit-breaker
    reward_risk: float = 2.0            # scale half the position off at 2R
    trail_pct: float = 0.005            # 0.5% trailing stop on the runner
    max_leverage: float = 1.0           # 1.0 = cash only, no margin

    # --- Fill model (backtest) ---
    slippage_bps: float = 1.0           # 1 basis point adverse slippage per fill

    # --- Data / broker (Alpaca) ---
    data_feed: str = os.getenv("ALPACA_DATA_FEED", "iex")
    alpaca_key: str = os.getenv("ALPACA_API_KEY", "")
    alpaca_secret: str = os.getenv("ALPACA_SECRET_KEY", "")
    alpaca_paper: bool = _flag("ALPACA_PAPER", "true")

    @property
    def has_alpaca(self) -> bool:
        return bool(self.alpaca_key and self.alpaca_secret)


CONFIG = Config()
