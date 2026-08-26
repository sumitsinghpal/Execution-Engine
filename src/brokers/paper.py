"""Deterministic paper broker used by default for paper and shadow workflows."""

import hashlib
from datetime import UTC, datetime
from typing import Any

from src.accounts.profiles import AccountProfile
from src.brokers.base import BrokerAdapter


class PaperBrokerAdapter(BrokerAdapter):
    """Simulate broker operations without network access or live side effects."""

    @staticmethod
    def _synthetic_price(symbol: str) -> float:
        """
        A stable, deterministic "current price" per symbol — same symbol
        always yields the same price within a run, so notional estimates
        stay consistent between a preview and its later execute call.
        Clearly synthetic; never presented as if it came from a real feed.
        """
        digest = hashlib.sha256(symbol.encode("utf-8")).hexdigest()
        return 20.0 + (int(digest[:8], 16) % 48000) / 100.0  # ~$20-$500 range

    async def get_quote(self, symbol: str) -> dict[str, Any]:
        mid = self._synthetic_price(symbol)
        spread = round(mid * 0.0005, 4)  # a nominal 5 bps synthetic spread
        return {
            "symbol": symbol,
            "bid": round(mid - spread, 4),
            "ask": round(mid + spread, 4),
            "last": mid,
            "quote_time": datetime.now(UTC).isoformat(),
            "mode": "PAPER",
        }

    async def preview_order(self, profile: AccountProfile, order_spec: dict[str, Any]) -> dict[str, Any]:
        quantity = order_spec.get("quantity", 0)
        limit_price = float(order_spec.get("limitPrice") or 0)
        if not limit_price:
            # MARKET order: estimate against a live quote instead of
            # silently reporting a $0 estimated investment.
            quote = await self.get_quote(order_spec.get("symbol", ""))
            limit_price = quote["last"]
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