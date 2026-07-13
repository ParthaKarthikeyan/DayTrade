"""Offline tests for the premarket gap-and-go logic (no network)."""

import copy

import pandas as pd
import pytest

from premarket.backtest import simulate_symbol_day
from premarket.prescan import count_symbol_headlines, rank_candidates
from premarket.scanner import rank_gappers
from premarket.strategy import (FirstPullback, PMPosition, PremarketMomentum,
                                make_strategy, split_signal)
from sim.config import CONFIG


def cfg(**over):
    c = copy.copy(CONFIG)
    c.start_cash = 2000.0
    c.risk_per_trade_pct = 0.01
    c.pm_stop_pct = 0.05
    c.pm_min_breakout_vol_mult = 1.0
    c.pm_price_min, c.pm_price_max, c.pm_min_gap_pct, c.pm_top_n = 1.0, 10.0, 5.0, 5
    c.daily_max_loss_pct = 0.05
    c.pm_strategy = "flag"      # legacy tests exercise the flag entry
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


# --- overnight prescan (pure parts) ---------------------------------------
def test_count_symbol_headlines_newest_first():
    articles = [  # newest first, as the API returns them
        {"headline": "FDA approves", "symbols": ["ABC", "XYZ"]},
        {"headline": "Offering priced", "symbols": ["ABC"]},
    ]
    out = count_symbol_headlines(articles)
    assert out["ABC"] == {"count": 2, "headline": "FDA approves"}
    assert out["XYZ"]["count"] == 1


def test_is_common_stock_rejects_foreign_prefixed():
    from premarket.scanner import is_common_stock
    # exchange-prefixed news symbols 400 the snapshot batch if let through
    assert not is_common_stock("TSX:ATZ")
    assert not is_common_stock("BRK.B")
    assert not is_common_stock("OPTXW")
    assert is_common_stock("VRAX")


def test_rank_candidates_filters_and_sorts():
    c = cfg()  # band $1-$10, prescan min gap 3.0
    news = {s: {"count": 3, "headline": "h"} for s in
            ["GAPS", "BIGG", "FLAT", "ABCDW", "NOSNAP", "RUNNER"]}
    snaps = {
        "GAPS":  {"price": 4.00, "prev_close": 3.50},   # +14% keep
        "BIGG":  {"price": 50.0, "prev_close": 40.0},   # out of band
        "FLAT":  {"price": 3.00, "prev_close": 2.99},   # catalyst, no move
        "ABCDW": {"price": 2.00, "prev_close": 1.00},   # warrant-style ticker
        "RUNNER": {"price": 5.00, "prev_close": 4.00},  # +25% keep, ranks first
    }
    out = rank_candidates(news, snaps, c)
    assert [r["symbol"] for r in out] == ["RUNNER", "GAPS"]


# --- strategy primitives -------------------------------------------------
def test_entry_signal_bull_flag_breakout():
    s = PremarketMomentum(cfg())   # pm_pullback_lookback = 3
    flag = [{"o": 3.10, "h": 3.12, "l": 3.05, "c": 3.08, "v": 1000},
            {"o": 3.08, "h": 3.11, "l": 3.06, "c": 3.09, "v": 1000},
            {"o": 3.09, "h": 3.12, "l": 3.07, "c": 3.10, "v": 1000}]
    breakout = {"o": 3.10, "h": 3.20, "l": 3.09, "c": 3.18, "v": 3000}
    window = flag + [breakout]
    stop = s.entry_signal(window, vwap=3.00, vol_avg=1000)
    assert stop == pytest.approx(3.05)                       # = flag low
    assert s.entry_signal(window, vwap=3.50, vol_avg=1000) is None    # below VWAP
    assert s.entry_signal(window, vwap=3.00, vol_avg=9999) is None    # weak volume
    no_break = {**breakout, "h": 3.11, "c": 3.10}
    assert s.entry_signal(flag + [no_break], vwap=3.00, vol_avg=1000) is None  # no new high


def test_exit_priority():
    s = PremarketMomentum(cfg())
    pos = PMPosition("X", entry=3.0, entry_time=None, shares=100, stop=2.9, high_water=3.0)
    assert s.exit_reason(pos, {"l": 2.85, "c": 2.95}, {"l": 2.9}, 2.95) == "stop"
    assert s.exit_reason(pos, {"l": 2.95, "c": 2.88}, {"l": 2.90}, 2.99) == "momentum_break"
    assert s.exit_reason(pos, {"l": 2.95, "c": 2.93}, {"l": 2.80}, 2.99) == "lost_vwap"
    assert s.exit_reason(pos, {"l": 2.95, "c": 3.05}, {"l": 2.80}, 2.99) is None


