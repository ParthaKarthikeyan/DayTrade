"""Premarket gap-and-go momentum strategy ($1-$10 listed small caps).

Long-only intraday momentum: each morning, scan for the biggest gappers, watch
them from premarket through the open + 2 hours, buy breakouts to new highs while
above VWAP, and cut instantly on any sign of weakness. Validated by a free
yfinance backtest (recent ~2 weeks) and, going forward, live paper trading.
"""
