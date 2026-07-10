"""Offline tests for the QQQ sweep-reversal book (sweep/)."""

import pandas as pd
import pytest

from sweep.levels import Level, merge_levels, swing_levels, build_day_frames, levels_for_all_days
from sweep.strategy import (SweepParams, LevelMachine, Signal, open_position,
                            step_position, IDLE, DEAD)
from sweep.backtest import run_session, run_book

ET = "America/New_York"
P = SweepParams()


# ----------------------------- helpers -----------------------------
def bar(o, h, l, c, v=1000.0):
    return {"open": o, "high": h, "low": l, "close": c, "volume": v}


def frame(bars, start="2026-06-01 09:30", freq="1min"):
    idx = pd.date_range(start=start, periods=len(bars), freq=freq, tz=ET)
    return pd.DataFrame(bars, index=idx)


def flat_bars(n, px=100.3, body=0.02):
    return [bar(px, px + body, px - body, px + (body if i % 2 else -body))
            for i in range(n)]


# ----------------------------- levels -----------------------------
def test_merge_levels_collapses_nearby():
    lvls = [Level(100.00, "swing"), Level(100.03, "swing"), Level(101.0, "swing")]
    out = merge_levels(lvls, ref_price=100.0, merge_bps=5.0)   # tol = 0.05
    assert len(out) == 2
    assert out[0] == pytest.approx(100.015)
    assert out[1] == 101.0


def test_swing_levels_finds_local_extremes():
    rows = [bar(100, 100.5, 99.5, 100),
            bar(100, 101.0, 99.8, 100.5),
            bar(100.5, 102.0, 100.2, 101.5),   # local high 102
            bar(101, 101.2, 99.0, 99.5),       # local low 99
            bar(99.5, 100.8, 99.2, 100.2),
            bar(100.2, 101.0, 99.8, 100.5)]
    b15 = frame(rows, freq="15min")
    lvls = swing_levels(b15, k=2)
    prices = {round(l.price, 1) for l in lvls}
    assert 102.0 in prices and 99.0 in prices


def test_session_levels_use_only_past_data():
    # Two sessions; day 2's levels must include day 1's RTH high/low/close.
    d1 = flat_bars(390)
    d1[100] = bar(100.3, 105.0, 100.2, 100.4)      # day-1 high 105
    d1[200] = bar(100.3, 100.4, 95.0, 100.2)       # day-1 low 95
    day1 = frame(d1, start="2026-06-01 09:30")
    day2 = frame(flat_bars(120), start="2026-06-02 08:00")
    allbars = pd.concat([day1, day2])
    days, frames = build_day_frames(allbars)
    lvls = levels_for_all_days(days, frames)
    assert lvls[days[0]] == []                     # first day: nothing prior
    day2_lvls = lvls[days[1]]
    assert any(abs(x - 105.0) < 0.2 for x in day2_lvls)
    assert any(abs(x - 95.0) < 0.2 for x in day2_lvls)


# ----------------------------- state machine -----------------------------
def test_sweep_then_strong_reclaim_signals_long():
    m = LevelMachine(100.0)
    # pierce below (prev_close above the level)
    assert m.step(100.3, 100.3, 99.85, 99.9, prev_close=100.3, avg_body=0.02, p=P) is None
    # strong bullish reclaim
    sig = m.step(99.9, 100.2, 99.88, 100.15, prev_close=99.9, avg_body=0.02, p=P)
    assert isinstance(sig, Signal) and sig.side == "long"
    assert sig.sweep_extreme == pytest.approx(99.85)
    assert m.state == DEAD                          # one shot per level per day


def test_weak_reclaim_resets_to_idle():
    m = LevelMachine(100.0)
    m.step(100.3, 100.3, 99.85, 99.9, prev_close=100.3, avg_body=0.5, p=P)
    sig = m.step(99.9, 100.05, 99.88, 100.02, prev_close=99.9, avg_body=0.5, p=P)
    assert sig is None and m.state == IDLE          # body too small vs avg 0.5


def test_deep_close_kills_the_level():
    m = LevelMachine(100.0)
    m.step(100.3, 100.3, 99.85, 99.9, prev_close=100.3, avg_body=0.02, p=P)
    # closes > 25 bps below the level -> genuine breakdown
    assert m.step(99.9, 99.9, 99.5, 99.6, prev_close=99.9, avg_body=0.02, p=P) is None
    assert m.state == DEAD
    # later reclaim must NOT signal
    assert m.step(99.6, 100.3, 99.6, 100.25, prev_close=99.6, avg_body=0.02, p=P) is None


