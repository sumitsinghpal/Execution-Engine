"""
Tests for the MCP tool bridge (src/mcp/tools.py) — see that module's
docstring for why it's exercised here, in the main venv, without ever
importing the real `mcp` package: register() only needs an object with a
.tool() decorator, so a minimal fake stands in for FastMCP. What actually
matters is proven here: every tool is a thin HTTP call onto the SAME
already-safety-gated API routes (same httpx.ASGITransport + monkeypatch
technique tests/test_trade_tf_contract.py already uses), so execute_order
still refuses without a real, matching approval artifact, and the admin
key is injected server-side rather than accepted as a tool parameter.
"""

import inspect
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
import pytest

from src.mcp import tools as mcp_tools


class _FakeMCP:
    """Stands in for mcp.server.fastmcp.FastMCP — captures each @mcp.tool() function by name so tests can call it directly."""

    def __init__(self):
        self.tools: dict[str, Any] = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


@pytest.fixture
def registered_tools(app_with_test_db, test_settings, monkeypatch):
    """
    Routes every httpx.AsyncClient the tools create through the real ASGI
    app instead of a real socket, and points tools.get_settings() at the
    same test_settings the app itself is using — so a tool's own admin-key
    header and the app's expected admin key are the exact same value.
    """
    monkeypatch.setattr(mcp_tools, "get_settings", lambda: test_settings)
    real_client = httpx.AsyncClient

    def local_client(*args, **kwargs):
        kwargs["transport"] = httpx.ASGITransport(app=app_with_test_db)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", local_client)

    fake = _FakeMCP()
    mcp_tools.register(fake)
    return fake.tools


class TestAdminKeyNeverAToolParameter:
    def test_no_tool_accepts_an_admin_key_or_credential_parameter(self, registered_tools):
        """The bridge's whole point: an MCP caller never sees or supplies the admin key."""
        forbidden = {"admin_key", "x_admin_key", "api_key", "api_key_admin", "credential", "token"}
        for name, fn in registered_tools.items():
            params = set(inspect.signature(fn).parameters)
            assert not (params & forbidden), f"{name} accepts a credential-shaped parameter: {params & forbidden}"


class TestHealthTool:
    @pytest.mark.asyncio
    async def test_health_reports_a_real_status(self, registered_tools):
        result = await registered_tools["health"]()
        assert result["status"] in {"healthy", "degraded"}
        assert result["database"] == "ok"


class TestPreviewAndExecuteLifecycle:
    @pytest.mark.asyncio
    async def test_preview_then_execute_then_status_via_the_bridge(self, registered_tools):
        decision_id = f"mcp-bridge-{uuid4()}"
        preview = await registered_tools["preview_order"](
            decision_id=decision_id, account="primary", symbol="SPY",
            asset_type="EQUITY", instruction="BUY", quantity=1, order_type="MARKET",
        )
        assert preview["risk_verdict"] == "APPROVED"
        assert preview["simulated"] is True

        receipt = await registered_tools["execute_order"](
            decision_id=decision_id,
            preview_id=preview["preview_id"],
            approved_by="mcp-test-op",
            approved_at=datetime.now(UTC).isoformat(),
            attestation="mcp bridge test",
            idempotency_key=f"{decision_id}-idem",
        )
        assert receipt["status"] == "FILLED"

        status = await registered_tools["get_order_status"](decision_id=decision_id)
        assert status["decision_id"] == decision_id
        assert status["status"] == "FILLED"

    @pytest.mark.asyncio
    async def test_execute_order_refuses_without_a_prior_preview(self, registered_tools):
        """The bridge cannot manufacture approval or bypass the engine's own preview-must-exist gate."""
        decision_id = f"mcp-bridge-no-preview-{uuid4()}"
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await registered_tools["execute_order"](
                decision_id=decision_id,
                preview_id="preview-does-not-exist",
                approved_by="mcp-test-op",
                approved_at=datetime.now(UTC).isoformat(),
                attestation="should be refused",
                idempotency_key=f"{decision_id}-idem",
            )
        assert exc_info.value.response.status_code == 400

    @pytest.mark.asyncio
    async def test_execute_order_refuses_a_preview_id_that_does_not_match(self, registered_tools):
        """Same HITL binding the plain HTTP route enforces — the bridge adds no separate bypass."""
        decision_id = f"mcp-bridge-mismatch-{uuid4()}"
        preview = await registered_tools["preview_order"](
            decision_id=decision_id, account="primary", symbol="SPY",
            asset_type="EQUITY", instruction="BUY", quantity=1, order_type="MARKET",
        )
        assert preview["risk_verdict"] == "APPROVED"

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await registered_tools["execute_order"](
                decision_id=decision_id,
                preview_id="a-completely-different-preview-id",
                approved_by="mcp-test-op",
                approved_at=datetime.now(UTC).isoformat(),
                attestation="mismatched preview id",
                idempotency_key=f"{decision_id}-idem",
            )
        assert exc_info.value.response.status_code in {400, 422}


class TestReadAndReconciliationTools:
    @pytest.mark.asyncio
    async def test_get_positions_reaches_the_real_route(self, registered_tools):
        positions = await registered_tools["get_positions"](account="primary")
        assert positions["account"] == "primary"
        assert isinstance(positions["positions"], list)

    @pytest.mark.asyncio
    async def test_reconcile_reaches_the_real_route(self, registered_tools):
        report = await registered_tools["reconcile"](account="primary")
        assert report["account"] == "primary"
        assert "matched" in report
