"""Offline tests for the forex backtest engine (synthetic bars, no network)."""

import pandas as pd

from forex.config import ForexConfig
from forex.data import to_oanda, _parse_candles
from forex.engine import run_orb, run_trend, run_variants


def test_to_oanda_mapping():
    assert to_oanda("EURUSD=X") == "EUR_USD"
    assert to_oanda("GBPUSD=X") == "GBP_USD"
    assert to_oanda("EUR_USD") == "EUR_USD"


def test_parse_oanda_candles():
    payload = {"candles": [
        {"complete": True, "time": "2026-06-01T07:00:00Z",
         "mid": {"o": "1.1000", "h": "1.1010", "l": "1.0995", "c": "1.1005"}},
        {"complete": False, "time": "2026-06-01T07:15:00Z",   # dropped (incomplete)
         "mid": {"o": "1.1005", "h": "1.1006", "l": "1.1004", "c": "1.1005"}},
    ]}
    df = _parse_candles(payload)
    assert len(df) == 1
    assert str(df.index.tz) == "UTC"
    assert df.iloc[0]["close"] == 1.1005


def cfg(**over):
    c = ForexConfig()
    c.start_cash = 2000.0
    c.trend_ema, c.fast_ema, c.slow_ema = 10, 3, 6   # responsive on short series
    for k, v in over.items():
        setattr(c, k, v)
    return c


def make_day(prices, date="2026-06-15", start_hour=7):
    idx = pd.date_range(f"{date} {start_hour:02d}:00", periods=len(prices),
                        freq="15min", tz="UTC")
    rows = [{"open": p, "high": p + 0.0003, "low": p - 0.0003, "close": p} for p in prices]
    return pd.DataFrame(rows, index=idx)


def test_orb_long_on_breakout_uptrend():
    prices = [1.1000] * 4 + [1.1000 + 0.0006 * i for i in range(1, 22)]
    res = run_orb(make_day(prices), cfg(), trend_filtered=False)
    assert res["trades"], "an upside breakout should trigger a long"
    assert res["trades"][0]["side"] == 1
    assert res["end"] > 2000.0


def test_orb_short_on_breakdown_downtrend():
    prices = [1.1000] * 4 + [1.1000 - 0.0006 * i for i in range(1, 22)]
    res = run_orb(make_day(prices), cfg(), trend_filtered=False)
    assert res["trades"] and res["trades"][0]["side"] == -1
    assert res["end"] > 2000.0


def test_trend_filter_blocks_counter_trend():
    # Downside breakout but price above the trend EMA -> trend_orb takes no short.
    prices = [1.1000] * 4 + [1.0998, 1.0999, 1.1001, 1.1002, 1.1003, 1.1004]
    filtered = run_orb(make_day(prices), cfg(trend_ema=50), trend_filtered=True)
    # With a long-ish trend EMA seeded high, a brief dip-breakout shouldn't flip us short.
    assert all(t["side"] == 1 for t in filtered["trades"]) or not filtered["trades"]


def test_run_variants_shape():
    prices = [1.1000] * 4 + [1.1000 + 0.0005 * i for i in range(1, 30)]
    out = run_variants(make_day(prices), cfg())
    assert set(out) == {"pure_orb", "trend_orb", "pure_trend"}
    for v in out.values():
        assert "end" in v and "ret" in v and "max_dd" in v


def test_no_trades_on_flat_day():
    res = run_orb(make_day([1.1000] * 30), cfg(), trend_filtered=False)
    assert res["trades"] == []
    assert res["end"] == 2000.0
