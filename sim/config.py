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

    # --- Premarket momentum strategy (live paper, $1–$10 listed small caps) ---
    pm_price_min: float = 1.0           # only trade names priced >= $1
    pm_price_max: float = 10.0          # ...and <= $10 (listed small caps)
    pm_min_gap_pct: float = 5.0         # minimum % up on the day to be a candidate
    pm_top_n: int = 10                  # watch this many top movers
    pm_max_positions: int = 1           # focus a small account on one trade at a time
    pm_session_start: str = "07:00"     # start watching in premarket (ET)
    pm_no_new_after: str = "11:30"      # open + 2h: stop taking new entries
    pm_flatten_at: str = "11:35"        # force-close everything by here
    pm_stop_pct: float = 0.05           # hard stop cap; the instant-cut usually fires first
    pm_min_breakout_vol_mult: float = 1.0  # breakout bar volume vs recent average
    # Ross-style scan + entry refinements
    pm_min_gain_pct: float = 25.0       # live: stock must be up >= this % on the day
    pm_max_float_shares: float = 10_000_000   # low-float filter (yfinance, fail-open)
    pm_require_news: bool = False       # require a recent news catalyst (weak free signal)
    pm_pullback_lookback: int = 3       # bars in the bull-flag before the breakout
    # Entry style: "flag" (consolidation breakout) or "first_pullback"
    # (Ross Cameron: squeeze -> 1-N red-candle pullback holding >= 50% of the
    # leg -> first candle to make a new high; stop = pullback low; target =
    # session-high retest at >= pm_fp_min_rr : 1, MACD not crossed down).
    pm_strategy: str = os.getenv("PM_STRATEGY", "first_pullback")
    pm_fp_leg_lookback: int = 10        # bars before the high that define the leg's low
    pm_fp_min_leg_pct: float = 0.02     # leg must be >= this (2%) to matter
    pm_fp_max_pullback_bars: int = 5    # pullback older than this is stale
    pm_fp_min_rr: float = 2.0           # target must pay >= 2x the risk
    pm_fp_macd: bool = True             # require MACD line above its signal line
    # Overnight research pass before the session (premarket/prescan.py): news
    # since yesterday's close (free Alpaca/Benzinga feed) -> price-band filter
    # -> seeded into the watchlist next to the 7:00 live movers scan.
    pm_prescan: bool = _flag("PM_PRESCAN", "true")
    pm_prescan_min_gap_pct: float = 3.0  # news name must also be moving >= this %

    # --- Premarket $10k book (the goal is judged on THIS, not the whole account) ---
    # The Alpaca paper account is shared by several experiments and holds an arbitrary
    # balance, so the bot trades a notional book seeded at pm_bankroll that compounds
    # with its own committed ledger P&L. Sizing/caps all reference the book.
    pm_bankroll: float = float(os.getenv("PM_BANKROLL", "10000"))
    pm_daily_target_pct: float = 0.015  # bank the day: flatten + stop at +1.5% (goal 1% net)
    pm_daily_max_loss_pct: float = 0.02 # daily stop: flatten + halt at -2% of the book
    pm_max_trades_per_day: int = 5      # hard cap on round-trips per session
    pm_loss_streak_pause: int = 2       # consecutive losers before a cooldown
    pm_cooldown_min: int = 20           # minutes of no-new-entries after the streak
    pm_max_position_pct: float = 0.5    # max notional per trade as a fraction of the book
    pm_min_stop_pct: float = 0.005      # min stop distance (blocks 1-cent-stop all-ins)
    # Bars: "yahoo" polls the consolidated all-venue tape (~1-2 min lag);
    # "iex_stream" is the old Alpaca websocket, which for low-float small caps
    # delivered almost no bars (IEX-executed trades only). See yahoo_feed.py.
    pm_data_source: str = os.getenv("PM_DATA_SOURCE", "yahoo")
    pm_poll_seconds: int = 60           # yahoo poll cadence (bars complete per minute)
    # Execution honesty on thin gappers: marketable LIMIT orders, never market.
    pm_limit_slip_pct: float = 0.004    # entry limit: signal price * (1 + this)
    pm_exit_slip_pct: float = 0.01      # exit limit band below the trigger price
    pm_fill_wait_s: int = 25            # poll a working order this long before cancelling
    pm_max_spread_pct: float = 0.01     # skip entries when quoted spread/mid exceeds this
    pm_min_bar_dollar_vol: float = 20_000.0  # avg $ volume/bar (recent) required to enter

    # --- Data / broker (Alpaca) ---
    data_feed: str = os.getenv("ALPACA_DATA_FEED", "iex")
    alpaca_key: str = os.getenv("ALPACA_API_KEY", "")
    alpaca_secret: str = os.getenv("ALPACA_SECRET_KEY", "")
    alpaca_paper: bool = _flag("ALPACA_PAPER", "true")

    @property
    def has_alpaca(self) -> bool:
        return bool(self.alpaca_key and self.alpaca_secret)


CONFIG = Config()
