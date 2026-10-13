"""Authenticated Kalshi Trade API client.

Supports current Kalshi RSA-PSS/SHA-256 and Ed25519 API keys. Order creation uses
Kalshi's current V2 endpoint and auto-routes by ticker across exchange shards.

Credential precedence is intentional: when KALSHI_PRIVATE_KEY_PATH is set, read
that file first (useful for Render Secret Files) so a stale environment PEM cannot
silently override it. If no path is configured, use KALSHI_PRIVATE_KEY or
KALSHI_PRIVATE_KEY_PEM. Never logs key material or signatures.
"""
from __future__ import annotations

import base64
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa, ed25519

DEFAULT_BASE = "https://external-api.kalshi.com/trade-api/v2"


class KalshiAPIError(RuntimeError):
    pass


class OrderSubmissionUncertain(KalshiAPIError):
    pass


class KalshiClient:
    def __init__(self, base_url=None, key_id=None, private_key_path=None,
                 private_key_pem=None, session=None, timeout=10):
        self.base_url = (base_url or os.getenv("KALSHI_BASE_URL") or DEFAULT_BASE).rstrip("/")
        parsed = urlparse(self.base_url)
        if parsed.scheme != "https" and parsed.hostname not in ("localhost", "127.0.0.1"):
            raise ValueError("Kalshi API base URL must use HTTPS")

        self.key_id = (key_id if key_id is not None else
                       (os.getenv("KALSHI_API_KEY_ID") or os.getenv("KALSHI_API_key_ID") or "")).strip()
        self.private_key_path = (private_key_path if private_key_path is not None else
                                 os.getenv("KALSHI_PRIVATE_KEY_PATH", "")).strip()
        self.private_key_pem = (private_key_pem if private_key_pem is not None else
                                (os.getenv("KALSHI_PRIVATE_KEY") or os.getenv("KALSHI_PRIVATE_KEY_PEM")))
        self.session = session or requests.Session()
        self.timeout = timeout
        self._key = None

    def _private_key(self):
        if self._key is not None:
            return self._key

        # Prefer the explicitly configured secret-file path over environment PEM.
        if self.private_key_path:
            try:
                raw = Path(self.private_key_path).read_bytes()
            except OSError as exc:
                raise RuntimeError(
                    "Kalshi private-key file not found/readable at KALSHI_PRIVATE_KEY_PATH; "
                    "check the mounted Secret File path and filename"
                ) from exc
        else:
            raw = self.private_key_pem
            if raw is None or raw == "":
                raise RuntimeError(
                    "Missing Kalshi private key. Configure KALSHI_PRIVATE_KEY_PATH to the "
                    "RSA private-key Secret File, or provide KALSHI_PRIVATE_KEY_PEM. "
                    "The API key ID alone cannot sign requests."
                )

        if isinstance(raw, str):
            raw = raw.replace("\\n", "\n").strip().encode("utf-8")
        try:
            key = serialization.load_pem_private_key(raw, password=None)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "Kalshi private key is not valid unencrypted PEM. Supply the complete RSA or Ed25519 "
                "PRIVATE key paired with KALSHI_API_KEY_ID, not the API key ID or a public key."
            ) from exc
        if not isinstance(key, (rsa.RSAPrivateKey, ed25519.Ed25519PrivateKey)):
            raise RuntimeError(
                "Kalshi API key must be an RSA or Ed25519 private key paired with KALSHI_API_KEY_ID; "
                f"configured key type is {type(key).__name__}."
            )
        self._key = key
        return self._key

    def _headers(self, method, path):
        if not self.key_id:
            raise RuntimeError("Missing Kalshi API key ID: set KALSHI_API_KEY_ID")
        timestamp = str(int(time.time() * 1000))
        api_path = urlparse(path).path
        prefix = "/trade-api/v2"
        sign_path = api_path if api_path.startswith(prefix) else prefix + (
            api_path if api_path.startswith("/") else "/" + api_path
        )
        message = (timestamp + method.upper() + sign_path).encode("utf-8")
        key = self._private_key()
        if isinstance(key, ed25519.Ed25519PrivateKey):
            signature = key.sign(message)
        else:
            signature = key.sign(
                message,
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                hashes.SHA256(),
            )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("ascii"),
            "Content-Type": "application/json",
        }

    def request(self, method, path, *, params=None, body=None, authenticated=True, order_submission=False):
        if not path.startswith("/") or ".." in path:
            raise ValueError("Invalid API path")
        headers = self._headers(method, path) if authenticated else {}
        try:
            response = self.session.request(
                method.upper(), self.base_url + path, params=params, json=body,
                headers=headers, timeout=self.timeout
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            if order_submission:
                raise OrderSubmissionUncertain(
                    "Order submission outcome uncertain; reconcile before retry"
                ) from exc
            raise
        if not response.ok:
            raise KalshiAPIError(
                f"Kalshi {method.upper()} {path} failed: HTTP {response.status_code}: {response.text[:500]}"
            )
        if response.status_code == 204 or not response.content:
            return {}
        return response.json()

    def balance(self): return self.request("GET", "/portfolio/balance")
    def events(self, **params):
        """List public Kalshi events, including nested markets when requested.

        Event discovery is public market-data access, so do not require account
        credentials for this endpoint. Parameters such as series_ticker, status,
        with_nested_markets, limit, cursor, and min_close_ts are passed through.
        """
        return self.request("GET", "/events", params=params, authenticated=False)

    def markets(self, **params): return self.request("GET", "/markets", params=params, authenticated=False)
    def market(self, ticker): return self.request("GET", f"/markets/{ticker}", authenticated=False)
    def orderbook(self, ticker): return self.request("GET", f"/markets/{ticker}/orderbook", authenticated=False)
    def create_order(self, order):
        """Submit an IOC order. `action` defaults to buy for backward compatibility.
        Prices are cents in the selected outcome (YES or NO). Sell orders use the
        opposite exchange side and are limit-protected at the specified minimum.
        """
        from decimal import Decimal
        ticker = str(order.get("ticker") or "").strip()
        outcome = str(order.get("side") or "").lower()
        action = str(order.get("action") or "buy").lower()
        if not ticker or outcome not in ("yes", "no") or action not in ("buy", "sell"):
            raise ValueError("Invalid order ticker, outcome side, or action")
        raw_price = order.get("yes_price") if outcome == "yes" else order.get("no_price")
        if raw_price is None:
            raise ValueError("Missing limit price for selected outcome")
        cents = Decimal(str(raw_price))
        if not cents.is_finite() or cents < 1 or cents > 99:
            raise ValueError("Order price must be between 1 and 99 cents")
        yes_book_price = cents / Decimal("100") if outcome == "yes" else Decimal("1") - cents / Decimal("100")
        count = Decimal(str(order.get("count")))
        if not count.is_finite() or count <= 0:
            raise ValueError("Order count must be positive")
        if count != count.to_integral_value():
            raise ValueError("Order count must be a whole number of contracts")
        if action == "buy":
            exchange_side = "bid" if outcome == "yes" else "ask"
        else:
            exchange_side = "ask" if outcome == "yes" else "bid"
        payload = {
            "ticker": ticker,
            "client_order_id": str(order.get("client_order_id") or ""),
            "side": exchange_side,
            "count": format(count, "f"),
            "price": format(yes_book_price.quantize(Decimal("0.0001")), ".4f"),
            "time_in_force": "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross",
            "post_only": False,
            "cancel_order_on_pause": True,
        }
        if not payload["client_order_id"]:
            raise ValueError("Missing client_order_id")
        return self.request("POST", "/portfolio/events/orders", body=payload, order_submission=True)

    def order(self, order_id): return self.request("GET", f"/portfolio/orders/{order_id}")
    def orders(self, **params): return self.request("GET", "/portfolio/orders", params=params)
    def cancel_order(self, order_id): return self.request("DELETE", f"/portfolio/orders/{order_id}")
    def fills(self, **params): return self.request("GET", "/portfolio/fills", params=params)
    def positions(self, **params): return self.request("GET", "/portfolio/positions", params=params)
    def settlements(self, **params): return self.request("GET", "/portfolio/settlements", params=params)
