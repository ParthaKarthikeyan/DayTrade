"""Free premarket gap-and-go backtest using yfinance (recent ~30 days).

Pipeline per day:
1. Reconstruct the scan: gap % = regular open / prior close, over the universe;
   keep $1-$10 names gapping >= the threshold; rank; take the top mover with
   usable intraday data (one focused position for a small account).
2. Pull that name's 1-minute bars INCLUDING premarket (yfinance prepost=True),
   restricted to the session window (07:00 -> 11:35 ET).
3. Trade it long-only with the shared PremarketMomentum rules, allowing multiple
   re-entries (multiple trades per day), instant-cut exits, and a daily loss cap.
4. Compound the account day to day.

Honest caveats: regular-open is a proxy for the premarket gap; small-cap minute
data on Yahoo is patchy (thin days are skipped and counted); the universe is a
fixed list, so news gappers outside it are invisible. Treat as indicative.
"""

from datetime import timedelta
from typing import List, Optional, Tuple

import pandas as pd

from sim.config import Config
from sim.engine import _parse_hhmm
from .strategy import PMPosition, PremarketMomentum

PM_SLIPPAGE = 0.0025          # 25 bps — small-cap spreads; still optimistic vs reality
VOL_LOOKBACK = 5             # bars used for the breakout volume average
MIN_SESSION_BARS = 20        # below this, the day's data is too thin to trust


# ----------------------------- data (yfinance) -----------------------------
def load_daily(universe: List[str], start: str, end: str) -> pd.DataFrame:
    import yfinance as yf
    fetch_start = (pd.Timestamp(start) - pd.Timedelta(days=10)).date().isoformat()
    fetch_end = (pd.Timestamp(end) + pd.Timedelta(days=1)).date().isoformat()
    return yf.download(universe, start=fetch_start, end=fetch_end, interval="1d",
                       auto_adjust=False, progress=False, group_by="ticker",
                       threads=True)


def _daily_col(daily: pd.DataFrame, sym: str, field: str) -> Optional[pd.Series]:
    try:
        if isinstance(daily.columns, pd.MultiIndex):
            return daily[sym][field]
        return daily[field]
    except (KeyError, IndexError):
        return None


def load_intraday(symbol: str, day: pd.Timestamp, cfg: Config) -> List[dict]:
    import yfinance as yf
    start = day.date().isoformat()
    end = (day + pd.Timedelta(days=1)).date().isoformat()
    df = yf.download(symbol, start=start, end=end, interval="1m", prepost=True,
                     auto_adjust=False, progress=False)
    if df.empty:
        return []
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    idx = df.index
    df = df.tz_convert(cfg.timezone) if idx.tz is not None else df.tz_localize("UTC").tz_convert(cfg.timezone)
    df = df.between_time(cfg.pm_session_start, cfg.pm_flatten_at)
    bars = []
    for t, r in df.iterrows():
        try:
            bars.append({"t": t, "o": float(r["Open"]), "h": float(r["High"]),
                         "l": float(r["Low"]), "c": float(r["Close"]),
                         "v": float(r["Volume"])})
        except (TypeError, ValueError):
            continue
    return [b for b in bars if b["c"] == b["c"]]  # drop NaN closes


# ----------------------------- scan reconstruction -------------------------
def watchlist_for_day(daily: pd.DataFrame, universe: List[str], day: pd.Timestamp,
                      prev_day: pd.Timestamp, cfg: Config) -> List[dict]:
    from .scanner import rank_gappers
    rows = []
    for sym in universe:
        op = _daily_col(daily, sym, "Open")
        cl = _daily_col(daily, sym, "Close")
        if op is None or cl is None or day not in op.index or prev_day not in cl.index:
            continue
        o, pc = op.get(day), cl.get(prev_day)
        if not (o == o and pc == pc) or pc <= 0:   # NaN / bad
            continue
        rows.append({"symbol": sym, "ref_price": float(o),
                     "gap_pct": (float(o) / float(pc) - 1.0) * 100.0})
    return rank_gappers(rows, cfg)


