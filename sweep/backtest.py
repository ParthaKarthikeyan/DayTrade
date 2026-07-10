"""Session-by-session backtest of the sweep-reversal book on 1-minute bars.

One position at a time, entries only 09:30 -> (session_end - 15min), forced
flat at session_end, fixed-fraction risk sizing capped at `max_lev` x equity,
costs charged per side in bps. The book compounds across sessions.
"""

from dataclasses import dataclass, field
from typing import List, Optional

import pandas as pd

from .levels import build_day_frames, levels_for_all_days
from .strategy import (SweepParams, make_machines, open_position,
                       step_position, Position)

BODY_WINDOW = 20                 # bars in the average-body baseline
MIN_SESSION_BARS = 60            # RTH bars required to trade the day


@dataclass
class DayResult:
    date: str
    trades: List[dict] = field(default_factory=list)
    pnl: float = 0.0
    skipped: bool = False


def _cost_adj(price: float, side: str, is_entry: bool, bps: float) -> float:
    """Adverse per-side cost: buys fill higher, sells fill lower."""
    k = bps / 10_000.0
    buying = (side == "long") == is_entry
    return price * (1 + k) if buying else price * (1 - k)


def run_session(day_bars: pd.DataFrame, levels: List[float], equity: float,
                p: SweepParams, date_str: str) -> DayResult:
    res = DayResult(date=date_str)
    rth = day_bars.between_time("09:30", p.session_end)
    if len(rth) < MIN_SESSION_BARS or not levels:
        res.skipped = True
        return res

    # Seed the body baseline with late premarket so bar 1 has context.
    pre = day_bars.between_time("08:00", "09:29").tail(BODY_WINDOW)
    bodies = list((pre["close"] - pre["open"]).abs()) if len(pre) else []

    end_t = rth.index[-1]
    cutoff = end_t - pd.Timedelta(minutes=15)   # no new entries in the last 15m
    machines = make_machines(levels)
    pos: Optional[Position] = None
    pending = None                              # signal awaiting next-bar entry
    day_risk = equity * p.risk_pct              # dollar value of 1R today
    lockout = False
    prev_close = float(rth["open"].iloc[0])

    for i, (t, bar) in enumerate(rth.iterrows()):
        o, h, l, c = (float(bar["open"]), float(bar["high"]),
                      float(bar["low"]), float(bar["close"]))
        is_last = t == end_t

        # 1) manage an open position
        if pos is not None:
            exit_px = step_position(pos, o, h, l, c, is_last, p)
            if exit_px is not None:
                fill = _cost_adj(exit_px, pos.side, False, p.cost_bps_side)
                entry_fill = pos.entry
                pnl = ((fill - entry_fill) if pos.side == "long"
                       else (entry_fill - fill)) * pos.shares
                res.trades.append({
                    "date": date_str, "time": str(t.time()), "side": pos.side,
                    "entry": round(entry_fill, 4), "exit": round(fill, 4),
                    "shares": round(pos.shares, 2), "pnl": round(pnl, 2),
                    "r": round(pnl / (pos.risk_ps * pos.shares), 2) if pos.risk_ps else 0.0,
                })
                res.pnl += pnl
                pos = None
                if res.pnl <= -p.daily_stop_r * day_risk:
                    lockout = True

        # 2) fill a pending signal at this bar's open
        if pos is None and pending is not None and not lockout and t <= cutoff:
            entry_fill = _cost_adj(o, pending.side, True, p.cost_bps_side)
            cand = open_position(pending, entry_fill, equity + res.pnl, p)
            # A gap through the stop between signal and entry kills the setup.
            ok = cand is not None and (
                (pending.side == "long" and entry_fill > cand.stop) or
                (pending.side == "short" and entry_fill < cand.stop))
            if ok:
                pos = cand
                # The entry bar itself can stop or target the trade.
                exit_px = step_position(pos, o, h, l, c, is_last, p)
                if exit_px is not None:
                    fill = _cost_adj(exit_px, pos.side, False, p.cost_bps_side)
                    pnl = ((fill - pos.entry) if pos.side == "long"
                           else (pos.entry - fill)) * pos.shares
                    res.trades.append({
                        "date": date_str, "time": str(t.time()), "side": pos.side,
                        "entry": round(pos.entry, 4), "exit": round(fill, 4),
                        "shares": round(pos.shares, 2), "pnl": round(pnl, 2),
                        "r": round(pnl / (pos.risk_ps * pos.shares), 2) if pos.risk_ps else 0.0,
                    })
                    res.pnl += pnl
                    pos = None
                    if res.pnl <= -p.daily_stop_r * day_risk:
                        lockout = True
        pending = None

        # 3) scan levels for a fresh signal (only when flat and allowed)
        avg_body = (sum(bodies[-BODY_WINDOW:]) / len(bodies[-BODY_WINDOW:])
                    if bodies else abs(c - o) or 1e-9)
        if (pos is None and not lockout and t <= cutoff
                and len(res.trades) < p.max_trades_per_day):
            for m in machines:
                sig = m.step(o, h, l, c, prev_close, avg_body, p)
                if sig is not None and (p.allow_short or sig.side == "long"):
                    pending = sig
                    break
        else:
            # Keep machine states honest even while in a trade / locked out.
            for m in machines:
                m.step(o, h, l, c, prev_close, avg_body, p)

        bodies.append(abs(c - o))
        prev_close = c

    return res


