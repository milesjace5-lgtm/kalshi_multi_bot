# Multi-Crypto Kalshi Hourly Bot

This package wires the existing hourly strategy into per-asset workers, Kalshi market discovery, order execution, order reconciliation, and a shared SQLite portfolio risk gate. This candidate is configured with `LIVE_TRADING=true` and `EMERGENCY_STOP=false` and enables live execution for ETH, SOL, and XRP. **Do not deploy this as-is unless you explicitly accept real-money order risk.** The series tickers are configuration inputs, not proof of current contract compatibility; package tests cannot validate live API permissions, market semantics, exchange responses, or profitability.

## Included behavior
- Coinbase market data and independent analysis/state for enabled assets in `assets.json`.
- Kalshi event/market lookup constrained to each worker's explicitly configured series ticker; no series means fail-closed.
- Verified market status, close window, settlement rule, strike and locked-outcome checks before order submission.
- Existing-order reconciliation and duplicate-order protection; uncertain submissions are not retried blindly.
- Shared atomic portfolio gate: maximum 3 reservations per UTC hour across workers and up to 25% of remaining uncommitted cash per reservation. Reservations persist in `state/portfolio-risk.sqlite3`.
- Asset-aware strike tolerance based on `TARGET_STEP`.
- Render Background Worker setup with persistent state disk.

## Required pre-live verification (even though live switches are enabled in this candidate)
1. For the first deployment, manually set `LIVE_TRADING=false` and `EMERGENCY_STOP=true` in Render until verification is complete.
2. Run `python discover_kalshi_crypto_series.py` and verify every configured ticker in Kalshi's current API/catalog. Candidate ticker strings in `assets.json` are configuration inputs, not proof of a live product or settlement compatibility.
3. Review logs for each asset, particularly selected event/market, strike, close timestamp, and settlement rule verification. If a product does not expose the same hourly strike proposition structure as the original BTC product, it should remain disabled until its contract adapter is implemented and tested.
4. Confirm API key permissions, balance schema, fee behavior, orderbook schema, and reconciliation against the actual account. No live order was submitted as part of package tests.
5. Only after all checks pass should you restore `LIVE_TRADING=true`, `EMERGENCY_STOP=false`, and the per-asset `live_enabled` switches.

## Risk gate details
The portfolio gate atomically reserves budget across subprocess workers. It counts reserved/submitted/filled/unknown reservations against the hourly limit and sizes each new reservation at `min(strategy_budget, remaining_cash * fraction)`. It is intentionally conservative: unresolved submissions remain reserved until reconciled; never manually delete the risk database while orders may be active.

## Deploy
Push the package contents to the intended GitHub repository and deploy the Render Blueprint. Keep the Render environment variable `LIVE_TRADING=false` and `EMERGENCY_STOP=true` during initial validation. Render persistent disk mount is configured at `/opt/render/project/src/state`.