# ----------------------------- per-day simulation --------------------------
def simulate_symbol_day(bars: List[dict], symbol: str, start_equity: float,
                        cfg: Config) -> Tuple[List[dict], float, bool]:
    strat = PremarketMomentum(cfg)
    no_new = _parse_hhmm(cfg.pm_no_new_after)
    flatten = _parse_hhmm(cfg.pm_flatten_at)

    equity = start_equity
    realized = 0.0
    pos: Optional[PMPosition] = None
    trades: List[dict] = []
    halted = False
    session_high = -float("inf")
    cum_pv = cum_vol = 0.0
    prev_bar = None

    def close(price, reason, t):
        nonlocal pos, realized, equity
        fill = price * (1 - PM_SLIPPAGE)
        pnl = pos.shares * (fill - pos.entry)
        realized += pnl
        equity = start_equity + realized
        trades.append({"symbol": symbol, "entry_time": pos.entry_time, "exit_time": t,
                       "entry": pos.entry, "shares": pos.shares, "pnl": pnl, "reason": reason})
        pos = None

    for i, bar in enumerate(bars):
        tp = (bar["h"] + bar["l"] + bar["c"]) / 3.0
        cum_pv += tp * bar["v"]; cum_vol += bar["v"]
        vwap = cum_pv / cum_vol if cum_vol > 0 else None
        vol_avg = (sum(b["v"] for b in bars[max(0, i - VOL_LOOKBACK):i]) /
                   min(i, VOL_LOOKBACK)) if i >= VOL_LOOKBACK else None
        t = bar["t"]

        # 1) manage open position
        if pos is not None:
            if t.time() >= flatten or halted:
                close(bar["c"], "breaker" if halted else "time_stop", t)
            else:
                reason = strat.exit_reason(pos, bar, prev_bar, vwap)
                if reason == "stop":
                    close(pos.stop, "stop", t)
                elif reason:
                    close(bar["c"], reason, t)
                elif bar["c"] > pos.entry:                 # in profit -> trail
                    if prev_bar is not None:
                        pos.stop = max(pos.stop, prev_bar["l"])
                    pos.high_water = max(pos.high_water, bar["h"])

        # 2) daily loss cap
        if not halted and equity <= start_equity * (1 - cfg.daily_max_loss_pct):
            halted = True
            if pos is not None:
                close(bar["c"], "breaker", t)

        # 3) entry (one position, before the cutoff)
        if (pos is None and not halted and t.time() <= no_new and vol_avg is not None
                and strat.entry_long(bar, session_high, vwap, vol_avg)):
            entry = bar["c"] * (1 + PM_SLIPPAGE)
            stop = strat.initial_stop(entry, bar["l"])
            rps = entry - stop
            if rps > 0:
                shares = int((equity * cfg.risk_per_trade_pct) // rps)
                shares = min(shares, int(equity // entry))
                if shares >= 1:
                    pos = PMPosition(symbol=symbol, entry=entry, entry_time=t,
                                     shares=shares, stop=stop, high_water=entry)

        session_high = max(session_high, bar["h"])
        prev_bar = bar

    if pos is not None:
        close(bars[-1]["c"], "eod", bars[-1]["t"])
    return trades, start_equity + realized, halted


# ----------------------------- driver --------------------------------------
def run(cfg: Config, universe: List[str], start: str, end: str) -> dict:
    daily = load_daily(universe, start, end)
    # trading days = daily index within [start, end], with a prior day available
    all_days = [d for d in daily.index if pd.Timestamp(start) <= d <= pd.Timestamp(end)]
    notes = {"thin_or_missing": 0, "days_no_candidate": 0}
    equity = cfg.start_cash
    per_day, trades = [], []

    for k, day in enumerate(all_days):
        prev_day = daily.index[daily.index.get_loc(day) - 1] if daily.index.get_loc(day) > 0 else None
        if prev_day is None:
            continue
        wl = watchlist_for_day(daily, universe, day, prev_day, cfg)
        chosen, bars = None, None
        for cand in wl:
            b = load_intraday(cand["symbol"], day, cfg)
            if len(b) >= MIN_SESSION_BARS:
                chosen, bars = cand, b
                break
            notes["thin_or_missing"] += 1
        if chosen is None:
            notes["days_no_candidate"] += 1
            per_day.append({"date": day.date().isoformat(), "symbol": None,
                            "trades": 0, "pnl": 0.0, "ret": 0.0, "halted": False})
            continue
        day_trades, end_eq, halted = simulate_symbol_day(bars, chosen["symbol"], equity, cfg)
        per_day.append({"date": day.date().isoformat(), "symbol": chosen["symbol"],
                        "gap": round(chosen["gap_pct"], 1), "trades": len(day_trades),
                        "pnl": end_eq - equity, "ret": (end_eq / equity - 1) * 100 if equity else 0,
                        "halted": halted})
        trades += day_trades
        equity = end_eq

    return summarize(cfg, per_day, trades, notes, start, end)


def summarize(cfg, per_day, trades, notes, start, end) -> dict:
    eq = [cfg.start_cash]
    for p in per_day:
        eq.append(eq[-1] + p["pnl"])
    peak, mdd = eq[0], 0.0
    for e in eq:
        peak = max(peak, e); mdd = max(mdd, (peak - e) / peak if peak else 0.0)
    wins = [t for t in trades if t["pnl"] > 0]
    active = [p for p in per_day if p["symbol"]]
    return {
        "start": start, "end": end, "start_cash": cfg.start_cash,
        "final_equity": eq[-1], "total_return": (eq[-1] / cfg.start_cash - 1) * 100,
        "days": len(per_day), "days_traded": len(active),
        "trades": len(trades), "win_rate": (len(wins) / len(trades) * 100) if trades else 0,
        "green_days": sum(1 for p in active if p["pnl"] > 0),
        "red_days": sum(1 for p in active if p["pnl"] < 0),
        "best_day": max((p["ret"] for p in active), default=0),
        "worst_day": min((p["ret"] for p in active), default=0),
        "max_drawdown": mdd * 100, "breaker_days": sum(1 for p in per_day if p["halted"]),
        "notes": notes, "per_day": per_day,
    }