def run_book(minute_bars: pd.DataFrame, p: SweepParams,
             start_cash: float = 10_000.0, *,
             prebuilt: Optional[tuple] = None) -> dict:
    """Replay every session in the frame. Returns summary + per-day equity.

    `prebuilt` = (days, frames, levels_by_day) lets a parameter sweep compute
    the expensive day-splitting and level extraction once and share it across
    every grid cell (levels only depend on the fixed level parameters)."""
    if prebuilt is not None:
        days, by_day, levels_by_day = prebuilt
    else:
        days, by_day = build_day_frames(minute_bars)
        levels_by_day = levels_for_all_days(days, by_day,
                                            swing_sessions=p.swing_sessions,
                                            swing_k=p.swing_k, merge_bps=p.merge_bps)
    equity = start_cash
    day_rows, all_trades, skipped = [], [], 0

    for d in days:
        res = run_session(by_day[d], levels_by_day.get(d, []), equity, p, d.isoformat())
        if res.skipped:
            skipped += 1
            continue
        equity += res.pnl
        all_trades.extend(res.trades)
        day_rows.append({"date": d.isoformat(), "pnl": round(res.pnl, 2),
                         "equity": round(equity, 2), "trades": len(res.trades)})

    wins = [t for t in all_trades if t["pnl"] > 0]
    losses = [t for t in all_trades if t["pnl"] <= 0]
    gross_w = sum(t["pnl"] for t in wins)
    gross_l = -sum(t["pnl"] for t in losses)
    curve = [start_cash] + [r["equity"] for r in day_rows]
    peak, max_dd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        max_dd = max(max_dd, (peak - v) / peak * 100.0)

    return {
        "net": round(equity - start_cash, 2),
        "end_equity": round(equity, 2),
        "sessions": len(day_rows),
        "skipped": skipped,
        "trades": len(all_trades),
        "win_rate": round(100.0 * len(wins) / len(all_trades), 1) if all_trades else 0.0,
        "avg_r": round(sum(t["r"] for t in all_trades) / len(all_trades), 2) if all_trades else 0.0,
        "pf": round(gross_w / gross_l, 2) if gross_l > 0 else float("inf"),
        "max_dd_pct": round(max_dd, 2),
        "per_session": round((equity - start_cash) / len(day_rows), 2) if day_rows else 0.0,
        "days": day_rows,
        "trade_log": all_trades,
    }
