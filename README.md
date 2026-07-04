# DayTrade — Paper-Trading Bots Chasing $100/Day on $10k

The goal: **+1%/day on a $10k account (~$100/day)**. Nobody sustains that blindly —
so this repo runs several candidate **$10k paper books in parallel**, measures each
one's FORWARD record honestly (real fills, committed ledgers, no backtest flattery),
and only promotes a book that proves itself.

## The books (each trades a notional $10k)

| Book | What it does | Schedule | Ledger |
|---|---|---|---|
| **Premarket stocks** | Momentum on low-float gappers, live Alpaca paper. Entry style (`pm_strategy`, since 2026-07-04): **first pullback** — squeeze → 1–5 red-candle pullback holding ≥50% of the leg → first candle to make a new high; stop = pullback low, target = HOD retest at ≥2:1, MACD gate ("flag" breakout entry still selectable). Daily governor: bank the day at **+1.5%**, hard stop at **-2%**, max 5 trades. Marketable-limit orders only. | weekdays 07:00–11:35 ET | `paper_ledger/premarket_*.json` |
| **Crypto 6h trend** | The research-validated 6h Donchian/EMA trend on BTC+ETH (the only family that survived cost-aware walk-forward), long/flat, live Alpaca crypto paper. Trades 24/7 — the book that can earn on weekends. | hourly cron | `paper_ledger/crypto_*.json` |
| **Futures trend** | Diversified daily trend across S&P/gold/crude/EUR/BTC micros, deterministic replay, 5%+8% risk books. A slow diversifier, not a daily-profit engine. | weekdays 22:15 UTC | `paper_ledger/futures_*.json` |

## Mission-control dashboard

**https://parthakarthikeyan.github.io/DayTrade/** — equity curves per book, daily
P&L vs the $100 goal line, trade tables with broker-fill reconciliation, workflow
status, and a near-live feed during sessions (bots push state to a gist every
~60s). Served from `docs/` by GitHub Pages; data comes straight from the committed
ledgers, so it updates after every bot run with no server to maintain.

## Promotion policy

**Validation window: the 10 trading sessions 2026-07-06 → 2026-07-17; go-live
decision that weekend; live trading starts 2026-07-20.** A book goes live only
with positive net expectancy on actual fills and drawdown inside its budget over
the window; books that fail stay paper. Ten sessions is thin evidence — the
compensating control is that live size starts small, keeps the same daily loss
caps, and only grows on live results. Backtests choose candidates; only the
forward ledger promotes them. *(Amended 2026-07-04 from ≥30 sessions: hard
deadline set by the operator.)*
(The US PDT rule was abolished effective 2026-06-04 — FINRA Regulatory Notice
26-10 replaced it with risk-based intraday margin — so a real $10k stock account
may day-trade freely, subject to the broker's phase-in. Verify with the broker
before any live deployment.)

---

# The original ORB bot + simulator (below)

An automated **stock** day-trading bot built around the classic **Opening Range
Breakout (ORB)** strategy, with a hard two-layer risk engine (never lose more
than ~5% of the account in a day) and a **real-time simulator seeded with $2000**
so you can backtest it on a real trading day's 1-minute data before risking
anything.

## How it works (the whole loop)

1. Pull a day's 1-minute bars (Alpaca, or yfinance offline).
2. Build the **opening range** = high/low of the first 15 minutes (09:30–09:45 ET).
3. Each new bar, check the **entry rules**. If they fire, size the position from
   the risk budget and enter.
4. Manage the open trade bar-by-bar: move the stop, scale out at profit, trail
   the runner.
5. Flatten everything by 15:55 — **no overnight positions**.
6. The **daily circuit-breaker** halts trading the moment the account is down 5%.

The exact same `sim/strategy.py` + `sim/risk.py` code runs in both the backtest
(`run_backtest.py`) and the live paper loop (`run_paper.py`).

## The strategy (entry rules)

Trades 1-minute bars during regular hours only. `EMA9`/`EMA20` are the fast/slow
exponential moving averages; `VWAP` is the session volume-weighted price.

| Direction | Conditions (ALL must be true) |
|-----------|-------------------------------|
| **Long**  | bar closes **above** opening-range high **and** `EMA9 > EMA20` **and** price `> VWAP` |
| **Short** | bar closes **below** opening-range low **and** `EMA9 < EMA20` **and** price `< VWAP` |

- One position at a time. **No new entries after 15:30.** All flat by 15:55.
- The EMA + VWAP filters keep you trading *with* the trend and block low-quality
  breakouts — this is what stops the bot from over-trading.

## Profit & loss rules (clear and defined)

Let **R = the dollars risked per trade** = `|entry price − stop price|`.

**Loss side — when a trade closes at a loss:**
- **Stop placement:** the *tighter* of (a) the opposite side of the opening
  range, or (b) **1%** of entry price (configurable up to 2%).
- If price hits the stop, the remaining shares are closed immediately. Maximum
  loss on any trade ≈ **1% of the account** (see sizing below).

