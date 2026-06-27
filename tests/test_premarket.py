"""Offline tests for the premarket gap-and-go logic (no network)."""

import copy

import pandas as pd
import pytest

from premarket.backtest import simulate_symbol_day
from premarket.scanner import rank_gappers
from premarket.strategy import PMPosition, PremarketMomentum
from sim.config import CONFIG


def cfg(**over):
    c = copy.copy(CONFIG)
    c.start_cash = 2000.0
    c.risk_per_trade_pct = 0.01
    c.pm_stop_pct = 0.05
    c.pm_min_breakout_vol_mult = 1.0
    c.pm_price_min, c.pm_price_max, c.pm_min_gap_pct, c.pm_top_n = 1.0, 10.0, 5.0, 5
    c.daily_max_loss_pct = 0.05
    for k, v in over.items():
        setattr(c, k, v)
    return c


def make_bars(closes, vols, date="2026-06-26"):
    idx = pd.date_range(f"{date} 09:30", periods=len(closes), freq="1min",
                        tz="America/New_York")
    return [{"t": t, "o": c, "h": c + 0.02, "l": c - 0.02, "c": c, "v": v}
            for t, c, v in zip(idx, closes, vols)]


# --- scanner -------------------------------------------------------------
def test_rank_gappers_filters_and_sorts():
    rows = [
        {"symbol": "A", "ref_price": 3.0, "gap_pct": 12.0},   # keep
        {"symbol": "B", "ref_price": 50.0, "gap_pct": 20.0},  # too pricey
        {"symbol": "C", "ref_price": 4.0, "gap_pct": 2.0},    # gap too small
        {"symbol": "D", "ref_price": 2.0, "gap_pct": 30.0},   # keep, biggest
    ]
    out = rank_gappers(rows, cfg())
    assert [r["symbol"] for r in out] == ["D", "A"]


# --- strategy primitives -------------------------------------------------
def test_entry_requires_breakout_vwap_and_volume():
    s = PremarketMomentum(cfg())
    bar = {"o": 3.0, "h": 3.1, "l": 3.0, "c": 3.10, "v": 5000}
    assert s.entry_long(bar, session_high=3.05, vwap=3.00, vol_avg=2000) is True
    assert s.entry_long(bar, session_high=3.20, vwap=3.00, vol_avg=2000) is False  # no breakout
    assert s.entry_long(bar, session_high=3.05, vwap=3.20, vol_avg=2000) is False  # below VWAP
    assert s.entry_long(bar, session_high=3.05, vwap=3.00, vol_avg=9999) is False  # weak volume


def test_exit_priority():
    s = PremarketMomentum(cfg())
    pos = PMPosition("X", entry=3.0, entry_time=None, shares=100, stop=2.9, high_water=3.0)
    assert s.exit_reason(pos, {"l": 2.85, "c": 2.95}, {"l": 2.9}, 2.95) == "stop"
    assert s.exit_reason(pos, {"l": 2.95, "c": 2.88}, {"l": 2.90}, 2.99) == "momentum_break"
    assert s.exit_reason(pos, {"l": 2.95, "c": 2.93}, {"l": 2.80}, 2.99) == "lost_vwap"
    assert s.exit_reason(pos, {"l": 2.95, "c": 3.05}, {"l": 2.80}, 2.99) is None


# --- end-to-end day ------------------------------------------------------
def test_breakout_day_takes_a_profitable_trade():
    closes = [3.00] * 8 + [3.05, 3.12, 3.20, 3.28, 3.36, 3.44, 3.40]
    vols = [1000] * 8 + [2500] * 7
    trades, end_eq, halted = simulate_symbol_day(make_bars(closes, vols), "TST", 2000.0, cfg())
    assert trades, "a breakout should trigger at least one entry"
    assert end_eq > 2000.0
    assert not halted


def test_fader_is_cut_for_a_small_loss():
    closes = [3.00] * 8 + [3.06, 2.95, 2.90, 2.88]
    vols = [1000] * 8 + [2500, 1500, 1500, 1500]
    trades, end_eq, halted = simulate_symbol_day(make_bars(closes, vols), "TST", 2000.0, cfg())
    assert trades and trades[0]["pnl"] < 0          # entered then cut
    loss_pct = (2000.0 - end_eq) / 2000.0
    assert 0 < loss_pct <= 0.02                      # instant-cut keeps it small
