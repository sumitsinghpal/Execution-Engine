"""Deterministic paper broker used by default for paper and shadow workflows."""

from datetime import UTC, datetime
from typing import Any

from src.accounts.profiles import AccountProfile
from src.brokers.base import BrokerAdapter


class PaperBrokerAdapter(BrokerAdapter):
    """Simulate broker operations without network access or live side effects."""

    async def preview_order(self, profile: AccountProfile, order_spec: dict[str, Any]) -> dict[str, Any]:
        quantity = order_spec.get("quantity", 0)
        limit_price = float(order_spec.get("limitPrice", 0))
        return {
            "orderId": f"paper-preview-{order_spec['orderId']}",
            "estimatedCommission": 0.0,
            "estimatedTotalInvestment": quantity * limit_price,
            "status": "OK",
            "symbol": order_spec.get("symbol"),
            "quantity": quantity,
            "mode": "PAPER",
        }

    async def submit_order(self, profile: AccountProfile, order_spec: dict[str, Any]) -> dict[str, Any]:
        return {
            "orderId": f"paper-order-{order_spec['orderId']}",
            "status": "ACCEPTED",
            "symbol": order_spec.get("symbol"),
            "quantity": order_spec.get("quantity"),
            "enteredTime": datetime.now(UTC).isoformat(),
            "mode": "PAPER",
        }

    async def get_order_status(self, profile: AccountProfile, order_id: str) -> dict[str, Any]:
        return {"orderId": order_id, "status": "FILLED", "filledQuantity": 0, "mode": "PAPER"}

    async def list_accounts(self) -> list[dict[str, Any]]:
        return [{"alias": "paper", "broker": "paper", "mode": "PAPER"}]

    async def get_positions(self, profile: AccountProfile) -> list[dict[str, Any]]:
        return []

    async def get_balances(self, profile: AccountProfile) -> dict[str, Any]:
        return {"availableFunds": 0, "mode": "PAPER"}