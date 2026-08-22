"""Build account-scoped Schwab Trader API order payloads."""

from typing import Any

from src.broker.order_builder import OrderBuilder
from src.models.orders import TradeProposal


class SchwabOrderBuilderV1(OrderBuilder):
    """Compatibility wrapper for deterministic Schwab Trader API payloads."""

    def build_order_spec(self, proposal: TradeProposal, account_hash: str) -> dict[str, Any]:
        order = super().build_order_spec(proposal, account_hash)
        order["accountHash"] = order.pop("accountId")
        return order