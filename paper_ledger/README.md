# Forward paper-trading ledgers (durable track records, committed each run).
# Each run appends one row per calendar day; bars after the deployment stamp are the
# genuine out-of-sample forward record.

## Files
- `futures_paper.json` — diversified $10k futures trend system, two paper books
  (5% / 8% risk). Written by `run_futures_paper.py`, committed by
  `.github/workflows/futures_paper.yml` (weekdays 22:15 UTC).
- `premarket_paper.json` — live Alpaca premarket gap-and-go bot ($2k paper).
  One row per session: start/end equity, P&L, trade count, win rate, watchlist,
  whether the 5% daily breaker fired. Written by `run_premarket_paper.py`,
  committed by `.github/workflows/premarket_paper.yml` (weekdays 11:00 UTC).

These ledgers are the source of truth for "did it work?" — readable straight from
the repo, independent of the ephemeral runner logs or the per-run UI summary.
