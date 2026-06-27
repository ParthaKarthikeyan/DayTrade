"""Shared state hand-off between the bot and the live dashboard.

The bot (backtest or paper loop) writes a JSON snapshot to a state file; the
Flask dashboard polls it. Keeping this in one place means the page never has to
know anything about the engine internals.
"""

import json
import os
import tempfile
from typing import List, Optional

from .portfolio import Portfolio
from .report import compute_metrics


def _trade_to_dict(t) -> dict:
    return {
        "entry_time": str(t.entry_time),
        "exit_time": str(t.exit_time),
        "side": t.side,
        "shares": t.shares,
        "entry_price": round(t.entry_price, 4),
        "pnl": round(t.pnl, 2),
        "exits": [
            {"time": str(f["time"]), "qty": f["qty"],
             "price": round(f["price"], 4), "reason": f["reason"],
             "pnl": round(f["pnl"], 2)}
            for f in t.exits
        ],
    }


def _position_to_dict(pos) -> Optional[dict]:
    if pos is None:
        return None
    return {
        "side": pos.side,
        "shares": pos.shares,
        "entry_price": round(pos.entry_price, 4),
        "stop": round(pos.stop, 4),
        "half_taken": pos.half_taken,
    }


def build_state(port: Portfolio, mode: str, symbol: str, updated_at: str,
                halted: bool, last_price: float = 0.0) -> dict:
    """Build a JSON-serializable snapshot of the run for the dashboard."""
    metrics = compute_metrics(port)
    equity = port.equity_curve[-1][1] if port.equity_curve else port.equity(last_price)
    pnl = equity - port.start_cash
    return {
        "mode": mode,
        "symbol": symbol,
        "updated_at": updated_at,
        "start_cash": port.start_cash,
        "equity": round(equity, 2),
        "pnl": round(pnl, 2),
        "pnl_pct": round(pnl / port.start_cash * 100.0, 2) if port.start_cash else 0.0,
        "halted": halted,
        "position": _position_to_dict(port.position),
        "equity_curve": [[str(t), round(e, 2)] for t, e in port.equity_curve],
        "trades": [_trade_to_dict(t) for t in port.trades],
        "metrics": {k: (None if v == float("inf") else v) for k, v in metrics.items()},
    }


def build_live_state(mode: str, symbol: str, updated_at: str, start_cash: float,
                     equity: float, halted: bool, position: Optional[dict],
                     equity_curve: List[list], trades: List[dict]) -> dict:
    """State builder for the paper loop, which sources equity from the broker."""
    pnl = equity - start_cash
    return {
        "mode": mode,
        "symbol": symbol,
        "updated_at": updated_at,
        "start_cash": start_cash,
        "equity": round(equity, 2),
        "pnl": round(pnl, 2),
        "pnl_pct": round(pnl / start_cash * 100.0, 2) if start_cash else 0.0,
        "halted": halted,
        "position": position,
        "equity_curve": equity_curve,
        "trades": trades,
        "metrics": {},
    }


def write_state(path: str, state: dict) -> None:
    """Atomically write the state file (temp file + os.replace)."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
