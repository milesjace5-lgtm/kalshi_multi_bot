"""Fail-closed live execution coordinator connected to bot reports."""
from __future__ import annotations
import hashlib, json, logging, os, time, uuid
from decimal import Decimal, ROUND_FLOOR
from execution_store import ExecutionStore
from kalshi_client import KalshiClient, OrderSubmissionUncertain
from settlement import interpret_settlement
from trading_controls import TradingControls
from portfolio_risk import reserve_entry, mark_status

log=logging.getLogger("execution")

def _value(obj,*keys):
    for k in keys:
        if isinstance(obj,dict) and obj.get(k) is not None: return obj[k]
    return None


def _side_from_locked_outcome(outcome, proposition):
    """Translate the analysis engine's locked OVER/UNDER outcome to Kalshi YES/NO.

    This deliberately does not inspect projected_finish or make a second forecast.
    The verified market proposition determines only whether that supplied outcome
    corresponds to YES or NO on the selected contract.
    """
    if not isinstance(outcome, str) or not isinstance(proposition, dict):
        return None
    text=outcome.strip().upper()
    is_over = ("OVER" in text or "ABOVE" in text)
    is_under = ("UNDER" in text or "BELOW" in text)
    if is_over == is_under:  # neither or ambiguous both
        return None
    predicate=str(proposition.get("predicate", "")).lower()
    if predicate in ("above", "at_or_above"):
        yes_means_over=True
    elif predicate in ("below", "at_or_below"):
        yes_means_over=False
    else:
        return None
    return "yes" if is_over == yes_means_over else "no"


def _outcome_target(outcome):
    """Extract the numeric checkpoint from an analysis outcome like '$83,000 OVER'."""
    if not isinstance(outcome, str):
        return None
    import re
    match=re.search(r"\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)", outcome)
    if not match:
        return None
    try:
        value=Decimal(match.group(1).replace(",", ""))
        return value if value.is_finite() and value > 0 else None
    except Exception:
        return None

def _dollars_from_balance(data):
    """Convert documented Kalshi portfolio balance (integer cents) to dollars.

    Prefer an explicit available_balance when a response/version supplies it. The
    Trade API v2 balance response commonly exposes `balance` in cents; accept that
    field only as the cash balance, never portfolio_value/payout (which can include
    unrealized positions). Unknown schemas fail closed.
    """
    raw=_value(data,"available_balance")
    if raw is None:
        raw=_value(data,"balance")
    if raw is None: return None
    try:
        value=Decimal(str(raw))
        if not value.is_finite() or value < 0: return None
        return value/Decimal("100")
    except Exception: return None

