"""Account state: cash, open positions, equity curve and the trade log.

Supports partial closes so the scale-out profit rule (half off at 2R, runner on
a trailing stop) is recorded as separate fills that roll up into one round-trip
trade for reporting.
"""

from dataclasses import dataclass, field
from typing import List, Optional

LONG = "long"
SHORT = "short"


@dataclass
class Position:
    symbol: str
    side: str                 # LONG or SHORT
    entry_price: float
    entry_time: object
    initial_shares: float
    shares: float             # currently open shares
    stop: float               # active stop price (moves with scale-out)
    risk_per_share: float
    half_taken: bool = False
    high_water: float = 0.0   # running favourable extreme for the trailing stop
    realized_pnl: float = 0.0
    exits: List[dict] = field(default_factory=list)

    @property
    def is_long(self) -> bool:
        return self.side == LONG

    def signed(self) -> int:
        return 1 if self.is_long else -1


@dataclass
class Trade:
    """A completed round trip (one entry, one or more scale-out exits)."""
    symbol: str
    side: str
    entry_time: object
    exit_time: object
    entry_price: float
    shares: float
    pnl: float
    exits: List[dict]


class Portfolio:
    def __init__(self, start_cash: float):
        self.start_cash = start_cash
        self.cash = start_cash
        self.position: Optional[Position] = None  # single symbol at a time
        self.trades: List[Trade] = []
        self.equity_curve: List[tuple] = []  # (timestamp, equity)

    # --- accounting -------------------------------------------------------
    def open(self, pos: Position) -> None:
        # Long: pay for shares. Short: receive proceeds (and carry a buy-back liability).
        self.cash -= pos.signed() * pos.shares * pos.entry_price
        pos.high_water = pos.entry_price
        self.position = pos

    def close_shares(self, qty: float, price: float, reason: str, time) -> float:
        """Close `qty` shares at `price`. Returns realized PnL for this fill."""
        pos = self.position
        assert pos is not None and qty <= pos.shares + 1e-9
        # Buy back (short) or sell (long): cash moves opposite to entry.
        self.cash += pos.signed() * qty * price
        pnl = pos.signed() * qty * (price - pos.entry_price)
        pos.realized_pnl += pnl
        pos.shares -= qty
        fill = {"time": time, "qty": qty, "price": price, "reason": reason, "pnl": pnl}
        pos.exits.append(fill)
        if pos.shares <= 1e-9:
            self.trades.append(Trade(
                symbol=pos.symbol, side=pos.side, entry_time=pos.entry_time,
                exit_time=time, entry_price=pos.entry_price,
                shares=pos.initial_shares, pnl=pos.realized_pnl, exits=pos.exits,
            ))
            self.position = None
        return pnl

    def market_value(self, price: float) -> float:
        if self.position is None:
            return 0.0
        return self.position.signed() * self.position.shares * price

    def equity(self, price: float) -> float:
        return self.cash + self.market_value(price)

    def record_equity(self, time, price: float) -> None:
        self.equity_curve.append((time, self.equity(price)))
