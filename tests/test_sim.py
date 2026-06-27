"""Offline unit + integration tests for the bot and simulator."""

import copy

import pandas as pd
import pytest

from sim.config import CONFIG
from sim.engine import SimEngine
from sim.indicators import ema, vwap
from sim.portfolio import LONG, SHORT
from sim.risk import RiskManager
from tests.synthetic import bull_trap, crash_day, sawtooth_down, uptrend_breakout


def fresh_cfg(**over):
    cfg = copy.copy(CONFIG)
    cfg.start_cash = 2000.0
    cfg.risk_per_trade_pct = 0.01
    cfg.max_stop_pct = 0.01
    cfg.daily_max_loss_pct = 0.05
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


# --- indicators -----------------------------------------------------------
def test_ema_matches_hand_calc():
    s = pd.Series([1.0, 2.0, 3.0])
    out = ema(s, 2).tolist()
    assert out[0] == pytest.approx(1.0)
    assert out[1] == pytest.approx(1.6667, abs=1e-3)
    assert out[2] == pytest.approx(2.5556, abs=1e-3)


def test_vwap_single_bar_is_typical_price():
    df = pd.DataFrame({"high": [10.0], "low": [10.0], "close": [10.0], "volume": [5.0]})
    assert vwap(df).iloc[0] == pytest.approx(10.0)


# --- position sizing ------------------------------------------------------
def test_plan_entry_sizing_and_stop():
    rm = RiskManager(fresh_cfg())
    # entry 50, max_stop 1% -> 0.5 ; OR opposite 1.0 -> tighter stop = 0.5
    plan = rm.plan_entry(equity=2000, cash=2000, side=LONG,
                         entry_price=50.0, or_high=51.0, or_low=49.0)
    assert plan.risk_per_share == pytest.approx(0.5)   # 1% cap is tighter
    assert plan.stop == pytest.approx(49.5)
    assert plan.shares == 40                            # floor($20 / $0.5)


def test_plan_entry_capped_by_cash():
    rm = RiskManager(fresh_cfg())
    plan = rm.plan_entry(equity=2000, cash=2000, side=LONG,
                         entry_price=100.0, or_high=101.0, or_low=99.9)
    # tiny risk/share would want many shares, but cash caps to floor(2000/100)=20
    assert plan.shares == 20


# --- daily circuit-breaker ------------------------------------------------
def test_breaker_triggers_at_exactly_5pct():
    rm = RiskManager(fresh_cfg())
    rm.start_day(2000.0)
    assert rm.check_breaker(1901.0) is False
    assert rm.check_breaker(1900.0) is True   # 5% of 2000 = 100
    assert rm.halted is True


# --- end-to-end simulation ------------------------------------------------
def test_starts_with_2000_and_flat_at_eod():
    cfg = fresh_cfg()
    port = SimEngine(cfg).run(uptrend_breakout(), "TEST")
    assert port.start_cash == 2000.0
    assert port.position is None               # never holds overnight
    assert len(port.equity_curve) > 0


def test_uptrend_scales_out_and_profits():
    cfg = fresh_cfg()
    port = SimEngine(cfg).run(uptrend_breakout(), "TEST")
    assert port.trades, "expected at least one trade on a breakout day"
    reasons = [f["reason"] for t in port.trades for f in t.exits]
    assert "scale_2R" in reasons               # half taken at +2R
    assert port.equity(0) >= 2000.0            # cash-only equity after flat


def test_crash_day_shorts_the_breakdown():
    # The bot should short a clean breakdown rather than sit out.
    port = SimEngine(fresh_cfg()).run(crash_day(), "TEST")
    assert port.trades
    assert all(t.side == SHORT for t in port.trades)


def test_per_trade_stop_caps_single_loss():
    # Long bull-trap, shorts disabled: exactly one losing trade, capped tightly.
    port = SimEngine(fresh_cfg(allow_short=False)).run(bull_trap(), "TEST")
    assert len(port.trades) == 1
    end_equity = port.equity_curve[-1][1]
    loss_pct = (2000.0 - end_equity) / 2000.0
    assert 0 < loss_pct <= 0.02            # ~1% per-trade risk, never blows up


def test_daily_breaker_halts_trading_near_5pct():
    # Sawtooth would bleed ~9% unprotected; the 5% breaker must stop it.
    cfg = fresh_cfg(allow_short=False, ema_fast=1, ema_slow=2)
    engine = SimEngine(cfg)
    port = engine.run(sawtooth_down(cycles=15), "TEST")
    end_equity = port.equity_curve[-1][1]
    loss_pct = (2000.0 - end_equity) / 2000.0
    assert engine.risk.halted is True       # circuit-breaker latched
    assert 0.05 <= loss_pct <= 0.065        # capped just past 5%, not 9%+
