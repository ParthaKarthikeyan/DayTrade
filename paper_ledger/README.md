# Forward paper-trading ledgers (durable track records, committed each run).
# Each bot appends rows as it runs; rows after each ledger's deployment stamp are the
# genuine out-of-sample forward record. These files are what the hosted dashboard
# (https://parthakarthikeyan.github.io/DayTrade/) renders.

## Files
- `premarket_paper.json` — live Alpaca premarket gap-and-go bot. Trades a notional
  **$10k book** (`book_start`/`book_end`), never the whole shared paper account; the
  `account_pnl` field is reference only. One row per session with the daily governor
  verdict (`stopped`: "target" = banked +1.5%, "loss_cap" = -2% stop). Written by
  `run_premarket_paper.py`, committed by `premarket_paper.yml` (weekdays).
  Rows before 2026-07-03 predate the $10k book and sized off the whole account.
- `premarket_trades.json` — per-trade log reconciled against the bot's own tagged
  Alpaca fills (real prices incl. slippage). `reconciliation_gap` = book P&L minus
  broker-fill P&L; a large gap means slippage the strategy logic didn't see.
- `crypto_paper.json` / `crypto_trades.json` — 24/7 Alpaca-paper crypto bot ($10k
  book) trading the research-validated 6h Donchian trend on BTC + ETH. Written by
  `run_crypto_paper.py`, committed by `crypto_paper.yml` (hourly cron; commits only
  material changes).
- `futures_paper.json` / `futures_trades.json` — diversified $10k futures trend
  system, deterministic daily replay, two books (5% / 8% risk of current equity,
  margin-capped, one shared account). The headline is the FORWARD record since
  `deployment_date`; the full-history replay is backtest context. `rules_version`
  re-stamps the deployment date whenever the frozen rules change, so forward records
  never mix rule sets. Written by `run_futures_paper.py` (`futures_paper.yml`).

## Promotion policy (when does a book earn real money?)
A book is promoted — given more paper capital, or considered for the first real
dollar — only after **at least 30 forward sessions** with:
1. positive net expectancy (total P&L > 0 on ACTUAL fills, not intended prices),
2. max drawdown inside the book's stated budget,
3. no unexplained reconciliation gaps.
Backtest results never qualify a book; only the forward record here does.

These ledgers are the source of truth for "did it work?" — readable straight from
the repo, independent of the ephemeral runner logs or the per-run UI summary.