class LiveExecution:
    def __init__(self, db_path=None, client=None, controls=None):
        self.client=client or KalshiClient()
        self.controls=controls or TradingControls.from_environment()
        self.store=ExecutionStore(db_path or os.getenv("EXECUTION_DB","execution.sqlite3"))
        self.max_spread=Decimal(os.getenv("MAX_SPREAD_CENTS","5"))
        self.min_liquidity=int(os.getenv("MIN_CONTRACT_LIQUIDITY","10"))
        self.max_price=int(os.getenv("MAX_ORDER_PRICE_CENTS","85"))
        self.max_positions=int(os.getenv("MAX_SIMULTANEOUS_POSITIONS","2"))
        self.max_entries=int(os.getenv("MAX_ENTRIES_PER_HOURLY_MARKET","1"))
        self.max_contracts=int(os.getenv("MAX_CONTRACTS_PER_ORDER","100"))
        self.cutoff=int(os.getenv("KALSHI_ORDER_CUTOFF_SECONDS","300"))
        self._reconciled=False
        self._last_reconcile_at=0.0
        self._market_cache_at=0.0
        self._market_cache=None
        self._market_cache_key=None
        self._market_cache_ready=False
        self._market_backoff_until=0.0
        self._orderbook_backoff_until=0.0
        self._orderbook_error_backoff=max(30.0, float(os.getenv("KALSHI_ORDERBOOK_ERROR_BACKOFF_SECONDS", "60")))
        self._market_cache_ttl=max(30.0, float(os.getenv("KALSHI_MARKET_CACHE_SECONDS", "60")))
        self._market_error_backoff=max(30.0, float(os.getenv("KALSHI_MARKET_ERROR_BACKOFF_SECONDS", "60")))

    def reconcile_before_entry(self, now=None):
        now=time.time() if now is None else now
        self._last_reconcile_at=now
        # Exchange state must be reachable and open intents resolved before new orders.
        # Credential/configuration failures should block trading cleanly rather than
        # escaping into the main bot loop as an unhandled traceback.
        try:
            self.client.orders(limit=200)
            self.client.positions()
            self.client.fills(limit=200)
        except Exception as exc:
            message = str(exc).strip() or exc.__class__.__name__
            log.warning("Account/order reconciliation unavailable; live entry blocked: %s", message)
            self._reconciled = False
            return False
        for intent in self.store.unresolved():
            oid=intent.get("exchange_order_id")
            if not oid:
                # No exchange order ID is not proof of failure; don't resubmit.
                self.store.transition(intent["client_order_id"],"UNKNOWN_PENDING_RECONCILIATION")
                continue
            try:
                detail=self.client.order(oid)
                order=_value(detail,"order") or detail
                status=str(_value(order,"status") or "").lower()
                mapped={"resting":"ACCEPTED","open":"ACCEPTED","pending":"ACCEPTED","executed":"FILLED",
                        "canceled":"CANCELLED","cancelled":"CANCELLED","rejected":"REJECTED"}
                if status in mapped: self.store.transition(intent["client_order_id"],mapped[status],oid)
                elif status: self.store.transition(intent["client_order_id"],"PARTIALLY FILLED",oid)
                else: self.store.transition(intent["client_order_id"],"UNKNOWN_PENDING_RECONCILIATION",oid)
            except Exception:
                log.exception("Existing order reconciliation failed; new entry blocked")
                return False
        unresolved=self.store.unresolved()
        # A resting/partially-filled order is still exposure. Do not open another order
        # until the exchange confirms it filled, canceled, or rejected.
        active_statuses={"SUBMITTING","ACCEPTED","PARTIALLY FILLED","UNKNOWN_PENDING_RECONCILIATION","CANCEL_REQUESTED"}
        has_active_order=any(x["status"] in active_statuses for x in unresolved)
        self._reconciled=not has_active_order
        if has_active_order:
            log.info("Existing order remains active or unresolved; new entry blocked")
        return self._reconciled

    def discover_hourly_crypto_market(self, now=None, target_level=None, reference_price=None, allow_nearest=False):
        now=time.time() if now is None else now
        # Underlying price changes on every analysis tick, so it must NOT be part of the
        # discovery cache key. Otherwise each new price bypasses the 429 cooldown.
        # Scope the cache to the hourly settlement, requested checkpoint, and mode.
        hour_key = int(now // 3600)
        cache_key=(hour_key, str(target_level), bool(allow_nearest))
        if (self._market_cache_ready and self._market_cache_key == cache_key
                and now-self._market_cache_at < self._market_cache_ttl):
            return self._market_cache
        # Rate-limit cooldown is global across changing reference prices/strikes.
        if now < self._market_backoff_until:
            return None
        try:
            result=self._discover_hourly_crypto_market_uncached(
                now, target_level=target_level, reference_price=reference_price,
                allow_nearest=allow_nearest
            )
        except Exception as exc:
            message=str(exc)
            if "429" in message or "too_many_requests" in message.lower():
                self._market_backoff_until=now+self._market_error_backoff
                log.warning("Kalshi hourly event discovery rate-limited (HTTP 429); pausing market lookups for %.0fs", self._market_error_backoff)
            else:
                self._market_backoff_until=now+30.0
                log.warning("Kalshi hourly event discovery failed; retrying after cooldown: %s", message[:300])
            self._market_cache=None
            self._market_cache_key=cache_key
            self._market_cache_at=now
            self._market_cache_ready=True
            return None
        self._market_cache=result
        self._market_cache_key=cache_key
        self._market_cache_at=now
        self._market_cache_ready=True
        if result is None:
            self._market_backoff_until=now+15.0
        else:
            self._market_backoff_until=0.0
            log.info("Kalshi hourly crypto market discovery succeeded: ticker=%s strike=%s close_ts=%s",
                     _value(result.get("market", {}), "ticker"),
                     result.get("proposition", {}).get("strike"), result.get("close_ts"))
        return result

    def _discover_hourly_crypto_market_uncached(self, now=None, target_level=None, reference_price=None, allow_nearest=False):
        """Find the current configured hourly event, then choose its specific strike market.

        Kalshi's hourly crypto product is a single event with many strike markets. Scanning
        thousands of unrelated open markets and requiring the word 'hourly' in every
        market title misses this product; all strike markets also share a close time.
        Query the documented events endpoint by series ticker instead.
        """
        now=time.time() if now is None else now
        hour=int(now//3600)*3600
        configured_series = str(os.getenv("KALSHI_HOURLY_SERIES_TICKER", "")).strip()
        if not configured_series or configured_series.upper() == "UNCONFIGURED":
            log.warning("No Kalshi series configured for asset=%s; refusing market discovery", os.getenv("ASSET_ID", os.getenv("PRODUCT_ID", "UNKNOWN")))
            return None
        max_pages=max(1,min(3,int(os.getenv("KALSHI_EVENT_DISCOVERY_MAX_PAGES","2"))))
        cursor=None
        seen_cursors=set()
        events=[]
        complete=False
        for _ in range(max_pages):
            params={
                "series_ticker": os.getenv("KALSHI_HOURLY_SERIES_TICKER", ""),
                "status":"open",
                "with_nested_markets":"true",
                "limit":200,
                "min_close_ts":hour,
            }
            if cursor:
                params["cursor"]=cursor
            data=self.client.events(**params)
            page=_value(data,"events")
            if not isinstance(page,list):
                log.warning("Kalshi events response has no events array; response schema rejected")
                return None
            events.extend(page)
            next_cursor=_value(data,"cursor")
            if not next_cursor:
                complete=True
                break
            next_cursor=str(next_cursor)
            if next_cursor in seen_cursors:
                log.warning("Kalshi event pagination cursor repeated; discovery rejected")
                return None
            seen_cursors.add(next_cursor)
            cursor=next_cursor
        if not complete:
            log.info("Kalshi hourly event discovery stopped at bounded page limit=%d", max_pages)

        candidates=[]
        # Kalshi's current Market object uses `active` for a tradable market;
        # `open` is also accepted for API versions/fixtures that expose that label.
        # Keep diagnostics so a future schema/status change is visible in Render logs.
        status_counts={}
        nested_count=0
        time_count=0
        settlement_count=0
        for event in events:
            series=str(_value(event,"series_ticker") or "")
            event_ticker=str(_value(event,"event_ticker") or "")
            event_title=str(_value(event,"title","sub_title","subtitle") or "")
            # The series filter is authoritative; check returned metadata too.
            if series and series.upper() != os.getenv("KALSHI_HOURLY_SERIES_TICKER","").upper():
                continue
            nested=_value(event,"markets")
            if not isinstance(nested,list):
                continue
            for m in nested:
                nested_count += 1
                ticker=str(_value(m,"ticker") or "")
                if not ticker:
                    continue
                status=str(_value(m,"status") or "unknown").lower()
                status_counts[status]=status_counts.get(status,0)+1
                if status not in {"open", "active"}:
                    continue
                close=_value(m,"close_time","expiration_time","expected_expiration_time")
                if not close:
                    continue
                try:
                    close_dt=self._utc_timestamp(close)
                    close_ts=close_dt.timestamp()
                except Exception:
                    continue
                # Hourly strike markets for the configured crypto series share the settlement window.
                if not (hour < close_ts <= hour+3600+2):
                    continue
                time_count += 1
                title=" ".join(x for x in (event_title,str(_value(m,"title") or ""),ticker) if x)
                subtitle=str(_value(m,"subtitle","yes_sub_title") or "")
                rules=str(_value(m,"rules_primary") or "")
                prop=interpret_settlement(title,subtitle,rules,ticker)
                if not prop:
                    continue
                settlement_count += 1
                candidates.append({"market":m,"event":event,"proposition":prop,"close_ts":close_ts,
                                   "event_ticker":event_ticker,"title":title})

        if not candidates:
            log.warning(
                "No eligible configured hourly strike market for current UTC hour=%d: events=%d nested_markets=%d statuses=%s time_window_matches=%d settlement_matches=%d; no order submitted",
                hour, len(events), nested_count, status_counts, time_count, settlement_count
            )
            return None

        # Match the analysis checkpoint to the actual listed strike. Kalshi tickers
        # sometimes encode a boundary as e.g. 82999.99 for a displayed $83,000
        # checkpoint, so allow a small rounding tolerance; do not silently trade a
        # materially different threshold just because it is the nearest available.
        anchor=None
        if target_level is not None:
            try:
                anchor=Decimal(str(target_level))
                if not anchor.is_finite(): anchor=None
            except Exception:
                anchor=None
        if anchor is None and reference_price is not None:
            try:
                anchor=Decimal(str(reference_price))
                if not anchor.is_finite(): anchor=None
            except Exception:
                anchor=None
        if anchor is None:
            log.warning("Hourly event found but target/reference price is unavailable; cannot select a strike safely")
            return None
        try:
            tolerance=Decimal(os.getenv("KALSHI_STRIKE_MATCH_TOLERANCE_DOLLARS", "1"))
        except Exception:
            log.error("Invalid KALSHI_STRIKE_MATCH_TOLERANCE_DOLLARS; refusing market selection")
            return None
        if not tolerance.is_finite() or tolerance < 0 or tolerance > Decimal("5"):
            log.error("Strike-match tolerance must be between $0 and $5; refusing market selection")
            return None
        distances=[(abs(c["proposition"]["strike"]-anchor), c) for c in candidates]
        eligible=[(distance,c) for distance,c in distances if distance <= tolerance]
        if not eligible:
            nearest=min(distance for distance,_ in distances)
            log.warning("No configured contract matches the analysis checkpoint within tolerance: target=%s nearest_distance=%s tolerance=%s; refusing a different outcome contract",
                        anchor, nearest, tolerance)
            return None
        nearest=min(distance for distance,_ in eligible)
        pool=[c for distance,c in eligible if distance==nearest]

        # If a price lies exactly between two strikes, prefer the more liquid market.
        def liquidity_score(c):
            m=c["market"]
            for key in ("liquidity_dollars","volume_24h_fp","volume_fp","open_interest_fp"):
                try:
                    value=Decimal(str(m.get(key)))
                    if value.is_finite(): return value
                except Exception:
                    pass
            return Decimal("0")
        pool.sort(key=lambda c:(liquidity_score(c),str(_value(c["market"],"ticker") or "")),reverse=True)
        selected=pool[0]
        m=selected["market"]
        detail=self.client.market(m["ticker"])
        verified=_value(detail,"market") or detail
        if str(_value(verified,"ticker") or "") != str(m["ticker"]):
            log.warning("Kalshi market detail ticker did not match selected ticker; refusing order")
            return None
        verified_status=str(_value(verified,"status") or "unknown").lower()
        if verified_status not in {"open", "active"}:
            log.warning("Selected crypto market is not tradable: ticker=%s status=%s; refusing order", m["ticker"], verified_status)
            return None
        verified_close=_value(verified,"close_time","expiration_time","expected_expiration_time")
        try:
            verified_close_ts=self._utc_timestamp(verified_close).timestamp()
        except Exception:
            log.warning("Selected crypto market has no verifiable close timestamp")
            return None
        if not (hour < verified_close_ts <= hour+3600+2):
            log.warning("Selected crypto market is not for the current hourly settlement window")
            return None
        verified_event_title=str(_value(selected["event"],"title","sub_title","subtitle") or "")
        verified_title=" ".join(x for x in (verified_event_title,str(_value(verified,"title") or ""),str(_value(verified,"subtitle") or ""),str(_value(verified,"ticker") or "")) if x)
        verified_rules=str(_value(verified,"rules_primary") or "")
        verified_prop=interpret_settlement(verified_title,str(_value(verified,"subtitle","yes_sub_title") or ""),verified_rules,str(m["ticker"]))
        if not verified_rules or verified_prop != selected["proposition"]:
            log.warning("Selected crypto market settlement rule could not be verified; refusing order")
            return None
        return {"market":verified,"proposition":verified_prop,"close_ts":verified_close_ts,"event_ticker":selected["event_ticker"]}

    @staticmethod
    def _utc_timestamp(value):
        """Parse Kalshi ISO timestamps or Unix timestamps as timezone-aware UTC."""
        from datetime import datetime, timezone
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), timezone.utc)
        if not isinstance(value, str) or not value.strip():
            raise ValueError("missing timestamp")
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timestamp has no timezone")
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _money(value, *, cents=False):
        """Parse a finite monetary value, converting integer-cent fields to dollars."""
        if value is None or isinstance(value, bool):
            raise ValueError("missing monetary field")
        amount = Decimal(str(value))
        if not amount.is_finite():
            raise ValueError("non-finite monetary field")
        return amount / Decimal("100") if cents else amount

    @classmethod
    def _settlement_net_pnl_dollars(cls, row):
        """Calculate one Kalshi settlement's net P&L in dollars.

        Current dollar-suffixed cost fields are dollars; legacy yes_total_cost and
        no_total_cost are cents. Kalshi `revenue` is cents; `fee_cost` is dollars.
        Missing cost/fee fields are not silently treated as zero.
        """
        revenue_dollars = _value(row, "revenue_dollars")
        revenue_cents = _value(row, "revenue")
        if revenue_dollars is not None:
            revenue = cls._money(revenue_dollars)
        elif revenue_cents is not None:
            revenue = cls._money(revenue_cents, cents=True)
        else:
            raise ValueError("settlement missing revenue")

        yes_cost_dollars = _value(row, "yes_total_cost_dollars")
        yes_cost_cents = _value(row, "yes_total_cost")
        no_cost_dollars = _value(row, "no_total_cost_dollars")
        no_cost_cents = _value(row, "no_total_cost")
        if yes_cost_dollars is not None:
            yes_cost = cls._money(yes_cost_dollars)
        elif yes_cost_cents is not None:
            yes_cost = cls._money(yes_cost_cents, cents=True)
        else:
            raise ValueError("settlement missing YES cost basis")
        if no_cost_dollars is not None:
            no_cost = cls._money(no_cost_dollars)
        elif no_cost_cents is not None:
            no_cost = cls._money(no_cost_cents, cents=True)
        else:
            raise ValueError("settlement missing NO cost basis")

        fee = _value(row, "fee_cost", "fees_paid_dollars")
        if fee is None:
            raise ValueError("settlement missing fee field")
        fees = cls._money(fee)
        if fees < 0:
            raise ValueError("negative settlement fee")
        return revenue - yes_cost - no_cost - fees

    @classmethod
    def _fill_price_dollars(cls, fill, side):
        """Read the traded outcome's price from current or legacy Kalshi fill fields."""
        for key in (f"{side}_price_dollars", f"{side}_price_fixed"):
            value = _value(fill, key)
            if value is not None:
                price = cls._money(value)
                break
        else:
            value = _value(fill, f"{side}_price")
            if value is None:
                raise ValueError(f"fill missing {side.upper()} price")
            price = cls._money(value, cents=True)
        if price < 0 or price > 1:
            raise ValueError("fill price outside 0-1 dollar range")
        return price

    @classmethod
    def _fill_count(cls, fill):
        value = _value(fill, "count_fp", "count")
        if value is None:
            raise ValueError("fill missing contract count")
        count = Decimal(str(value))
        if not count.is_finite() or count <= 0:
            raise ValueError("invalid fill contract count")
        return count

    def _verified_daily_realized_pnl(self, now):
        """Calculate UTC-day realized P&L from settlements and FIFO-matched sales.

        The entire available fill history is paginated so a sale can be matched to
        the original purchase, including purchases made on earlier days. If a sale
        cannot be matched to an existing long lot, or any required fill field is
        missing, verification fails closed instead of guessing.
        """
        from datetime import datetime, timezone
        now_dt = datetime.fromtimestamp(float(now), timezone.utc)
        day_start = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)

        def fetch_all_pages(method, response_key, **base_params):
            rows = []
            cursor = None
            seen_cursors = set()
            # A hard bound protects the bot from a broken cursor/API response.
            for _ in range(1000):
                params = dict(base_params)
                if cursor:
                    params["cursor"] = cursor
                response = method(**params)
                page_rows = _value(response, response_key)
                if not isinstance(page_rows, list):
                    raise ValueError(f"{response_key} response is not a list")
                rows.extend(page_rows)
                next_cursor = _value(response, "cursor")
                if not next_cursor:
                    return rows
                next_cursor = str(next_cursor)
                if next_cursor in seen_cursors:
                    raise ValueError("pagination cursor repeated")
                seen_cursors.add(next_cursor)
                cursor = next_cursor
            raise ValueError("pagination exceeded safety limit")

        settlements = fetch_all_pages(
            self.client.settlements, "settlements", limit=200,
            min_ts=int(day_start.timestamp()), max_ts=int(now_dt.timestamp()), subaccount=0
        )
        daily_pnl = Decimal("0")
        for row in settlements:
            if not isinstance(row, dict):
                raise ValueError("settlement row is not an object")
            settled_time = _value(row, "settled_time")
            settled_dt = self._utc_timestamp(settled_time)
            if settled_dt.date() == now_dt.date():
                daily_pnl += self._settlement_net_pnl_dollars(row)

        # Fetch all available fills, not just today's fills: today's sales may close
        # positions bought on earlier days. FIFO lots are separated by market ticker
        # and outcome side. We intentionally do not guess at missing historical lots.
        fills = fetch_all_pages(self.client.fills, "fills", limit=200, subaccount=0)
        normalized = []
        for fill in fills:
            if not isinstance(fill, dict):
                raise ValueError("fill row is not an object")
            created = self._utc_timestamp(_value(fill, "created_time", "ts"))
            action = str(_value(fill, "action") or "").strip().lower()
            side = str(_value(fill, "side", "outcome_side") or "").strip().lower()
            ticker = str(_value(fill, "ticker", "market_ticker") or "").strip()
            if action not in ("buy", "sell") or side not in ("yes", "no") or not ticker:
                raise ValueError("fill direction, side, or ticker is unrecognized")
            count = self._fill_count(fill)
            price = self._fill_price_dollars(fill, side)
            fee_raw = _value(fill, "fee_cost")
            if fee_raw is None:
                raise ValueError("fill missing fee_cost; realized P&L cannot be verified")
            fee = self._money(fee_raw)
            if fee < 0:
                raise ValueError("fill has negative fee_cost")
            normalized.append((created, ticker, side, action, count, price, fee))

        normalized.sort(key=lambda item: item[0])
        # FIFO inventory supports both long and short contracts. Kalshi permits
        # sell fills that open a short position; treating every sell as a close
        # falsely reports a missing purchase lot. Each lot is
        # [signed_remaining_count, entry_price, entry_fee_per_contract].
        # Positive quantity is long; negative quantity is short.
        from collections import defaultdict, deque
        lots = defaultdict(deque)
        for created, ticker, side, action, count, price, fee in normalized:
            key = (ticker, side)
            trade_sign = Decimal("1") if action == "buy" else Decimal("-1")
            remaining = count
            fee_per_contract = fee / count
            realized = Decimal("0")
            queue = lots[key]

            # Match this fill against FIFO inventory on the opposite side.
            while remaining > 0 and queue and queue[0][0] * trade_sign < 0:
                lot_count, entry_price, entry_fee_per_contract = queue[0]
                matched = min(remaining, abs(lot_count))
                if lot_count > 0:  # close a long by selling
                    gross = (price - entry_price) * matched
                else:  # close a short by buying back
                    gross = (entry_price - price) * matched
                realized += gross - (entry_fee_per_contract * matched) - (fee_per_contract * matched)
                if lot_count > 0:
                    lot_count -= matched
                else:
                    lot_count += matched
                remaining -= matched
                if lot_count == 0:
                    queue.popleft()
                else:
                    queue[0][0] = lot_count

            # Any unmatched remainder opens/increases a position. It is not
            # realized P&L until a later opposite-side fill or settlement.
            if remaining > 0:
                queue.append([trade_sign * remaining, price, fee_per_contract])

            # Count P&L on the date the position is closed, whether that close
            # was a sell (closing long) or a buy (covering short).
            if created.date() == now_dt.date():
                daily_pnl += realized
        return daily_pnl

    def log_account_status(self):
            """Log read-only account connectivity and the bot's current configured budget."""
            try:
                balance_data = self.client.balance()
                available = _dollars_from_balance(balance_data)
                if available is None:
                    raise ValueError("balance response did not contain a recognized cash-balance field")

                equity_raw = _value(balance_data, "balance")
                if equity_raw is None:
                    raise ValueError("balance response did not contain the expected balance field")
                equity = Decimal(str(equity_raw)) / Decimal("100")
                if not equity.is_finite() or equity < 0:
                    raise ValueError("balance value was invalid")

                # Read every position page so the displayed exposure is not based only on
                # the first page. Unknown fields fail closed instead of showing a guess.
                positions = []
                cursor = None
                seen_cursors = set()
                for _ in range(1000):
                    params = {"limit": 200}
                    if cursor:
                        params["cursor"] = cursor
                    page_data = self.client.positions(**params)
                    rows = _value(page_data, "market_positions")
                    if rows is None:
                        rows = _value(page_data, "positions")
                    if not isinstance(rows, list):
                        raise ValueError("positions response schema was not recognized")
                    positions.extend(rows)
                    next_cursor = _value(page_data, "cursor")
                    if not next_cursor:
                        break
                    next_cursor = str(next_cursor)
                    if next_cursor in seen_cursors:
                        raise ValueError("positions pagination cursor repeated")
                    seen_cursors.add(next_cursor)
                    cursor = next_cursor
                else:
                    raise ValueError("positions pagination exceeded safety limit")

                exposure = Decimal("0")
                active_positions = 0
                for row in positions:
                    quantity_raw = _value(row, "position_fp", "position")
                    if quantity_raw is None:
                        raise ValueError("position quantity field was missing")
                    quantity = Decimal(str(quantity_raw))
                    if not quantity.is_finite():
                        raise ValueError("position quantity was invalid")
                    if quantity != 0:
                        active_positions += 1
                    exposure_dollars = _value(row, "market_exposure_dollars", "event_exposure_dollars")
                    exposure_cents = _value(row, "market_exposure", "total_cost")
                    if exposure_dollars is not None:
                        amount = Decimal(str(exposure_dollars))
                    elif exposure_cents is not None:
                        amount = Decimal(str(exposure_cents)) / Decimal("100")
                    else:
                        raise ValueError("position exposure field was missing")
                    if not amount.is_finite() or amount < 0:
                        raise ValueError("position exposure was invalid")
                    exposure += amount

                budget = self.controls.budget(available, equity, exposure, Decimal("0"))
                log.info(
                    "KALSHI ACCOUNT STATUS | API=OK | available_cash=$%.2f | "
                    "position_exposure=$%.2f | active_positions=%d/%d | "
                    "max_new_trade_budget=$%.2f | BALANCE_USE=%.0f%% | "
                    "MAX_ACCOUNT_EXPOSURE=%.0f%% | MAX_CONTRACTS_PER_ORDER=%d | "
                    "LIVE_TRADING=%s | EMERGENCY_STOP=%s",
                    available, exposure, active_positions, self.max_positions, budget,
                    float(self.controls.balance_use * 100),
                    float(self.controls.max_account_exposure * 100), self.max_contracts,
                    self.controls.live_trading, self.controls.kill_switch,
                )
                return True
            except Exception as exc:
                # Avoid printing credentials or request headers; report a concise failure.
                reason = str(exc).strip() or exc.__class__.__name__
                log.warning("KALSHI ACCOUNT STATUS | API=FAILED/UNVERIFIED | reason=%s", reason)
                return False

    def startup_execution_check(self):
        """Run read-only account/API checks and reconcile before allowing entries.

        This does not submit a test order, so PASS means the account and execution
        prerequisites responded successfully, not that an order has been filled.
        """
        account_ok = self.log_account_status()
        live_enabled = bool(self.controls.live_trading)
        emergency_stop = bool(self.controls.kill_switch)
        reconcile_ok = self.reconcile_before_entry()
        ready = account_ok and reconcile_ok and live_enabled and not emergency_stop
        log.info(
            "KALSHI LIVE EXECUTION SELF-CHECK | %s | account_api=%s | "
            "order_reconciliation=%s | LIVE_TRADING=%s | EMERGENCY_STOP=%s | "
            "ORDER_SUBMISSION=NOT_TESTED",
            "PASS - prerequisites ready" if ready else "BLOCKED - see checks above",
            "PASS" if account_ok else "FAIL/UNVERIFIED",
            "PASS" if reconcile_ok else "BLOCKED/UNVERIFIED",
            live_enabled,
            emergency_stop,
        )
        return ready

    def maybe_trade(self, report, snapshot, now=None):
        now=time.time() if now is None else now
        # Never let configuration alone bypass any gate.
        if not self.controls.live_trading: return "BLOCKED: LIVE_TRADING=false"
        if self.controls.kill_switch: return "BLOCKED: EMERGENCY_STOP"
        # Reconcile at least every 15 seconds so open orders cannot remain invisible,
        # while avoiding multiple authenticated API calls on every analysis tick.
        if now-self._last_reconcile_at >= 15.0 or not self._reconciled:
            if now-self._last_reconcile_at < 15.0 and self._last_reconcile_at > 0 and not self._reconciled:
                return "BLOCKED: reconciliation retry cooldown"
            if not self.reconcile_before_entry(now=now):
                return "BLOCKED: reconciliation incomplete or active order exists"
        if not isinstance(report,dict) or report.get("status")!="OK": return "BLOCKED: forecast unavailable"
        price=snapshot.get("price")
        raw_age=snapshot.get("price_age_seconds")
        if raw_age is None:
            raw_age=snapshot.get("age", 9999)
        try:
            price_age=float(raw_age)
        except (TypeError, ValueError):
            price_age=9999.0
        if price is None or price_age > 5:
            return "BLOCKED: stale underlying price"

        remaining=3600-(now%3600)
        start=float(os.getenv("CALLOUT_START_MINUTES","30"))
        late=float(os.getenv("CALLOUT_LATE_CUTOFF_MINUTES","10"))
        end=float(os.getenv("CALLOUT_END_MINUTES","5"))
        rem_min=remaining/60
        if not (end < rem_min <= start):
            return "BLOCKED: outside strict trading window"

        lock=report.get("lock") or {}
        path_state=str(lock.get("path_state", "")).strip().upper()
        locked=bool(lock.get("locked"))
        invalidated=bool(lock.get("invalidated"))
        # Standard entries require an intact, locked, advancing path. In the
        # final 10-to-5 minute window, the strategy may explicitly emit
        # LATE_HIGH_CONFIDENCE_NO_PATH; that narrowly-scoped fallback requires
        # both overall and outcome confidence >= 75%, plus a directional outcome.
        try:
            outcome_confidence=float(report.get("outcome_confidence", -1))
        except (TypeError, ValueError):
            outcome_confidence=-1.0
        if outcome_confidence > 1.0:
            outcome_confidence /= 100.0
        try:
            overall_confidence=float(report.get("confidence", -1))
        except (TypeError, ValueError):
            overall_confidence=-1.0
        if overall_confidence > 1.0:
            overall_confidence /= 100.0

        callout_state=str(report.get("callout_state", "")).strip().upper()
        fallback_no_path=(
            end < rem_min < late
            and callout_state == "LATE_HIGH_CONFIDENCE_NO_PATH"
            and overall_confidence >= 0.75
            and outcome_confidence >= 0.75
            and report.get("outcome") not in (None, "", "NO CLEAR OUTCOME", "WAITING FOR SETUP")
            and not invalidated
        )
        path_ok=path_state in ("PATH ADVANCING", "ADVANCING") and locked and not invalidated
        if not path_ok and not fallback_no_path:
            return "BLOCKED: no locked advancing path or qualifying late high-confidence fallback"
        min_outcome_confidence=float(os.getenv("MIN_OUTCOME_CONFIDENCE", "0.62"))
        if fallback_no_path:
            min_outcome_confidence=max(min_outcome_confidence, 0.75)
        if outcome_confidence < min_outcome_confidence:
            return f"BLOCKED: outcome confidence below {min_outcome_confidence:.0%} threshold"
        # Daily realized-loss limit intentionally disabled per user request.
        # All other market, exposure, order, reconciliation, and emergency-stop
        # checks remain in force.
        found=self.discover_hourly_crypto_market(
            now, target_level=report.get("target_level"),
            reference_price=snapshot.get("price"),
            allow_nearest=True
        )
        if not found: return "BLOCKED: no verified eligible hourly crypto contract"
        market=found["market"]; ticker=market["ticker"]; hour=int(now//3600)*3600
        if self.store.has_hour_order(hour,ticker): return "BLOCKED: hourly duplicate prevention"
        if now >= found["close_ts"]-self.cutoff: return "BLOCKED: exchange cutoff"
        prop=found["proposition"]
        # The analysis engine owns the prediction. Use its confirmed locked outcome;
        # do not recalculate direction from projected_finish in execution.
        locked_outcome=lock.get("locked_outcome") if path_ok else report.get("outcome")
        if not locked_outcome:
            locked_outcome=report.get("outcome")
        outcome_target=_outcome_target(locked_outcome)
        target_level=report.get("target_level")
        try:
            target_level_d=Decimal(str(target_level)) if target_level is not None else None
        except Exception:
            target_level_d=None
        if outcome_target is None:
            return "BLOCKED: locked analysis outcome missing/ambiguous"
        if target_level_d is not None and abs(outcome_target-target_level_d) > Decimal("1"):
            return "BLOCKED: locked outcome target does not match analysis checkpoint"
        step = Decimal(str(os.getenv("TARGET_STEP", "1")))
        tolerance = Decimal(os.getenv("KALSHI_STRIKE_MATCH_TOLERANCE_STEPS", "1")) * step
        if abs(prop["strike"]-outcome_target) > tolerance:
            return "BLOCKED: selected contract strike does not match locked outcome target"
        side=_side_from_locked_outcome(locked_outcome,prop)
        if side is None:
            return "BLOCKED: locked outcome cannot be mapped to verified contract"
        log.info("TRADE DECISION | outcome=%s | contract=%s | strike=$%s | side=%s | confidence=%.1f%% | mode=%s",
                 locked_outcome, ticker, prop["strike"], side, outcome_confidence*100.0,
                 "LATE_75_NO_PATH" if fallback_no_path else "LOCKED_PATH")
        # Never let an order-book API failure crash the worker. Fail closed and
        # pause repeated requests after rate limits or transient API errors.
        if now < self._orderbook_backoff_until:
            wait_seconds=max(1, int(self._orderbook_backoff_until-now))
            return f"BLOCKED: orderbook cooldown ({wait_seconds}s remaining)"
        try:
            book=self.client.orderbook(ticker)
        except Exception as exc:
            message=str(exc).strip() or exc.__class__.__name__
            is_rate_limit=("429" in message or "too_many_requests" in message.lower()
                           or "rate limit" in message.lower())
            self._orderbook_backoff_until=now+self._orderbook_error_backoff
            if is_rate_limit:
                log.warning("Kalshi orderbook rate-limited for %s; pausing orderbook requests for %.0fs: %s",
                            ticker, self._orderbook_error_backoff, message)
                return "BLOCKED: orderbook rate-limited; cooldown active"
            log.warning("Kalshi orderbook unavailable for %s; live entry blocked for %.0fs: %s",
                        ticker, self._orderbook_error_backoff, message)
            return "BLOCKED: orderbook API error; cooldown active"
        ob_fp=_value(book,"orderbook_fp")
        if isinstance(ob_fp,dict):
            ob=ob_fp
            own_bids=_value(ob,"yes_dollars" if side=="yes" else "no_dollars")
            opposite_bids=_value(ob,"no_dollars" if side=="yes" else "yes_dollars")
            dollar_prices=True
        else:
            ob=_value(book,"orderbook")
            if not isinstance(ob,dict): ob=book
            own_bids=_value(ob,side)
            opposite_bids=_value(ob,"no" if side=="yes" else "yes")
            dollar_prices=False
        # Kalshi returns bids only. Convert complementary bids to executable asks.
        # Current orderbook_fp levels use dollar prices and fixed-point quantities;
        # legacy orderbook levels use integer-cent prices and integer quantities.
        def parse_levels(levels):
            parsed=[]
            if not isinstance(levels,list): return parsed
            for level in levels:
                if not isinstance(level,(list,tuple)) or len(level)<2:
                    continue
                try:
                    raw_price=Decimal(str(level[0]))
                    raw_qty=Decimal(str(level[1]))
                    price=raw_price*Decimal("100") if dollar_prices else raw_price
                    if not price.is_finite() or not raw_qty.is_finite(): continue
                    price_int=int(price)
                    qty_int=int(raw_qty.to_integral_value(rounding=ROUND_FLOOR))
                    if price == Decimal(price_int) and 0 <= price_int <= 100 and qty_int > 0:
                        parsed.append((price_int,qty_int))
                except Exception:
                    continue
            return parsed
        own=parse_levels(own_bids); opposite=parse_levels(opposite_bids)
        if not opposite: return "BLOCKED: unsupported/empty complementary order book schema"
        best_bid=max((x[0] for x in own),default=None)
        asks=sorted(((100-p,q) for p,q in opposite),key=lambda x:x[0])
        best=asks[0][0]
        spread=(best-best_bid) if best_bid is not None else None
        if spread is None: return "BLOCKED: own-side bid unavailable; spread cannot be verified"
        if spread < 0 or Decimal(spread)>self.max_spread: return "BLOCKED: spread exceeds maximum or is inconsistent"
        liquidity=sum(q for ask,q in asks if ask<=best+int(self.max_spread))
        if liquidity<self.min_liquidity: return "BLOCKED: insufficient ask-side liquidity"
        if best>self.max_price: return "BLOCKED: best executable ask exceeds maximum"
        balance_data=self.client.balance()
        available=_dollars_from_balance(balance_data)
        if available is None: return "BLOCKED: available balance schema unverified"
        # Fetch every page and accept current fixed-point fields as well as legacy cents fields.
        positions=[]
        cursor=None
        seen_cursors=set()
        for _ in range(1000):
            params={"limit":200}
            if cursor:
                params["cursor"]=cursor
            positions_page=self.client.positions(**params)
            page_rows=_value(positions_page,"market_positions")
            if page_rows is None:
                page_rows=_value(positions_page,"positions")
            if not isinstance(page_rows,list):
                return "BLOCKED: positions schema unverified"
            positions.extend(page_rows)
            next_cursor=_value(positions_page,"cursor")
            if not next_cursor:
                break
            next_cursor=str(next_cursor)
            if next_cursor in seen_cursors:
                return "BLOCKED: positions pagination cursor repeated"
            seen_cursors.add(next_cursor)
            cursor=next_cursor
        else:
            return "BLOCKED: positions pagination exceeded safety limit"

        active_positions=0
        exposure=Decimal("0")
        for position_row in positions:
            quantity_raw=_value(position_row,"position_fp","position")
            if quantity_raw is None:
                return "BLOCKED: position quantity schema unverified"
            try:
                quantity=Decimal(str(quantity_raw))
                if not quantity.is_finite():
                    return "BLOCKED: position quantity schema unverified"
                if quantity != 0:
                    active_positions += 1
            except Exception:
                return "BLOCKED: position quantity schema unverified"

            exposure_dollars=_value(position_row,"market_exposure_dollars","event_exposure_dollars")
            exposure_cents=_value(position_row,"market_exposure","total_cost")
            try:
                if exposure_dollars is not None:
                    amount=Decimal(str(exposure_dollars))
                elif exposure_cents is not None:
                    amount=Decimal(str(exposure_cents))/Decimal("100")
                else:
                    return "BLOCKED: position exposure schema unverified"
                if not amount.is_finite() or amount < 0:
                    return "BLOCKED: position exposure schema unverified"
                exposure += amount
            except Exception:
                return "BLOCKED: position exposure schema unverified"
        if active_positions>=self.max_positions:
            return "BLOCKED: maximum simultaneous positions"
        equity_raw=_value(balance_data,"balance")
        if equity_raw is None: return "BLOCKED: account equity schema unverified"
        equity=Decimal(str(equity_raw))/Decimal("100")
        # Conservative maximum cost including a 10% fee reserve. Actual fee schema may vary;
        # exchange-confirmed fills are required for accounting.
        price_d=Decimal(best)/100
        # The controls budget enforces the account-wide exposure ceiling. BALANCE_USE is
        # set to 1.0 by the runner so the portfolio gate below applies the 25% cash fraction
        # exactly once and atomically across every asset worker.
        max_spend=self.controls.budget(available,equity,exposure,Decimal("0"))
        client_id=str(uuid.uuid4())
        reservation,gate_reason=reserve_entry(
            asset=os.getenv("ASSET_ID",os.getenv("PRODUCT_ID","UNKNOWN")),
            intended_dollars=float(max_spend), available_cash=float(available),
            max_per_hour=int(os.getenv("PORTFOLIO_MAX_TRADES_PER_HOUR","3")),
            fraction=float(os.getenv("PORTFOLIO_CASH_FRACTION","0.25")),
            client_order_id=client_id, now=now)
        if not reservation:
            log.info("PORTFOLIO GATE | blocked=%s",gate_reason)
            return "BLOCKED: portfolio risk gate: "+gate_reason
        per_contract=price_d+Decimal("0.10")*price_d
        reserved_budget=Decimal(str(reservation["budget"]))
        qty=min(self.max_contracts,int((reserved_budget/per_contract).to_integral_value(rounding=ROUND_FLOOR)),liquidity)
        if qty<1:
            mark_status(reservation["id"],"released")
            return "BLOCKED: portfolio risk budget cannot fund one contract"
        payload={"ticker":ticker,"client_order_id":client_id,"type":"limit","action":"buy","side":side,
                 "count":qty,("yes_price" if side=="yes" else "no_price"):best}
        self.store.create_intent(client_id,hour,ticker,side,best,qty,payload)
        self.store.transition(client_id,"SUBMITTING")
        try:
            response=self.client.create_order(payload)
        except OrderSubmissionUncertain:
            self.store.transition(client_id,"UNKNOWN_PENDING_RECONCILIATION")
            mark_status(reservation["id"],"unknown",client_id)
            log.exception("Order submit uncertain. No retry will be attempted.")
            return "UNKNOWN_PENDING_RECONCILIATION"
        except Exception:
            self.store.transition(client_id,"REJECTED")
            mark_status(reservation["id"],"failed",client_id)
            log.exception("Order submission rejected/failed")
            return "REJECTED"
        order=_value(response,"order") or response
        oid=_value(order,"order_id")
        status=str(_value(order,"status") or "").lower()
        if not oid:
            self.store.transition(client_id,"UNKNOWN_PENDING_RECONCILIATION")
            mark_status(reservation["id"],"unknown",client_id)
            return "UNKNOWN_PENDING_RECONCILIATION"
        mapped={"resting":"ACCEPTED","open":"ACCEPTED","executed":"FILLED","canceled":"CANCELLED","cancelled":"CANCELLED","rejected":"REJECTED"}
        if status in mapped:
            final_status=mapped[status]
        else:
            try:
                filled=Decimal(str(_value(order,"fill_count","fill_count_fp") or "0"))
                remaining=Decimal(str(_value(order,"remaining_count","remaining_count_fp") or "0"))
                if filled > 0 and remaining > 0:
                    final_status="PARTIALLY FILLED"
                elif filled > 0:
                    final_status="FILLED"
                elif remaining > 0:
                    final_status="ACCEPTED"
                else:
                    final_status="CANCELLED"  # IOC order with no fill
            except Exception:
                final_status="UNKNOWN_PENDING_RECONCILIATION"
        self.store.transition(client_id,final_status,str(oid))
        if final_status in ("FILLED","PARTIALLY FILLED"):
            mark_status(reservation["id"],"filled",client_id)
        elif final_status in ("ACCEPTED",):
            mark_status(reservation["id"],"submitted",client_id)
        elif final_status in ("CANCELLED","REJECTED"):
            mark_status(reservation["id"],"released",client_id)
        else:
            mark_status(reservation["id"],"unknown",client_id)
        return f"ORDER_{final_status}:{oid}"
