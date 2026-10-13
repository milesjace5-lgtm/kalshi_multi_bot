# Multi-crypto Kalshi bot — deployable source package

This is the closest safe integration of the supplied BTC hourly bot into a multi-asset project. It preserves the strategy files and the 30-to-5-minute entry window/late-hour strategy logic from the source. It includes per-asset Coinbase workers, separate learning/state files, Render config, and configurable Kalshi series mapping.

## Important live execution limitations
This is **not honestly represented as fully verified live multi-asset trading**. The source execution path was originally written for BTC hourly markets. Market discovery has been changed to read `KALSHI_HOURLY_SERIES_TICKER` per worker, but market semantics/strike units and settlement rules still need confirmation for each asset. `portfolio_risk.py` provides a SQLite reservation gate but the existing order path does not call it yet, so the global max-three-trades and 25%-of-remaining-cash portfolio rule are not yet enforced end-to-end. Do not enable multi-asset live trading until that integration and paper tests are complete.

## Install / deploy
- Upload the extracted folder contents to a new GitHub repo root.
- Render service type: Background Worker.
- Build: `pip install -r requirements.txt`
- Start: `python multi_asset_runner.py`
- Configure persistent disk before relying on SQLite learning/order state.
- Set secrets only in Render environment variables; never commit API keys/private keys.

## Live-mode gates
Global `LIVE_TRADING=true` is required, plus `live_enabled: true` and a non-empty verified `kalshi_series_ticker` for the asset in `assets.json`. BTC is preconfigured to `KXBTCD`; other assets are deliberately disabled for live execution until their series tickers and contract rules are verified. Keep `EMERGENCY_STOP` behavior in mind; the runner sets it based on each asset's live eligibility.
The user requested max 3 entries/hour across all assets and each next position up to 25% of remaining available cash. This requirement is documented but not enforced by the current order path; `portfolio_risk.py` is a building block, not a substitute for wiring it into order submission. The package must be tested in paper mode before any live use.

## Assets
Edit `assets.json`. `target_step` values are configuration starting points, not guaranteed Kalshi strikes. Confirm Coinbase pair availability, Kalshi series ticker, market cadence, strike increment, and settlement source per asset. Kalshi settlement price sources can differ from Coinbase spot, so the strategy's reference price may not exactly match contract settlement.

## Strategy timing
The inherited strategy retains the source's callout entry window (30-minute start and 5-minute cutoff) and late-hour behavior. Check the actual environment values and strategy gates in `config.py` and `live_execution.py`.
