# Multi-Crypto Kalshi Bot — GitHub / Render package

This is a separate multi-asset **analysis-only** project based on the supplied BTC hourly strategy source. It starts one isolated worker per enabled Coinbase USD product and keeps each worker's learner/state database separate.

## Important status
- Live orders are forcibly disabled (`LIVE_TRADING=false`, `EMERGENCY_STOP=true`) by the runner.
- The inherited Kalshi execution logic is BTC-series-specific. Do not remove the safety override until each asset's actual Kalshi hourly series/strike format is verified and a shared portfolio execution controller has been implemented and tested.
- The requested portfolio-wide limit of 3 trades/hour and sizing of up to 25% of remaining available cash per new position are **not active** in this analysis-only package.
- `assets.json` target steps are configurable starting values, not confirmed Kalshi strike intervals. Enable only Coinbase products that are available to your feed/account. USDT is disabled by default.
- This package does not claim profitability or live-execution readiness.

## Upload to a new GitHub repo
1. Extract this ZIP.
2. Upload the contents of the `multi-crypto-kalshi-bot` folder to the root of a new repository.
3. Commit and push. Do not upload `.env` files, private keys, or API secrets.

## Deploy to Render
Create a **Background Worker** from the new repository. Build command: `pip install -r requirements.txt`; start command: `python multi_asset_runner.py`. Or use the included `render.yaml` Blueprint.

## Configure assets
Edit `assets.json` to enable/disable assets and set `target_step`. Each enabled asset runs as a separate process, with a unique learner database. Watch Render logs for Coinbase feed and analysis status. Persisted worker state is in `state/`; configure a persistent disk and adjust paths if you need state retained across deploys.

## Local syntax check
Run `python -m compileall -q .`. This verifies Python syntax only, not API credentials, Coinbase product availability, Kalshi market mappings, order behavior, or strategy performance.
