"""Tests for the dashboard state hand-off."""

import json

from sim.engine import SimEngine
from sim.report import compute_metrics
from sim.state import build_state, write_state
from tests.test_sim import fresh_cfg
from tests.synthetic import uptrend_breakout


def test_build_state_is_json_serializable_and_consistent():
    port = SimEngine(fresh_cfg()).run(uptrend_breakout(), "TEST")
    state = build_state(port, mode="backtest", symbol="TEST",
                        updated_at="2026-06-26 10:00:00", halted=False)

    # Must round-trip through JSON with no non-serializable objects.
    blob = json.dumps(state)
    again = json.loads(blob)

    m = compute_metrics(port)
    assert again["mode"] == "backtest"
    assert again["symbol"] == "TEST"
    assert again["start_cash"] == 2000.0
    assert again["equity"] == round(m["end_equity"], 2)
    assert again["metrics"]["num_trades"] == m["num_trades"]
    assert len(again["equity_curve"]) == len(port.equity_curve)
    assert len(again["trades"]) == len(port.trades)
    # Each trade carries its scale-out fills.
    assert all("exits" in t for t in again["trades"])


def test_write_state_atomic(tmp_path):
    port = SimEngine(fresh_cfg()).run(uptrend_breakout(), "TEST")
    state = build_state(port, mode="backtest", symbol="TEST",
                        updated_at="2026-06-26 10:00:00", halted=False)
    path = tmp_path / "nested" / "state.json"
    write_state(str(path), state)
    assert path.exists()
    with open(path) as f:
        assert json.load(f)["symbol"] == "TEST"