**Profit side — when a trade closes at a profit (scale-out):**
- **At +1R:** move the stop to **breakeven** → the trade can no longer lose.
- **At +2R:** **sell half** the position, banking the gain.
- **Runner (other half):** rides a **trailing stop** (0.5% below the running
  high for longs / above the low for shorts) and exits only when the trend
  reverses — this lets winners run.
- **Time stop:** anything still open is force-closed at **15:55**.

So a winning trade can exit in up to three pieces (half at 2R, runner on the
trail, remainder at the time stop), each logged separately.

## Risk engine — the two layers that cap losses at 5%

**Layer 1 — per-trade sizing.** Risk a fixed **1% of current equity** per trade:

```
risk_dollars   = equity × 1%                 # $20 on a $2000 account
risk_per_share = |entry − stop|
shares         = floor(risk_dollars / risk_per_share)   # capped by buying power
```

Because position size is derived *from* the stop distance, a stopped-out trade
loses ~1% of the account regardless of the stock's price.

**Layer 2 — daily circuit-breaker.** If equity falls **5% below the day's
starting balance** (−$100 on $2000), the bot **halts all new entries and
flattens open positions** for the rest of the day. This is the hard "max 5%
loss" guarantee.

### Worked example ($2000 account)

- Account equity = **$2000** → risk budget = **$20** per trade.
- Buy signal at **$50.00**; stop (1% of entry) at **$49.50** → risk/share = $0.50.
- Shares = floor($20 / $0.50) = **40 shares** (cost $2000, within buying power).
- Stopped out at $49.50 → loss = 40 × $0.50 = **$20 (1%)**.
- Hits +2R ($51.00): sell 20 shares (+$20 locked), stop to breakeven, trail the
  other 20 for more.
- After ~5 consecutive losers (−$100) the circuit-breaker stops the day at −5%.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env          # then add your free Alpaca PAPER keys
```

`.env` keys (see `.env.example`): `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`,
`ALPACA_PAPER=true`, `ALPACA_DATA_FEED=iex` (free feed for paper accounts).

## Backtest the bot on a real day

```bash
python run_backtest.py --symbol AAPL --date 2026-06-26
```

Starts at $2000, replays that day's 1-minute bars, and prints a per-trade table
plus a summary (return, win rate, profit factor, **max drawdown**, and whether
the 5% breaker fired). Useful flags:

```
--csv data/aapl.csv     # run fully offline from a CSV (timestamp,open,high,low,close,volume)
--cash 5000             # change starting capital
--risk 0.01             # risk per trade (0.01 = 1%)
--max-stop 0.02         # widen the per-trade stop cap to 2%
--daily-max-loss 0.05   # daily circuit-breaker threshold
--no-short              # long-only
```

If no Alpaca keys are set, it falls back to **yfinance** (1-minute history is
limited to ~the last 30 days).

## Live paper trading

```bash
python run_paper.py --symbols AAPL,MSFT      # during US market hours
```

Connects to your Alpaca **paper** account, streams 1-minute bars, and submits
paper market orders for every entry/scale-out/stop the strategy generates. It is
paper only — review thoroughly before considering real money.

## Live tracking dashboard

A self-contained local web page (no external CDNs) that tracks the bot in real
time — equity, P&L, the open position, a trade log, and an equity-curve chart.
It works for **both** a finished backtest and the **live paper bot**.

```bash
python run_backtest.py --symbol AAPL --date 2026-06-26   # writes logs/state.json
python dashboard.py                                       # then open http://localhost:5000
```

The bot writes `logs/state.json`; the page polls `/api/state` every ~2 seconds.
During paper trading the page updates each minute as new bars arrive, and shows a
red **HALTED** badge the moment the 5% daily circuit-breaker trips. Paper-mode
trade P&L is approximated from the local fill mirror.

## Project layout

```
sim/
  config.py      all tunables (account, strategy, risk, fills, data)
  indicators.py  EMA + VWAP (causal, no look-ahead)
  strategy.py    ORBStrategy — pure entry logic (shared by sim & live)
  risk.py        RiskManager — sizing, scale-out, stop, 5% breaker
  portfolio.py   cash/positions/equity + partial-close trade log
  engine.py      SimEngine — bar-by-bar replay + conservative fills
  data.py        Alpaca / yfinance / CSV data adapters
  report.py      performance metrics + report formatting
  state.py       JSON state snapshot for the dashboard
run_backtest.py  CLI backtester
run_paper.py     live Alpaca paper loop
dashboard.py     local Flask dashboard (serves the live page + /api/state)
static/          dashboard.html (self-contained, no CDNs)
tests/           offline unit + integration tests (pytest)
```

Run the tests: `python -m pytest tests/ -q`

## Disclaimer

Educational software. Trading involves substantial risk of loss. Backtest
results are not indicative of future returns. Test on paper thoroughly; use at
your own risk.
