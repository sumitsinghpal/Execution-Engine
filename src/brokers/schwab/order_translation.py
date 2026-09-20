"""
Translation between this system's broker-neutral order spec and Schwab's
Trader API order format.

Why this exists: OrderBuilder.build_order_spec() produces a FLAT, broker-neutral
dict ({"symbol", "quantity", "instruction", "orderType", "limitPrice", ...}) that
the paper broker consumes directly. Schwab's Trader API does not accept that
shape. It expects a nested order — orderStrategyType / session / duration /
orderLegCollection[{instruction, quantity, instrument{symbol, assetType}}] —
with the limit price called `price`. Sending the flat spec to previewOrder would
be a 400 on first live contact. The translation lives here, in the Schwab
adapter's own package, so nothing that speaks the neutral spec (the paper broker,
the risk checker, the audit trail) has to change.

Everything unsupported FAILS CLOSED with a BrokerError rather than being guessed:
this is money.

Honest limit: this encodes Schwab's documented order schema. It has not been run
against the live API (no refresh token has been issued yet); Schwab's own
previewOrder is the real check, which is exactly what the adapter calls next.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from src.brokers.base import BrokerError

# ETFs are ordered as EQUITY in Schwab's order schema. BOND and FUTURE are not
# translated: guessing an instrument shape for asset classes this system has never
# sent to Schwab is exactly how a wrong order gets placed.
_STOCK_LIKE = {"EQUITY", "ETF"}
_ORDER_TYPES = {"MARKET", "LIMIT", "STOP", "STOP_LIMIT"}
_OPTION_SYMBOL_LENGTH = 21

# TradeProposal.instruction is only BUY/SELL, but Schwab wants open/close intent for
# options. Map conservatively: BUY opens a long; SELL can only CLOSE one. This makes it
# impossible to write (sell-to-open) an uncovered option by accident — Schwab rejects a
# SELL_TO_CLOSE with nothing to close, which is the safe failure.
_OPTION_INSTRUCTIONS = {"BUY": "BUY_TO_OPEN", "SELL": "SELL_TO_CLOSE"}
_EQUITY_INSTRUCTIONS = {"BUY": "BUY", "SELL": "SELL"}


def _price(spec: dict[str, Any], key: str) -> Optional[str]:
    raw = spec.get(key)
    if raw in (None, ""):
        return None
    try:
        value = Decimal(str(raw))
    except InvalidOperation:
        raise BrokerError(f"{key} is not a number: {raw!r}") from None
    if not value.is_finite() or value <= 0:
        raise BrokerError(f"{key} must be positive, got {raw!r}")
    return format(value, "f")


def to_schwab_order(order_spec: dict[str, Any]) -> dict[str, Any]:
    """Build the nested Schwab order for a neutral spec, or raise BrokerError."""
    symbol = order_spec.get("symbol")
    if not isinstance(symbol, str) or not symbol:
        raise BrokerError("order spec has no symbol")
    asset = str(order_spec.get("assetType") or "EQUITY").upper()
    side = str(order_spec.get("instruction") or "").upper()
    order_type = str(order_spec.get("orderType") or "").upper()
    quantity = order_spec.get("quantity")

    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
        raise BrokerError(f"quantity must be a positive whole number, got {quantity!r}")
    if order_type not in _ORDER_TYPES:
        raise BrokerError(f"orderType {order_type or None!r} is not translatable to Schwab (supported: {sorted(_ORDER_TYPES)})")

    if asset in _STOCK_LIKE:
        schwab_asset, instructions = "EQUITY", _EQUITY_INSTRUCTIONS
    elif asset == "OPTION":
        if len(symbol) != _OPTION_SYMBOL_LENGTH:
            raise BrokerError(f"option symbol must be the 21-character padded form, got {symbol!r}")
        schwab_asset, instructions = "OPTION", _OPTION_INSTRUCTIONS
    else:
        raise BrokerError(f"assetType {asset!r} is not supported by the Schwab translation")
    if side not in instructions:
        raise BrokerError(f"instruction {side or None!r} is not translatable to Schwab (supported: BUY, SELL)")

    limit, stop = _price(order_spec, "limitPrice"), _price(order_spec, "stopPrice")
    if order_type in ("LIMIT", "STOP_LIMIT") and limit is None:
        raise BrokerError(f"{order_type} order has no limitPrice")
    if order_type in ("STOP", "STOP_LIMIT") and stop is None:
        raise BrokerError(f"{order_type} order has no stopPrice")
    if order_type == "MARKET" and (limit is not None or stop is not None):
        raise BrokerError("MARKET order must not carry a limitPrice or stopPrice")
    if order_type == "LIMIT" and stop is not None:
        raise BrokerError("LIMIT order must not carry a stopPrice")
    if order_type == "STOP" and limit is not None:
        raise BrokerError("STOP order must not carry a limitPrice")

    order: dict[str, Any] = {
        "orderType": order_type,
        "session": "NORMAL",
        "duration": "DAY",
        "orderStrategyType": "SINGLE",
        "orderLegCollection": [
            {
                "instruction": instructions[side],
                "quantity": quantity,
                "instrument": {"symbol": symbol, "assetType": schwab_asset},
            }
        ],
    }
    if limit is not None:
        order["price"] = limit  # Schwab calls the limit price `price`
    if stop is not None:
        order["stopPrice"] = stop
    return order


def extract_order_value(response: Any) -> Optional[float]:
    """The dollar value Schwab's previewOrder reports, or None if the response has no such field."""
    if not isinstance(response, dict):
        return None
    for holder in (response.get("orderStrategy"), response):
        balance = holder.get("orderBalance") if isinstance(holder, dict) else None
        value = balance.get("orderValue") if isinstance(balance, dict) else None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _commission(response: dict[str, Any]) -> float:
    total = 0.0
    legs = ((response.get("commissionAndFee") or {}).get("commission") or {}).get("commissionLegs") or []
    for leg in legs:
        for item in (leg or {}).get("commissionValues") or []:
            value = (item or {}).get("value")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                total += float(value)
    return round(total, 2)


def normalize_preview(response: Any, order_value: float, value_source: str) -> dict[str, Any]:
    """
    Reshape Schwab's previewOrder response into the keys Executor.preview_order
    reads. Before this, Executor read `estimatedTotalInvestment` — a key only the
    paper broker returns — so a real Schwab preview would have shown a $0 cost
    and silently ignored anything Schwab rejected.
    """
    payload = response if isinstance(response, dict) else {}
    validation = payload.get("orderValidationResult") or {}
    rejects = [str(item.get("message", item)) if isinstance(item, dict) else str(item) for item in validation.get("rejects") or []]
    return {
        "schwabPreview": payload,
        "estimatedTotalInvestment": round(order_value, 2),
        "estimatedCommission": _commission(payload),
        "status": "REJECTED" if rejects else "OK",
        "rejects": rejects,
        "estimateSource": value_source,  # "schwab" or "local_estimate" — a human should know which
        "mode": "LIVE",
    }