# --- first-pullback (Ross Cameron) setup ----------------------------------
def fpb(o, h, l, c, v=1000):
    return {"o": o, "h": h, "l": l, "c": c, "v": v}


def fp_hist(pull_lows=(3.32, 3.26), entry=fpb(3.28, 3.37, 3.27, 3.31, 3000)):
    """Flat base -> 3-bar squeeze to 3.52 -> two red pullback bars -> entry bar."""
    base = [fpb(3.0, 3.02, 2.98, 3.0)] * 6
    leg = [fpb(3.00, 3.17, 2.99, 3.15, 2000),
           fpb(3.15, 3.32, 3.14, 3.30, 2500),
           fpb(3.30, 3.52, 3.29, 3.50, 3000)]
    pull = [fpb(3.50, 3.50, pull_lows[0], pull_lows[0] + 0.02, 1200),
            fpb(3.34, 3.35, pull_lows[1], pull_lows[1] + 0.02, 1000)]
    return base + leg + pull + [entry]


def test_first_pullback_signal():
    s = FirstPullback(cfg())
    sig = s.entry_signal(fp_hist(), vwap=3.10, vol_avg=1500)
    assert sig is not None
    assert sig["stop"] == pytest.approx(3.26)     # low of the pullback
    assert sig["target"] == pytest.approx(3.52)   # retest of the session high


def test_first_pullback_rejects_deep_retracement():
    # pullback below 50% of the 2.98 -> 3.52 leg (3.25) = damaged goods, skip
    s = FirstPullback(cfg())
    hist = fp_hist(pull_lows=(3.30, 3.10),
                   entry=fpb(3.12, 3.40, 3.11, 3.35, 3000))
    assert s.entry_signal(hist, vwap=3.10, vol_avg=1500) is None


def test_first_pullback_needs_a_red_candle():
    s = FirstPullback(cfg())
    hist = fp_hist()
    for b in hist[-3:-1]:                          # make the pullback all-green
        b["c"] = b["o"] + 0.01
    assert s.entry_signal(hist, vwap=3.10, vol_avg=1500) is None


def test_first_pullback_needs_two_to_one():
    # entry so close to the session high that the retest pays < 2R -> skip
    s = FirstPullback(cfg())
    hist = fp_hist(pull_lows=(3.46, 3.44),
                   entry=fpb(3.46, 3.51, 3.45, 3.50, 3000))
    assert s.entry_signal(hist, vwap=3.10, vol_avg=1500) is None


def test_first_pullback_macd_gate():
    s = FirstPullback(cfg())
    falling = [{"c": 10.0 - i * 0.05} for i in range(40)]
    rising = [{"c": 5.0 + i * 0.05} for i in range(40)]
    assert not s._macd_ok(falling)
    assert s._macd_ok(rising)
    assert s._macd_ok(falling[:10])                # too few bars: gate skipped


def test_first_pullback_target_exit():
    s = FirstPullback(cfg())
    pos = PMPosition("X", entry=3.31, entry_time=None, shares=100, stop=3.26,
                     high_water=3.31, target=3.52)
    assert s.exit_reason(pos, {"h": 3.53, "l": 3.42, "c": 3.50}, {"l": 3.30}, 3.2) == "target"
    assert s.exit_reason(pos, {"h": 3.40, "l": 3.35, "c": 3.38}, {"l": 3.30}, 3.2) is None


def test_make_strategy_and_split_signal():
    assert isinstance(make_strategy(cfg(pm_strategy="first_pullback")), FirstPullback)
    flag = make_strategy(cfg())
    assert isinstance(flag, PremarketMomentum) and not isinstance(flag, FirstPullback)
    assert split_signal(3.05) == (3.05, None)
    assert split_signal({"stop": 3.26, "target": 3.52}) == (3.26, 3.52)


def test_first_pullback_day_end_to_end():
    rows = fp_hist() + [fpb(3.31, 3.45, 3.30, 3.43, 2500),
                        fpb(3.43, 3.55, 3.42, 3.53, 2500)]
    idx = pd.date_range("2026-06-26 07:30", periods=len(rows), freq="1min",
                        tz="America/New_York")
    bars = [{"t": t, **r} for t, r in zip(idx, rows)]
    trades, end_eq, halted = simulate_symbol_day(
        bars, "TST", 2000.0, cfg(pm_strategy="first_pullback"))
    assert trades and trades[0]["reason"] == "target"
    assert end_eq > 2000.0 and not halted


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
