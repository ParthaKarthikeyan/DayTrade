"""Micro futures contract specs for realistic, integer-contract position sizing.

A $10k account can't hold fractional contracts — it trades WHOLE micros. That
granularity matters: if one micro's stop-risk already exceeds your per-trade risk
budget, you either take 1 contract (more risk than planned) or none. This module
holds the specs needed to model that honestly.

`point_value` = USD P&L per 1.00 move in the price series we backtest (the yfinance
=F / -USD series). `margin` = approximate overnight initial margin per contract.
Margins are broker- and volatility-dependent and move around — these are conservative
ballparks (verify with your broker before funding). Each sleeve maps to its micro:
ES->MES, GC->MGC, CL->MCL, 6E->M6E, BTC->MBT; the 10Y note has no true micro, so the
rates sleeve uses the full ZN contract (still affordable at $10k).
"""

# ticker -> (micro symbol, point_value $ per 1.00 price move, approx overnight margin $)
SPECS = {
    "ES=F":   ("MES", 5.0,     1600.0),   # Micro E-mini S&P 500: $5 / index point
    "GC=F":   ("MGC", 10.0,    1500.0),   # Micro Gold: $10 / $1 of gold (10 oz)
    "CL=F":   ("MCL", 100.0,   1100.0),   # Micro WTI Crude: $100 / $1 (100 bbl)
    "ZN=F":   ("ZN",  1000.0,  1800.0),   # 10Y T-Note (FULL — no micro): $1000 / point
    "6E=F":   ("M6E", 12500.0, 350.0),    # Micro EUR/USD: $12,500 / 1.00 (€12,500)
    "BTC-USD":("MBT", 0.10,    2200.0),   # Micro Bitcoin: $0.10 / $1 of BTC (0.1 BTC)
}


def contracts_for(risk_dollars: float, stop_distance: float, point_value: float) -> int:
    """Whole contracts whose stop-loss risks <= risk_dollars. Floor (never round up
    risk). Returns 0 if even one contract would exceed the budget."""
    if stop_distance <= 0 or point_value <= 0:
        return 0
    per_contract_risk = stop_distance * point_value
    return int(risk_dollars // per_contract_risk)
