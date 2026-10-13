from __future__ import annotations

"""Coinbase public market-data feed for the hourly BTC callout bot.

IMPORTANT:
- Analysis/callout only. No trading or account endpoints are used.
- Coinbase Advanced websocket is used for live ticker/trades/L2.
- Public Coinbase market-data REST is used for candles/ticker so the bot does
  not die when the Advanced brokerage REST endpoint returns HTTP 401.
- No API key or Coinbase credentials are required.
"""

import json
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

try:
    import websocket
except ImportError:  # pragma: no cover
    websocket = None

from config import CFG

# Advanced Trade websocket. This is public market data; no auth is needed for
# the channels used below.
WS_URL = "wss://advanced-trade-ws.coinbase.com"

# Public Coinbase Exchange market-data REST. These endpoints do not require a
# Coinbase account/API key and provide the same BTC-USD public market feed.
PUBLIC_REST = "https://api.exchange.coinbase.com"

# Optional Advanced Trade public REST endpoint. Some deployments/endpoints can
# return 401 even for market-data requests, so it is only a fallback.
ADVANCED_REST = "https://api.coinbase.com/api/v3/brokerage"

# Coinbase Exchange candle granularity is seconds.
GRANULARITY_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
}

# Advanced Trade granularity names, used only for the optional fallback.
ADVANCED_GRANULARITY = {
    "1m": "ONE_MINUTE",
    "5m": "FIVE_MINUTE",
    "15m": "FIFTEEN_MINUTE",
    "30m": "THIRTY_MINUTE",
    "1h": "ONE_HOUR",
}

# The strategy does not need fresh REST candles every five seconds. Keeping a
# short cache also avoids hammering Coinbase and makes Render much more stable.
CANDLE_CACHE_SECONDS = {
    "1m": 5.0,
    "5m": 10.0,
    "15m": 15.0,
    "30m": 20.0,
    "1h": 30.0,
}