def test_short_side_mirrors():
    m = LevelMachine(100.0)
    m.step(99.7, 100.15, 99.7, 100.1, prev_close=99.7, avg_body=0.02, p=P)
    sig = m.step(100.1, 100.12, 99.8, 99.85, prev_close=100.1, avg_body=0.02, p=P)
    assert isinstance(sig, Signal) and sig.side == "short"
    assert sig.sweep_extreme == pytest.approx(100.15)


# ----------------------------- position math -----------------------------
def test_open_position_sizing_respects_leverage_cap():
    sig = Signal("long", 100.0, 99.85)
    pos = open_position(sig, entry=100.16, equity=10_000.0, p=P)
    # risk sizing would want ~300 shares; 2x-lev cap allows ~199
    assert pos.shares == pytest.approx(2 * 10_000 / 100.16, rel=1e-6)
    assert pos.stop < 99.85 < pos.entry
    assert pos.target == pytest.approx(pos.entry + 2 * pos.risk_ps)


def test_stop_beats_target_when_bar_touches_both():
    sig = Signal("long", 100.0, 99.85)
    pos = open_position(sig, entry=100.16, equity=10_000.0, p=P)
    exit_px = step_position(pos, 100.1, 101.0, 99.5, 100.9, is_last_bar=False, p=P)
    assert exit_px == pytest.approx(min(100.1, pos.stop))   # conservative: stop first


def test_breakeven_bump_applies_next_bar():
    p = SweepParams(be_at_1r=True)
    sig = Signal("long", 100.0, 99.85)
    pos = open_position(sig, entry=100.16, equity=10_000.0, p=p)
    one_r = pos.entry + pos.risk_ps
    # bar reaches +1R but not target: no exit, bump flagged
    assert step_position(pos, 100.2, one_r + 0.01, 100.15, one_r, False, p) is None
    assert pos.bump_next_bar and not pos.at_breakeven
    # next bar dips to entry: stopped at breakeven, not the original stop
    exit_px = step_position(pos, one_r, one_r, pos.entry - 0.01, pos.entry, False, p)
    assert exit_px == pytest.approx(pos.entry)


# ----------------------------- session replay -----------------------------
def winning_long_day():
    """Flat -> sweep of 100 -> strong reclaim -> rally through the 2R target."""
    bars = flat_bars(10)
    bars.append(bar(100.3, 100.3, 99.85, 99.9))        # pierce
    bars.append(bar(99.9, 100.2, 99.88, 100.15))       # strong reclaim
    bars.append(bar(100.15, 100.3, 100.1, 100.25))     # entry bar (fills at open)
    bars += [bar(100.25 + i * 0.1, 100.35 + i * 0.1, 100.2 + i * 0.1,
                 100.3 + i * 0.1) for i in range(8)]   # rally through target
    bars += flat_bars(50, px=101.0)
    return frame(bars)


def test_run_session_takes_and_wins_the_long():
    res = run_session(winning_long_day(), levels=[100.0], equity=10_000.0,
                      p=P, date_str="2026-06-01")
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t["side"] == "long" and t["pnl"] > 0
    assert 1.5 < t["r"] <= 2.0                          # ~2R minus costs
    assert res.pnl == pytest.approx(t["pnl"])


def test_run_session_respects_session_end_flatten():
    p = SweepParams(rr=50.0)                            # target unreachably far
    res = run_session(winning_long_day(), levels=[100.0], equity=10_000.0,
                      p=p, date_str="2026-06-01")
    assert len(res.trades) == 1                         # closed by forced flatten
    assert res.trades[0]["time"] >= "10:30"             # exit at the last bar


def test_run_book_compounds_and_skips_first_day():
    # Day 1 flat AT 100.0 so its derived levels form one zone at ~100.0 —
    # the level day 2's scripted sweep actually pierces and reclaims.
    d1 = frame(flat_bars(390, px=100.0), start="2026-06-01 09:30")
    d2 = winning_long_day().set_axis(
        pd.date_range("2026-06-02 09:30", periods=len(winning_long_day()),
                      freq="1min", tz=ET))
    out = run_book(pd.concat([d1, d2]), SweepParams(), start_cash=10_000.0)
    # day 1 has no prior session -> no levels -> skipped
    assert out["sessions"] == 1
    assert out["skipped"] == 1
    assert out["trades"] >= 1
    assert out["end_equity"] > 10_000.0
