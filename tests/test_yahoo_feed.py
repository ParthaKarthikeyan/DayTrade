"""Offline tests for the Yahoo polling feed (pure filtering logic)."""

from datetime import time as dtime

import pandas as pd

from premarket.yahoo_feed import new_completed_bars

ET = "America/New_York"
START, END = dtime(7, 0), dtime(11, 35)


def make_df(times, base=5.0):
    idx = pd.DatetimeIndex([pd.Timestamp(f"2026-07-13 {t}", tz=ET) for t in times])
    return pd.DataFrame({"Open": base, "High": base + 0.05, "Low": base - 0.05,
                         "Close": base + 0.02, "Volume": 1000.0}, index=idx)


def test_dispatches_only_completed_session_bars():
    df = make_df(["06:59", "07:00", "07:01", "07:02"])
    now = pd.Timestamp("2026-07-13 07:02:30", tz=ET)
    bars = new_completed_bars(df, None, now, START, END)
    # 06:59 is before the session; 07:02 is still forming at 07:02:30
    assert [b["t"].strftime("%H:%M") for b in bars] == ["07:00", "07:01"]


def test_after_cursor_prevents_duplicates():
    df = make_df(["07:00", "07:01", "07:02"])
    now = pd.Timestamp("2026-07-13 07:05:00", tz=ET)
    first = new_completed_bars(df, None, now, START, END)
    again = new_completed_bars(df, first[-1]["t"], now, START, END)
    assert len(first) == 3 and again == []


def test_bar_completion_needs_full_minute():
    df = make_df(["09:30"])
    just_before = pd.Timestamp("2026-07-13 09:30:59", tz=ET)
    just_after = pd.Timestamp("2026-07-13 09:31:00", tz=ET)
    assert new_completed_bars(df, None, just_before, START, END) == []
    assert len(new_completed_bars(df, None, just_after, START, END)) == 1


def test_handles_naive_index_and_nan_close():
    idx = pd.DatetimeIndex([pd.Timestamp("2026-07-13 08:00"),
                            pd.Timestamp("2026-07-13 08:01")])
    df = pd.DataFrame({"Open": [5.0, 5.0], "High": [5.1, 5.1],
                       "Low": [4.9, 4.9], "Close": [5.05, float("nan")],
                       "Volume": [100.0, 100.0]}, index=idx)
    now = pd.Timestamp("2026-07-13 09:00", tz=ET)
    bars = new_completed_bars(df, None, now, START, END)
    assert len(bars) == 1 and bars[0]["c"] == 5.05


def test_empty_frame_is_no_bars():
    assert new_completed_bars(None, None,
                              pd.Timestamp("2026-07-13 09:00", tz=ET),
                              START, END) == []