class CoinbaseFeed:
    """Reliable public Coinbase BTC-USD market-data feed.

    The interface intentionally matches the old feed so this is a true
    copy/paste replacement for coinbase_feed.py.
    """

    def __init__(self, product_id: Optional[str] = None):
        self.product_id = product_id or CFG.product_id
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "BTC-Hourly-Coinbase-Callout-Bot/1.0",
                "Accept": "application/json",
            }
        )

        self.lock = threading.RLock()
        self.price = 0.0
        self.price_time = 0.0
        self.trades: List[Dict[str, Any]] = []
        self.l2: Dict[str, Any] = {"usable": False, "reason": "NO_L2_DATA"}

        self.stop_event = threading.Event()
        self.ws_thread: Optional[threading.Thread] = None
        self.ws_connected = False
        self.ws_last_message = 0.0
        self.ws_last_error = ""

        self._candle_cache: Dict[str, Dict[str, Any]] = {}
        self._last_rest_error = ""
        self._rest_source = "PUBLIC_COINBASE"

    # ------------------------------------------------------------------
    # REST
    # ------------------------------------------------------------------
    def _get(self, url: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """GET with retry/backoff and useful errors."""
        last: Optional[Exception] = None
        for attempt in range(max(1, int(CFG.max_rest_retries))):
            try:
                r = self.session.get(url, params=params, timeout=8)

                # A 401 from Advanced REST is not a reason to kill the bot.
                # The public Coinbase endpoint is tried by the caller.
                r.raise_for_status()
                self._last_rest_error = ""
                return r.json()
            except Exception as exc:
                last = exc
                self._last_rest_error = str(exc)
                if attempt + 1 < max(1, int(CFG.max_rest_retries)):
                    time.sleep(0.35 * (attempt + 1))

        raise RuntimeError(f"Coinbase REST failed: {last}")

    def _public_candles(self, tf: str, limit: int) -> List[Dict[str, float]]:
        """Read candles from Coinbase's public market-data endpoint."""
        gran = GRANULARITY_SECONDS[tf]
        # Coinbase Exchange allows at most 300 candles per request.
        limit = max(2, min(int(limit), 300))
        end = int(time.time())
        start = end - gran * limit
        # Coinbase Exchange expects ISO-8601 timestamps for start/end.
        start_iso = datetime.fromtimestamp(start, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        end_iso = datetime.fromtimestamp(end, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

        data = self._get(
            f"{PUBLIC_REST}/products/{self.product_id}/candles",
            {"start": start_iso, "end": end_iso, "granularity": gran},
        )

        rows: List[Dict[str, float]] = []
        for c in data if isinstance(data, list) else []:
            try:
                # Exchange format: [time, low, high, open, close, volume]
                ts, low, high, opn, close, volume = c[:6]
                rows.append(
                    {
                        "time": float(ts),
                        "ts": int(float(ts)),
                        "complete": int(float(ts)) + gran <= int(time.time()),
                        "open": float(opn),
                        "high": float(high),
                        "low": float(low),
                        "close": float(close),
                        "volume": float(volume),
                    }
                )
            except (TypeError, ValueError, IndexError):
                continue

        rows.sort(key=lambda x: x["time"])
        return rows[-limit:]

    def _advanced_candles(self, tf: str, limit: int) -> List[Dict[str, float]]:
        """Optional Advanced Trade fallback. Some hosts return 401 here."""
        gran = GRANULARITY_SECONDS[tf]
        limit = max(2, min(int(limit), 300))
        end = int(time.time())
        start = end - gran * limit
        start_iso = datetime.fromtimestamp(start, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        end_iso = datetime.fromtimestamp(end, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

        data = self._get(
            f"{ADVANCED_REST}/products/{self.product_id}/candles",
            {
                "start": start_iso,
                "end": end_iso,
                "granularity": ADVANCED_GRANULARITY[tf],
            },
        )

        rows: List[Dict[str, float]] = []
        for c in data.get("candles", []) if isinstance(data, dict) else []:
            try:
                rows.append(
                    {
                        "time": float(c["start"]),
                        "ts": int(float(c["start"])),
                        "complete": int(float(c["start"])) + gran <= int(time.time()),
                        "open": float(c["open"]),
                        "high": float(c["high"]),
                        "low": float(c["low"]),
                        "close": float(c["close"]),
                        "volume": float(c["volume"]),
                    }
                )
            except (KeyError, TypeError, ValueError):
                continue

        rows.sort(key=lambda x: x["time"])
        return rows[-limit:]

    def candles(self, tf: str, limit: int = 200) -> List[Dict[str, float]]:
        if tf not in GRANULARITY_SECONDS:
            raise ValueError(f"Unsupported timeframe: {tf}")

        now = time.time()
        cached = self._candle_cache.get(tf)
        max_age = CANDLE_CACHE_SECONDS.get(tf, 10.0)

        if cached and now - float(cached.get("time", 0)) < max_age:
            rows = cached.get("rows", [])
            if rows:
                return rows[-limit:]

        try:
            rows = self._public_candles(tf, limit)
            if rows:
                self._rest_source = "COINBASE_PUBLIC_MARKET_DATA"
                self._candle_cache[tf] = {"time": now, "rows": rows}
                return rows[-limit:]
            raise RuntimeError("Coinbase public candles returned no rows")
        except Exception as public_exc:
            # Try Advanced Trade only after the public endpoint fails.
            try:
                rows = self._advanced_candles(tf, limit)
                if rows:
                    self._rest_source = "COINBASE_ADVANCED_REST"
                    self._candle_cache[tf] = {"time": now, "rows": rows}
                    return rows[-limit:]
                raise RuntimeError("Advanced candles returned no rows")
            except Exception as advanced_exc:
                # If a recent cache exists, use it rather than crashing the
                # whole analysis loop. Marking it stale is handled below by
                # snapshot metadata; no data is invented.
                if cached and cached.get("rows"):
                    self._last_rest_error = (
                        f"PUBLIC={public_exc}; ADVANCED={advanced_exc}; USING_CACHED_CANDLES"
                    )
                    return cached["rows"][-limit:]
                raise RuntimeError(
                    f"Coinbase candles unavailable | PUBLIC={public_exc} | ADVANCED={advanced_exc}"
                )

    def _public_ticker(self) -> float:
        data = self._get(f"{PUBLIC_REST}/products/{self.product_id}/ticker")
        price = float(data.get("price", 0)) if isinstance(data, dict) else 0.0
        if price > 0:
            return price
        raise RuntimeError("Coinbase public ticker returned no price")

    def _advanced_ticker(self) -> float:
        data = self._get(f"{ADVANCED_REST}/products/{self.product_id}")
        price = float(data.get("price") or data.get("price_usd") or 0)
        if price > 0:
            return price
        raise RuntimeError("Coinbase Advanced ticker returned no price")

    def ticker(self) -> float:
        # WebSocket ticker is preferred when it is genuinely fresh.
        with self.lock:
            ws_price = float(self.price or 0)
            ws_age = time.time() - float(self.price_time or 0)

        if ws_price > 0 and ws_age <= 5:
            return ws_price

        try:
            p = self._public_ticker()
            with self.lock:
                self.price = p
                self.price_time = time.time()
            self._rest_source = "COINBASE_PUBLIC_MARKET_DATA"
            return p
        except Exception as public_exc:
            try:
                p = self._advanced_ticker()
                with self.lock:
                    self.price = p
                    self.price_time = time.time()
                self._rest_source = "COINBASE_ADVANCED_REST"
                return p
            except Exception as advanced_exc:
                with self.lock:
                    if self.price > 0:
                        self._last_rest_error = (
                            f"TICKER PUBLIC={public_exc}; ADVANCED={advanced_exc}; USING_CACHED_PRICE"
                        )
                        return self.price
                raise RuntimeError(
                    f"Coinbase ticker unavailable | PUBLIC={public_exc} | ADVANCED={advanced_exc}"
                )

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------
    def snapshot(self) -> Dict[str, Any]:
        # Pull sequentially so one failed timeframe can fall back to its
        # existing cache instead of destroying the whole snapshot.
        bars: Dict[str, List[Dict[str, float]]] = {}
        errors: Dict[str, str] = {}

        for tf in GRANULARITY_SECONDS:
            try:
                bars[tf] = self.candles(tf, 350)
            except Exception as exc:
                bars[tf] = []
                errors[tf] = str(exc)

        # Ticker is independent from candles.
        price = 0.0
        try:
            price = self.ticker()
        except Exception as exc:
            errors["ticker"] = str(exc)

        # If ticker failed but a live 1m close exists, that is a last-resort
        # market-data fallback. It is clearly not called live if it is old.
        if price <= 0 and bars.get("1m"):
            price = float(bars["1m"][-1]["close"])

        now = time.time()
        with self.lock:
            price_age = now - float(self.price_time or 0) if self.price_time else 9999.0
            trades = [
                dict(t)
                for t in self.trades
                if now - float(t.get("time", now)) <= CFG.trade_window_seconds
            ][-1500:]
            l2 = dict(self.l2)
            ws_connected = bool(self.ws_connected)
            ws_age = now - float(self.ws_last_message or 0)
            ws_error = self.ws_last_error

        prev_hour = None
        if len(bars.get("1h", [])) >= 2:
            prev_hour = bars["1h"][-2]

        # Mark L2 unusable unless it is actually fresh.
        l2_age = now - float(l2.get("timestamp", 0) or 0)
        if not l2.get("usable") or l2_age > float(CFG.l2_max_age_seconds):
            l2 = {
                "usable": False,
                "reason": "STALE_OR_UNAVAILABLE",
                "age_seconds": round(l2_age, 3) if l2.get("timestamp") else None,
            }

        status = "OK" if price > 0 and bars.get("1m") else "DEGRADED"
        if not bars.get("1m"):
            status = "NO_CANDLE_DATA"

        levels = l2.get("levels", {}) if isinstance(l2, dict) else {}
        return {
            "price": price,
            "age": price_age,
            "price_age_seconds": price_age,
            "bars": bars,
            "trades": trades,
            "l2": l2,
            "bids": levels.get("bids", {}),
            "asks": levels.get("asks", {}),
            "book_age": l2.get("age_seconds", l2_age),
            "book_valid": bool(l2.get("usable")),
            "prev_hour": prev_hour,
            "timestamp": now,
            "data_status": status,
            "rest_source": self._rest_source,
            "rest_errors": errors,
            "last_rest_error": self._last_rest_error,
            "ws_connected": ws_connected,
            "ws_age_seconds": round(ws_age, 3) if self.ws_last_message else None,
            "ws_error": ws_error,
        }

    # ------------------------------------------------------------------
    # WebSocket
    # ------------------------------------------------------------------
    def start(self) -> None:
        if websocket is None:
            return
        if self.ws_thread and self.ws_thread.is_alive():
            return
        self.stop_event.clear()
        self.ws_thread = threading.Thread(
            target=self._ws_loop,
            name="coinbase-ws",
            daemon=True,
        )
        self.ws_thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        with self.lock:
            self.ws_connected = False

    def _ws_loop(self) -> None:
        while not self.stop_event.is_set():
            ws = None
            try:
                ws = websocket.create_connection(
                    WS_URL,
                    timeout=10,
                    enableTrace=False,
                    origin="https://advanced.coinbase.com",
                )

                def subscribe(channel: str) -> None:
                    ws.send(
                        json.dumps(
                            {
                                "type": "subscribe",
                                "product_ids": [self.product_id],
                                "channel": channel,
                            }
                        )
                    )

                # Public channels only. No JWT, API key, or account access.
                for channel in ("heartbeats", "ticker", "market_trades", "level2"):
                    subscribe(channel)

                with self.lock:
                    self.ws_connected = True
                    self.ws_last_error = ""
                    self.ws_last_message = time.time()

                ws.settimeout(5)

                while not self.stop_event.is_set():
                    try:
                        raw = ws.recv()
                    except Exception as exc:
                        with self.lock:
                            self.ws_last_error = str(exc)
                        break

                    if not raw:
                        continue

                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue

                    with self.lock:
                        self.ws_last_message = time.time()
                    self._handle(msg)

            except Exception as exc:
                with self.lock:
                    self.ws_last_error = str(exc)
            finally:
                with self.lock:
                    self.ws_connected = False
                try:
                    if ws is not None:
                        ws.close()
                except Exception:
                    pass

            # Coinbase can close a clean websocket (1000) normally. Reconnect
            # quietly instead of treating it as a fatal analysis error.
            if not self.stop_event.is_set():
                time.sleep(2)

    # ------------------------------------------------------------------
    # WebSocket message parser
    # ------------------------------------------------------------------
    @staticmethod
    def _event_time(value: Any, fallback: float) -> float:
        try:
            if value is None:
                return fallback
            s = str(value)
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            return datetime.fromisoformat(s).timestamp()
        except Exception:
            return fallback

    def _handle(self, message: Dict[str, Any]) -> None:
        now = time.time()
        channel = str(message.get("channel", "")).lower()

        with self.lock:
            # Ticker channel.
            if channel == "ticker":
                for event in message.get("events", []) or []:
                    for tick in event.get("tickers", []) or []:
                        if tick.get("product_id") != self.product_id:
                            continue
                        try:
                            p = float(tick.get("price"))
                        except (TypeError, ValueError):
                            continue
                        if p > 0:
                            self.price = p
                            self.price_time = now

            # Coinbase Advanced market-trades messages.
            elif channel == "market_trades":
                for event in message.get("events", []) or []:
                    for trade in event.get("trades", []) or []:
                        try:
                            size = float(trade.get("size", 0))
                            price = float(trade.get("price", 0))
                        except (TypeError, ValueError):
                            continue
                        if size <= 0 or price <= 0:
                            continue

                        ts = self._event_time(trade.get("time"), now)
                        side = str(trade.get("side", "")).lower()
                        self.trades.append(
                            {
                                "time": ts,
                                "ts": ts,
                                "size": size,
                                "side": side,
                                "price": price,
                            }
                        )

                        self.price = price
                        self.price_time = now

                # Keep memory bounded.
                cutoff = now - max(600, int(CFG.trade_window_seconds) * 2)
                self.trades = [t for t in self.trades if float(t.get("time", now)) >= cutoff][-3000:]

            # The subscription is "level2" but Coinbase commonly sends the
            # resulting messages on the "l2_data" channel. Accept both.
            elif channel in ("level2", "l2_data", "l2"):
                self._handle_l2(message, now)

    def _handle_l2(self, message: Dict[str, Any], now: float) -> None:
        events = message.get("events", []) or []
        for event in events:
            event_type = str(event.get("type", "")).lower()
            updates = event.get("updates", []) or []

            if event_type == "snapshot":
                bids: Dict[str, float] = {}
                asks: Dict[str, float] = {}
            else:
                old = self.l2.get("levels", {})
                bids = dict(old.get("bids", {}))
                asks = dict(old.get("asks", {}))

            for update in updates:
                side = str(update.get("side", "")).lower()
                price = str(update.get("price_level", update.get("price", "")))
                try:
                    qty = float(update.get("new_quantity", update.get("quantity", 0)) or 0)
                except (TypeError, ValueError):
                    continue

                if not price:
                    continue

                if side == "bid":
                    target = bids
                elif side in ("offer", "ask"):
                    target = asks
                else:
                    continue

                if qty <= 0:
                    target.pop(price, None)
                else:
                    target[price] = qty

            # Do not claim usable L2 unless there is actually a book.
            usable = bool(bids or asks)
            self.l2 = {
                "usable": usable,
                "timestamp": now,
                "levels": {"bids": bids, "asks": asks},
            }
