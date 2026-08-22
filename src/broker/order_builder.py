from __future__ import annotations

from decimal import Decimal
from typing import Any

from src.models.orders import OrderType, TradeProposal


def _normalize_decimal(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return f"{value:.2f}"


def build_schwab_order_payload(proposal: TradeProposal) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "account": proposal.account,
        "orderStrategyType": "SINGLE",
        "orderType": proposal.order_type.value,
        "session": "NORMAL",
        "duration": "DAY",
        "orderLegCollection": [
            {
                "instruction": proposal.instruction.value,
                "quantity": proposal.quantity,
                "instrument": {
                    "symbol": proposal.symbol.upper(),
                    "assetType": proposal.asset_type.value,
                },
            }
        ],
    }
    if proposal.order_type == OrderType.LIMIT:
        payload["price"] = _normalize_decimal(proposal.limit_price)
    return payload
