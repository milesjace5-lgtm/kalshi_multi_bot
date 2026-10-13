"""Fail-closed live-trading controls with explicit configuration status.

These controls validate operator configuration and enforce exposure budgets.
They do not certify API authentication, market selection, or order execution;
LiveExecution must independently verify those before submitting an order.
"""
from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR, InvalidOperation
import os

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


def flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ValueError(f"{name} must be true/false (or 1/0); received an invalid value")


def _decimal_env(name: str, default: str) -> Decimal:
    raw = os.getenv(name, default).strip()
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError):
        raise ValueError(f"{name} must be a valid number") from None
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")
    return value


@dataclass(frozen=True)
class TradingControls:
    live_trading: bool
    balance_use: Decimal
    max_account_exposure: Decimal
    daily_loss_limit: Decimal
    kill_switch: bool

    @classmethod
    def from_environment(cls):
        result = cls(
            live_trading=flag("LIVE_TRADING", False),
            balance_use=_decimal_env("BALANCE_USE", "0.25"),
            max_account_exposure=_decimal_env("MAX_ACCOUNT_EXPOSURE", "0.25"),
            daily_loss_limit=_decimal_env("DAILY_LOSS_LIMIT_DOLLARS", "25"),
            kill_switch=flag("EMERGENCY_STOP", False),
        )
        if not Decimal("0") < result.balance_use <= Decimal("1"):
            raise ValueError("BALANCE_USE must be >0 and <=1 (0.25 means 25%)")
        if not Decimal("0") < result.max_account_exposure <= Decimal("1"):
            raise ValueError("MAX_ACCOUNT_EXPOSURE must be >0 and <=1")
        if result.daily_loss_limit < 0:
            raise ValueError("DAILY_LOSS_LIMIT_DOLLARS must be nonnegative")
        return result

    def budget(self, verified_available_dollars, verified_account_equity_dollars,
               existing_exposure_dollars, fees_dollars=0):
        """Return a conservative dollar budget from verified account values."""
        available = Decimal(str(verified_available_dollars))
        equity = Decimal(str(verified_account_equity_dollars))
        exposure = Decimal(str(existing_exposure_dollars))
        fees = Decimal(str(fees_dollars))
        values = (available, equity, exposure, fees)
        if any(not v.is_finite() for v in values) or min(values) < 0:
            raise ValueError("account values and fees must be finite and nonnegative")
        exposure_room = max(Decimal("0"), equity * self.max_account_exposure - exposure)
        limit = min(available * self.balance_use, exposure_room) - fees
        return max(Decimal("0"), limit).quantize(Decimal("0.01"), rounding=ROUND_FLOOR)

    def entry_status(self):
        """Describe configuration only; this is not an execution-readiness claim."""
        if self.kill_switch:
            return "BLOCKED: EMERGENCY_STOP"
        if not self.live_trading:
            return "BLOCKED: LIVE_TRADING=false"
        return "CONFIGURED: LIVE_TRADING enabled; API, market, and risk checks still required"
