"""
HTTP client for EDGE-TF's execution gateway (api/execution_app.py in the
EDGE-TF-disclosure-agent-engine repo — a separate process, separate repo).

That gateway is a *pull* contract: it never calls this system. This system
polls GET /execution/orders for trades EDGE-TF has already approved, claims
one atomically right before actually executing it (so two executors can
never race the same trade), and reports the broker outcome back so EDGE-TF's
own audit ledger and portfolio view stay accurate. See
src/execution/edge_tf_connector.py for how these calls are sequenced, and
src/execution/external_signals.py for what gets stored locally in between.

Nothing here is a broker call. Nothing here places an order — it only ever
moves EDGE-TF's own ExecutionInstruction/ExecutionReport JSON shapes across
the wire, unchanged.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx


class EdgeTFGatewayError(RuntimeError):
    """Raised for any non-2xx response from the EDGE-TF execution gateway."""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(f"EDGE-TF gateway error {status_code} [{code}]: {message}")
        self.status_code = status_code
        self.code = code


class EdgeTFClient:
    """
    One instance per call site — cheap, holds no persistent connection.
    base_url should be the gateway's root, e.g. "https://edge-tf.internal:8601"
    (no trailing slash required).
    """

    def __init__(self, base_url: str, token: str, timeout_sec: float = 10.0):
        if not base_url:
            raise ValueError("base_url is required")
        if not token:
            raise ValueError("token is required")
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._timeout = timeout_sec

    async def _request(self, method: str, path: str, *, json: Optional[dict] = None) -> dict:
        url = f"{self._base_url}{path}"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.request(method, url, headers=self._headers, json=json)
        if response.status_code >= 400:
            try:
                body = response.json()
            except ValueError:
                body = {}
            raise EdgeTFGatewayError(
                response.status_code,
                body.get("error", "UNKNOWN"),
                body.get("detail", response.text),
            )
        return response.json()

    async def list_orders(self) -> list[dict[str, Any]]:
        """Every trade currently APPROVED and unclaimed, as ExecutionInstruction dicts."""
        body = await self._request("GET", "/execution/orders")
        return body.get("orders", [])

    async def claim(self, trade_id: str, *, executor_id: str) -> dict[str, Any]:
        """
        Atomically claim one trade for this executor. Raises EdgeTFGatewayError
        (409 CLAIM_CONFLICT / REVALIDATION_FAILED, or 404 UNKNOWN_TRADE) if it
        can't be claimed — the caller must not execute locally in that case.
        """
        return await self._request(
            "POST", f"/execution/orders/{trade_id}/claim", json={"executor_id": executor_id}
        )

    async def report(self, trade_id: str, report: dict[str, Any]) -> dict[str, Any]:
        """Post the broker outcome for a previously claimed trade back to EDGE-TF."""
        return await self._request("POST", f"/execution/orders/{trade_id}/reports", json=report)

    async def post_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Push current broker balances/positions so EDGE-TF's portfolio view reflects reality."""
        return await self._request("POST", "/execution/portfolio/snapshots", json=snapshot)

    async def portfolio_state(self) -> dict[str, Any]:
        return await self._request("GET", "/execution/portfolio/state")
