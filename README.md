# Multi-Crypto Kalshi Hourly Bot

This is a multi-worker adaptation of the supplied BTC hourly bot. It uses Coinbase product feeds for signals and Kalshi's API for hourly strike-market discovery and execution.

## Render
- Build: `pip install -r requirements.txt`
- Start: `python multi_asset_runner.py`
- `render.yaml` sets live mode to true. Configure Kalshi API credentials in Render Environment using the exact variable names expected by `kalshi_client.py`/`config.py`; never commit private keys.
- The state directory must be persistent for the shared risk ledger. Add a persistent disk mounted at `/opt/render/project/src/state` (paid plan may be required) or reservations may reset after redeploys.

## Portfolio gate
- Shared maximum: 3 entries per UTC hour across workers.
- Per-trade budget: at most 25% of estimated remaining uncommitted cash.
- The runner uses per-asset state databases and one shared SQLite portfolio risk database.

## Asset configuration
BTC, ETH, ZEC, XRP, SOL, NEAR, HYPE, and SUI are configured with candidate directional series tickers. QNT and USDT are disabled because this package has no verified matching hourly series for them. The execution layer must find an open market with the configured series, current-hour close time, and a settlement proposition it can parse; otherwise it skips rather than submitting an order.

## Important limitations
This package has been syntax-checked and locally tested for portfolio sizing/entry caps, but it has **not** been validated against live Kalshi market responses or a live account. Series availability and ticker naming can change. Before live use, run the market discovery helper and inspect logs for each series, confirm market settlement/index rules and price steps, and start with small size. Coinbase spot is not necessarily identical to the reference index used by a Kalshi contract. No software can guarantee profitable trades.

The strategy code was adapted from the supplied BTC bot; asset-specific thresholds and signal calibration may require tuning for each asset.
