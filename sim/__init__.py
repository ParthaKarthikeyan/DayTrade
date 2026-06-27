"""US-stock day-trading bot + simulator.

Opening Range Breakout (ORB) strategy with EMA/VWAP trend filter, a two-layer
risk engine (per-trade stop + 5% daily circuit-breaker), and scale-out profit
taking. The same strategy/risk code powers both the backtest simulator
(`sim.engine`) and the live Alpaca paper loop (`run_paper.py`).
"""

from .config import CONFIG, Config

__all__ = ["CONFIG", "Config"]
