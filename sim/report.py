"""Turn a finished Portfolio into human-readable performance metrics."""

from typing import Dict

from .portfolio import Portfolio


def compute_metrics(port: Portfolio) -> Dict:
    trades = port.trades
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl < 0]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = -sum(t.pnl for t in losses)

    equity = [e for _, e in port.equity_curve] or [port.start_cash]
    peak = equity[0]
    max_dd = 0.0
    for e in equity:
        peak = max(peak, e)
        max_dd = max(max_dd, (peak - e) / peak if peak else 0.0)

    end_equity = equity[-1]
    breaker_fired = any(
        f["reason"] == "breaker" for t in trades for f in t.exits)

    return {
        "start_equity": port.start_cash,
        "end_equity": end_equity,
        "total_return_pct": (end_equity / port.start_cash - 1.0) * 100.0,
        "num_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": (len(wins) / len(trades) * 100.0) if trades else 0.0,
        "avg_win": (gross_win / len(wins)) if wins else 0.0,
        "avg_loss": (-gross_loss / len(losses)) if losses else 0.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss else float("inf"),
        "largest_loss": min((t.pnl for t in trades), default=0.0),
        "max_drawdown_pct": max_dd * 100.0,
        "breaker_fired": breaker_fired,
    }


def format_report(port: Portfolio, symbol: str, date: str) -> str:
    m = compute_metrics(port)
    lines = []
    lines.append(f"\n===== Backtest: {symbol}  {date} =====")
    lines.append(f"Start equity      : ${m['start_equity']:,.2f}")
    lines.append(f"End equity        : ${m['end_equity']:,.2f}")
    lines.append(f"Total return      : {m['total_return_pct']:+.2f}%")
    lines.append(f"Trades            : {m['num_trades']} "
                 f"(W {m['wins']} / L {m['losses']}, win rate {m['win_rate_pct']:.1f}%)")
    lines.append(f"Avg win / loss    : ${m['avg_win']:,.2f} / ${m['avg_loss']:,.2f}")
    pf = m['profit_factor']
    lines.append(f"Profit factor     : {'inf' if pf == float('inf') else f'{pf:.2f}'}")
    lines.append(f"Largest loss      : ${m['largest_loss']:,.2f}")
    lines.append(f"Max drawdown      : {m['max_drawdown_pct']:.2f}%")
    lines.append(f"5% breaker fired  : {'YES' if m['breaker_fired'] else 'no'}")

    if port.trades:
        lines.append("\n--- Trades ---")
        lines.append(f"{'entry':<20}{'side':<6}{'shares':>7}{'entry$':>10}"
                     f"{'pnl$':>10}  exits")
        for t in port.trades:
            entry = str(t.entry_time)[:19]
            exits = ", ".join(
                f"{f['reason']}@{f['price']:.2f}x{int(f['qty'])}" for f in t.exits)
            lines.append(f"{entry:<20}{t.side:<6}{int(t.shares):>7}"
                         f"{t.entry_price:>10.2f}{t.pnl:>10.2f}  {exits}")
    return "\n".join(lines)
