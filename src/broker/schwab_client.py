from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx

from src.config import get_settings


class SchwabClient:
    def __init__(self) -> None:
        self.settings = get_settings()

    async def preview_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.settings.schwab_mock_mode:
            qty = Decimal(str(payload["orderLegCollection"][0]["quantity"]))
            price = Decimal(str(payload.get("price", "100")))
            return {
                "preview_id": f"mock-preview-{uuid4()}",
                "estimated_notional": str((qty * price).quantize(Decimal("0.01"))),
                "status": "ACCEPTED",
                "received_at": datetime.now(UTC).isoformat(),
            }
        return await self._request_with_retries("/trader/v1/accounts/preview", payload)

    async def submit_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.settings.schwab_mock_mode:
            return {
                "broker_order_id": f"mock-order-{uuid4()}",
                "status": "ACKNOWLEDGED",
                "submitted_at": datetime.now(UTC).isoformat(),
            }
        return await self._request_with_retries("/trader/v1/accounts/orders", payload)

    async def get_order_status(self, broker_order_id: str) -> dict[str, Any]:
        if self.settings.schwab_mock_mode:
            return {
                "broker_order_id": broker_order_id,
                "status": "ACKNOWLEDGED",
                "updated_at": datetime.now(UTC).isoformat(),
            }
        return await self._request_with_retries(f"/trader/v1/orders/{broker_order_id}", None, method="GET")

    async def _request_with_retries(
        self,
        path: str,
        payload: dict[str, Any] | None,
        method: str = "POST",
        attempts: int = 3,
    ) -> dict[str, Any]:
        timeout = httpx.Timeout(self.settings.schwab_timeout_seconds)
        async with httpx.AsyncClient(base_url=self.settings.schwab_base_url, timeout=timeout) as client:
            for i in range(attempts):
                try:
                    response = await client.request(method, path, json=payload)
                    response.raise_for_status()
                    return response.json()
                except (httpx.HTTPError, httpx.TimeoutException):
                    if i == attempts - 1:
                        raise
                    await asyncio.sleep(0.25 * (2**i))
        raise RuntimeError("unreachable")
