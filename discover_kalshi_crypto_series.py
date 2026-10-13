"""Read-only helper: discover candidate Kalshi series for crypto assets.

This script does not authenticate, place orders, or change assets.json. It queries
Kalshi's public API and prints candidate series with title/ticker/frequency and
settlement source metadata so a human can verify exact contract rules.
"""
import json
import re
import sys
import requests

BASE = "https://external-api.kalshi.com/trade-api/v2"
ASSETS = ["BTC", "ETH", "ZEC", "XRP", "SOL", "USDT", "NEAR", "QNT", "HYPE", "SUI"]


def get_json(path, params=None):
    response = requests.get(f"{BASE}{path}", params=params or {}, timeout=20)
    response.raise_for_status()
    return response.json()


def main():
    print("Read-only Kalshi series discovery. Candidates are NOT automatically approved for trading.\n")
    found = []
    cursor = None
    # API pagination varies by endpoint/version; stop safely if the response has no cursor.
    for _ in range(20):
        params = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        try:
            payload = get_json("/series", params)
        except Exception as exc:
            print(f"ERROR fetching public series list: {exc}", file=sys.stderr)
            print("Check the current Kalshi API docs and try again.", file=sys.stderr)
            return 1
        rows = payload.get("series", [])
        if not isinstance(rows, list):
            break
        for row in rows:
            ticker = str(row.get("ticker", ""))
            title = str(row.get("title", ""))
            text = f"{ticker} {title} {row.get('category', '')} {' '.join(row.get('tags', []) if isinstance(row.get('tags'), list) else [])}"
            upper = text.upper()
            if any(re.search(rf"\b{re.escape(symbol)}\b", upper) for symbol in ASSETS) and any(k in upper for k in ("CRYPTO", "BITCOIN", "ETHEREUM", "COIN", "SOLANA", "RIPPLE", "XRP", "TOKEN")):
                found.append(row)
        cursor = payload.get("cursor")
        if not cursor or not rows:
            break
    if not found:
        print("No obvious crypto series candidates returned. This does not prove none exist; inspect Kalshi's current series catalog manually.")
        return 0
    found.sort(key=lambda r: (str(r.get("ticker", "")), str(r.get("title", ""))))
    for row in found:
        out = {
            "ticker": row.get("ticker"),
            "title": row.get("title"),
            "category": row.get("category"),
            "frequency": row.get("frequency"),
            "settlement_sources": row.get("settlement_sources"),
            "contract_url": row.get("contract_url"),
            "tags": row.get("tags"),
        }
        print(json.dumps(out, ensure_ascii=False))
    print(f"\nFound {len(found)} candidate series. Verify ticker, hourly cadence, underlying asset, strike format, close time, and settlement rules before configuring any live worker.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
