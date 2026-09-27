"""
MCP tool surface for Execution-Engine — a trusted bridge onto the
existing, already-safety-gated HTTP API (see src/api/server.py), not a
new capability of its own. Each tool below is a thin httpx call against
one of those same routes; every check that already applies over plain
HTTP — approval binding, risk checks, kill switch, idempotency,
broker/account routing — applies here unchanged, because these tools
call exactly the routes a human HTTP client would.

Deliberately no `import mcp` at module level: register() takes the
FastMCP instance as a plain Any and only calls its .tool() decorator on
it. The real `mcp` PyPI package's pinned dependencies (pydantic>=2.8,
anyio>=4.6, starlette>=0.39 even on its oldest 1.x release) are
incompatible with this repo's own pinned FastAPI stack
(fastapi==0.104.1/pydantic==2.5.0/starlette==0.27.0) — installing it
into this venv upgrades those and breaks the app outright (verified: it
silently pulled in a newer pydantic/starlette/anyio and every FastAPI
route failed to import). See docs/MCP_BRIDGE.md for why the MCP server
process runs in its own separate virtualenv instead. Keeping this module
free of that import lets it be tested here, in the main venv/test suite,
with a lightweight fake `mcp` double (tests/test_mcp_tools.py) — nothing
in this file needs the real package.

The admin key is injected here, server-side, from settings.api_key_admin
— never a parameter any tool accepts from its caller. An MCP caller can
do exactly what an authenticated HTTP caller could, no more, no less.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

from src.config import get_settings


def _client() -> httpx.AsyncClient:
    settings = get_settings()
    return httpx.AsyncClient(
        base_url=settings.mcp_target_base_url,
        headers={"x-admin-key": settings.api_key_admin},
        timeout=30.0,
    )


async def _get(path: str, params: Optional[dict] = None) -> dict:
    async with _client() as client:
        response = await client.get(path, params=params)
    response.raise_for_status()
    return response.json()


async def _post(path: str, json: Optional[dict] = None, params: Optional[dict] = None) -> dict:
    async with _client() as client:
        response = await client.post(path, json=json, params=params)
    response.raise_for_status()
    return response.json()


def register(mcp: Any) -> None:
    @mcp.tool()
    async def health() -> dict:
        """Report database and broker connectivity, and whether the service is healthy."""
        return await _get("/v1/health")

    @mcp.tool()
    async def preview_order(
        decision_id: str,
        account: str,
        symbol: str,
        asset_type: str,
        instruction: str,
        quantity: int,
        order_type: str = "MARKET",
        agent_id: str = "default",
        limit_price: Optional[str] = None,
        stop_price: Optional[str] = None,
    ) -> dict:
        """
        Preview a trade proposal: validates it, runs every existing risk
        check, and returns the configured broker's own preview. Never
        submits anything — execute_order is a separate, explicit call.
        """
        body = {
            "decision_id": decision_id,
            "agent_id": agent_id,
            "account": account,
            "symbol": symbol,
            "asset_type": asset_type,
            "instruction": instruction,
            "quantity": quantity,
            "order_type": order_type,
            "limit_price": limit_price,
            "stop_price": stop_price,
        }
        return await _post("/v1/orders/preview", json=body)

    @mcp.tool()
    async def execute_order(
        decision_id: str,
        preview_id: str,
        approved_by: str,
        approved_at: str,
        attestation: str,
        idempotency_key: str,
    ) -> dict:
        """
        Execute a previously-previewed order. Requires a genuine approval
        artifact (approved_by/approved_at/attestation/idempotency_key) —
        this tool cannot manufacture approval, only forward it; the engine
        itself still enforces the approval-matches-preview binding, preview
        expiry, the kill switch, and every other server-side gate.
        """
        body = {
            "decision_id": decision_id,
            "preview_id": preview_id,
            "approval": {
                "preview_id": preview_id,
                "approved_by": approved_by,
                "approved_at": approved_at,
                "attestation": attestation,
                "idempotency_key": idempotency_key,
            },
        }
        return await _post("/v1/orders/execute", json=body)

    @mcp.tool()
    async def get_order_status(decision_id: str) -> dict:
        """Current status of a previously previewed/executed order."""
        return await _get(f"/v1/orders/{decision_id}")

    @mcp.tool()
    async def cancel_order(decision_id: str) -> dict:
        """Request cancellation at the broker for an order still in flight."""
        return await _post(f"/v1/orders/{decision_id}/cancel")

    @mcp.tool()
    async def get_positions(account: str) -> dict:
        """Read-only broker-reported positions for one account alias."""
        return await _get(f"/v1/account/{account}/positions")

    @mcp.tool()
    async def reconcile(account: str = "primary") -> dict:
        """
        Compare this system's believed positions against what the broker
        actually reports for the account; any mismatch trips the kill
        switch automatically — this can only halt trading, never resume it.
        """
        return await _post("/v1/reconciliation/positions", params={"account": account})
