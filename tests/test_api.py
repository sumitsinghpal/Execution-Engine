"""Tests for API endpoints."""

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from src.api.server import app, get_db
from src.models.orders import (
    ApprovalArtifact,
    AssetType,
    ExecutionRequest,
    Instruction,
    OrderType,
    TradeProposal,
)


@pytest.fixture
def client(app_with_test_db):
    """Create FastAPI test client with test database."""
    return TestClient(app_with_test_db)


class TestPreviewEndpoint:
    """Test POST /v1/orders/preview."""
    
    def test_preview_valid_order(self, client, sample_trade_proposal):
        """Preview accepts valid order."""
        response = client.post(
            "/v1/orders/preview",
            json=sample_trade_proposal.model_dump(mode="json"),
        )
        
        assert response.status_code == 200
        data = response.json()
        assert "preview_id" in data
        assert data["decision_id"] == "edge-20260821-001"
        assert "payload_checksum" in data
        assert "expires_at" in data
    
    def test_preview_rejects_invalid_schema(self, client):
        """Preview rejects invalid schema."""
        response = client.post(
            "/v1/orders/preview",
            json={
                "decision_id": "edge-test",
                "account": "primary",
                "symbol": "QQQ",
                # Missing required fields
            },
        )
        
        assert response.status_code == 422  # Validation error
    
    def test_preview_rejects_unknown_fields(self, client):
        """Preview rejects unknown fields."""
        response = client.post(
            "/v1/orders/preview",
            json={
                "decision_id": "edge-test",
                "account": "primary",
                "symbol": "QQQ",
                "asset_type": "ETF",
                "instruction": "BUY",
                "quantity": 10,
                "order_type": "MARKET",
                "unknown_field": "should fail",
            },
        )
        
        assert response.status_code == 422


class TestExecuteEndpoint:
    """
    Test POST /v1/orders/execute — the endpoint that actually submits to the
    broker. Previously untested: execute_order() checked for a pre-existing
    approval record before ever recording one, so every execute call failed
    with "No valid approval found" regardless of what was sent. These tests
    exist to keep that path exercised going forward.
    """

    def _preview(self, client, proposal):
        resp = client.post("/v1/orders/preview", json=proposal.model_dump(mode="json"))
        assert resp.status_code == 200
        return resp.json()["preview_id"]

    @staticmethod
    def _unique_proposal(sample_trade_proposal, suffix):
        # The idempotency dedup below is intentionally keyed by
        # (decision_id, operation) alone, not by idempotency_key — that's
        # correct production behavior (it should not be possible to
        # re-execute the same decision twice just by minting a fresh
        # idempotency_key). Test isolation therefore has to come from giving
        # each test its own decision_id, not from varying idempotency_key.
        return sample_trade_proposal.model_copy(
            update={"decision_id": f"{sample_trade_proposal.decision_id}-{suffix}"}
        )

    def test_execute_valid_order_succeeds(self, client, sample_trade_proposal):
        """A previewed order can be executed with a complete, fresh approval."""
        proposal = self._unique_proposal(sample_trade_proposal, "exec-ok")
        preview_id = self._preview(client, proposal)

        response = client.post(
            "/v1/orders/execute",
            json={
                "decision_id": proposal.decision_id,
                "preview_id": preview_id,
                "approval": {
                    "preview_id": preview_id,
                    "approved_by": "test_operator",
                    "approved_at": datetime.utcnow().isoformat(),
                    "attestation": "Approved for integration test",
                    "idempotency_key": f"{proposal.decision_id}:exec:1",
                },
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["decision_id"] == proposal.decision_id
        assert data["status"] == "SUBMITTED"
        assert "execution_id" in data

    def test_execute_rejects_empty_attestation(self, client, sample_trade_proposal):
        """An approval with an empty attestation is rejected, not silently accepted."""
        proposal = self._unique_proposal(sample_trade_proposal, "empty-attest")
        preview_id = self._preview(client, proposal)

        response = client.post(
            "/v1/orders/execute",
            json={
                "decision_id": proposal.decision_id,
                "preview_id": preview_id,
                "approval": {
                    "preview_id": preview_id,
                    "approved_by": "test_operator",
                    "approved_at": datetime.utcnow().isoformat(),
                    "attestation": "",
                    "idempotency_key": f"{proposal.decision_id}:exec:2",
                },
            },
        )

        assert response.status_code == 400

    def test_execute_rejects_stale_approval(self, client, sample_trade_proposal):
        """An approval older than the configured max age is rejected."""
        proposal = self._unique_proposal(sample_trade_proposal, "stale")
        preview_id = self._preview(client, proposal)
        stale_time = datetime.utcnow() - timedelta(hours=2)

        response = client.post(
            "/v1/orders/execute",
            json={
                "decision_id": proposal.decision_id,
                "preview_id": preview_id,
                "approval": {
                    "preview_id": preview_id,
                    "approved_by": "test_operator",
                    "approved_at": stale_time.isoformat(),
                    "attestation": "Approved but stale",
                    "idempotency_key": f"{proposal.decision_id}:exec:3",
                },
            },
        )

        assert response.status_code == 400

    def test_duplicate_execute_returns_cached_response(self, client, sample_trade_proposal):
        """
        Executing the same decision twice returns the original result rather
        than resubmitting to the broker a second time.
        """
        proposal = self._unique_proposal(sample_trade_proposal, "dup")
        preview_id = self._preview(client, proposal)
        payload = {
            "decision_id": proposal.decision_id,
            "preview_id": preview_id,
            "approval": {
                "preview_id": preview_id,
                "approved_by": "test_operator",
                "approved_at": datetime.utcnow().isoformat(),
                "attestation": "Approved for integration test",
                "idempotency_key": f"{proposal.decision_id}:exec:4",
            },
        }

        first = client.post("/v1/orders/execute", json=payload)
        assert first.status_code == 200

        second = client.post("/v1/orders/execute", json=payload)
        assert second.status_code == 200
        assert second.json()["execution_id"] == first.json()["execution_id"]


class TestKillSwitchActuallyBlocksOrders:
    """
    The kill switch admin endpoints and the actual order-blocking check used
    to be completely disconnected: POST /v1/kill-switch/on updated an
    in-process module global that only /v1/kill-switch/status ever read,
    while Executor._get_kill_switch_state() read a totally different, static
    value (settings.kill_switch_enabled from .env) that the endpoint never
    touched. Enabling the kill switch through the API did not, in fact,
    prevent a single new order from being previewed. These tests exercise
    the real end-to-end path — through the actual HTTP endpoints, not a
    directly-constructed RiskChecker.evaluate(kill_switch_on=True) call —
    since that's exactly the seam where the wiring was broken.
    """

    def test_preview_rejected_while_kill_switch_on(self, client, sample_trade_proposal):
        # A unique decision_id, not the fixture's plain one — other tests in
        # this file preview that same decision_id, and the (correctly
        # working) preview idempotency cache would otherwise hand back
        # their cached, pre-kill-switch APPROVED result here instead of
        # actually exercising this test's own request.
        proposal = sample_trade_proposal.model_copy(
            update={"decision_id": f"{sample_trade_proposal.decision_id}-ks-on"}
        )
        on_resp = client.post("/v1/kill-switch/on", headers={"x-admin-key": "change-me-in-prod"})
        assert on_resp.status_code == 200
        assert on_resp.json()["enabled"] is True

        try:
            preview_resp = client.post("/v1/orders/preview", json=proposal.model_dump(mode="json"))
            assert preview_resp.status_code == 200
            data = preview_resp.json()
            assert data["risk_verdict"] == "REJECTED", (
                "Order was still APPROVED while the kill switch was ON via the real API"
            )
            assert data["risk_details"]["checks"]["kill_switch_off"] is False
        finally:
            client.post("/v1/kill-switch/off", headers={"x-admin-key": "change-me-in-prod"})

    def test_preview_allowed_again_after_kill_switch_off(self, client, sample_trade_proposal):
        proposal = sample_trade_proposal.model_copy(
            update={"decision_id": f"{sample_trade_proposal.decision_id}-ks-off"}
        )
        client.post("/v1/kill-switch/on", headers={"x-admin-key": "change-me-in-prod"})
        client.post("/v1/kill-switch/off", headers={"x-admin-key": "change-me-in-prod"})

        preview_resp = client.post("/v1/orders/preview", json=proposal.model_dump(mode="json"))
        assert preview_resp.status_code == 200
        assert preview_resp.json()["risk_verdict"] == "APPROVED"


class TestMultiAgentKillSwitch:
    """
    A deployment running multiple coordinating agents needs to be able to
    halt one misbehaving agent without an all-stop, while the fleet-wide
    switch still overrides every agent regardless of its own state. These
    tests exercise the real HTTP endpoints end-to-end, the same way
    TestKillSwitchActuallyBlocksOrders does for the fleet-wide switch.
    """

    def test_halting_one_agent_does_not_block_another(self, client, sample_trade_proposal):
        halted_agent_proposal = sample_trade_proposal.model_copy(
            update={"decision_id": f"{sample_trade_proposal.decision_id}-agent-halted", "agent_id": "momentum-agent"}
        )
        other_agent_proposal = sample_trade_proposal.model_copy(
            update={"decision_id": f"{sample_trade_proposal.decision_id}-agent-other", "agent_id": "meanrev-agent"}
        )

        on_resp = client.post(
            "/v1/kill-switch/agents/momentum-agent/on", headers={"x-admin-key": "change-me-in-prod"}
        )
        assert on_resp.status_code == 200
        assert on_resp.json()["enabled"] is True

        try:
            halted_resp = client.post("/v1/orders/preview", json=halted_agent_proposal.model_dump(mode="json"))
            assert halted_resp.json()["risk_verdict"] == "REJECTED"

            other_resp = client.post("/v1/orders/preview", json=other_agent_proposal.model_dump(mode="json"))
            assert other_resp.json()["risk_verdict"] == "APPROVED", (
                "halting one agent must not block a different agent's orders"
            )
        finally:
            client.post("/v1/kill-switch/agents/momentum-agent/off", headers={"x-admin-key": "change-me-in-prod"})

    def test_agent_kill_switch_requires_admin_key(self, client):
        resp = client.post("/v1/kill-switch/agents/some-agent/on")
        assert resp.status_code == 403

    def test_agent_kill_switch_rejects_reserved_global_scope(self, client):
        resp = client.post("/v1/kill-switch/agents/__global__/on", headers={"x-admin-key": "change-me-in-prod"})
        assert resp.status_code == 400

    def test_agent_kill_switch_status_reflects_toggled_state(self, client):
        client.post("/v1/kill-switch/agents/status-probe-agent/on", headers={"x-admin-key": "change-me-in-prod"})
        try:
            status_resp = client.get("/v1/kill-switch/agents/status-probe-agent/status")
            assert status_resp.status_code == 200
            assert status_resp.json()["enabled"] is True
        finally:
            client.post("/v1/kill-switch/agents/status-probe-agent/off", headers={"x-admin-key": "change-me-in-prod"})

    def test_agents_status_reports_combined_global_and_own_state(self, client, sample_trade_proposal):
        proposal = sample_trade_proposal.model_copy(
            update={"decision_id": f"{sample_trade_proposal.decision_id}-coord-view", "agent_id": "coordination-probe"}
        )
        client.post("/v1/orders/preview", json=proposal.model_dump(mode="json"))
        client.post(
            "/v1/kill-switch/agents/coordination-probe/on", headers={"x-admin-key": "change-me-in-prod"}
        )

        try:
            resp = client.get("/v1/agents/status")
            assert resp.status_code == 200
            data = resp.json()
            assert data["global_kill_switch"]["enabled"] is False

            agent_entry = next(a for a in data["agents"] if a["agent_id"] == "coordination-probe")
            assert agent_entry["halted"] is True
            assert agent_entry["halted_by_own_switch"] is True
            assert agent_entry["halted_by_global_switch"] is False
        finally:
            client.post(
                "/v1/kill-switch/agents/coordination-probe/off", headers={"x-admin-key": "change-me-in-prod"}
            )


class TestAgentExposureCheckEndpoint:
    """Test POST /v1/risk/agent-exposure-check."""

    def test_reports_zero_exposure_for_an_agent_with_no_orders(self, client):
        resp = client.post("/v1/risk/agent-exposure-check", params={"agent_id": "exposure-endpoint-fresh-agent"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["agent_id"] == "exposure-endpoint-fresh-agent"
        assert data["committed_notional_usd"] == "0"
        assert data["cap_usd"] is None
        assert data["breached"] is False

    def test_rejects_reserved_global_scope(self, client):
        resp = client.post("/v1/risk/agent-exposure-check", params={"agent_id": "__global__"})
        assert resp.status_code == 400

    def test_executed_order_counts_toward_exposure(self, client, sample_trade_proposal):
        """A full preview -> approve -> execute flow should show up in this agent's committed notional."""
        proposal = sample_trade_proposal.model_copy(
            update={"decision_id": f"{sample_trade_proposal.decision_id}-exposure-flow", "agent_id": "exposure-flow-agent"}
        )
        preview_resp = client.post("/v1/orders/preview", json=proposal.model_dump(mode="json"))
        assert preview_resp.status_code == 200
        preview = preview_resp.json()
        assert preview["risk_verdict"] == "APPROVED"

        execute_resp = client.post(
            "/v1/orders/execute",
            json={
                "decision_id": proposal.decision_id,
                "preview_id": preview["preview_id"],
                "approval": {
                    "preview_id": preview["preview_id"],
                    "approved_by": "test-operator",
                    "approved_at": datetime.utcnow().isoformat(),
                    "attestation": "approved for test",
                    "idempotency_key": f"idem-{proposal.decision_id}",
                },
            },
        )
        assert execute_resp.status_code == 200

        exposure_resp = client.post("/v1/risk/agent-exposure-check", params={"agent_id": "exposure-flow-agent"})
        assert exposure_resp.status_code == 200
        assert float(exposure_resp.json()["committed_notional_usd"]) == pytest.approx(float(preview["estimated_cost"]))


class TestSymbolExposureEndpoint:
    """Test GET /v1/risk/symbol-exposure."""

    def test_reports_zero_for_a_never_traded_symbol(self, client):
        resp = client.get("/v1/risk/symbol-exposure", params={"account": "primary", "symbol": "EEM"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["account"] == "primary"
        assert data["symbol"] == "EEM"
        assert data["committed_notional_usd"] == "0"
        assert data["cap_usd"] is None
        assert data["breached"] is False


class TestHealthEndpoint:
    """Test GET /v1/health."""
    
    def test_health_check(self, client):
        """Health check returns status."""
        response = client.get("/v1/health")
        
        assert response.status_code == 200
        data = response.json()
        assert "status" in data
        assert "timestamp" in data
        assert "database" in data
        assert "broker_connectivity" in data


class TestKillSwitchEndpoints:
    """Test kill switch endpoints."""
    
    def test_kill_switch_on_requires_admin_key(self, client):
        """kill-switch/on requires admin key."""
        response = client.post("/v1/kill-switch/on")
        
        assert response.status_code == 403
    
    def test_kill_switch_on_with_valid_key(self, client):
        """kill-switch/on works with valid key."""
        response = client.post(
            "/v1/kill-switch/on",
            headers={"x-admin-key": "change-me-in-prod"},
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["enabled"] is True
    
    def test_kill_switch_off_with_valid_key(self, client):
        """kill-switch/off works with valid key."""
        # First turn it on
        client.post(
            "/v1/kill-switch/on",
            headers={"x-admin-key": "change-me-in-prod"},
        )
        
        # Then turn it off
        response = client.post(
            "/v1/kill-switch/off",
            headers={"x-admin-key": "change-me-in-prod"},
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["enabled"] is False
    
    def test_get_kill_switch_status(self, client):
        """Get current kill switch state."""
        response = client.get("/v1/kill-switch/status")
        
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data["enabled"], bool)


class TestMarketStatusEndpoint:
    """Test GET /v1/market-status."""

    def test_market_status(self, client):
        """Market status returns info."""
        response = client.get("/v1/market-status")

        assert response.status_code == 200
        data = response.json()
        assert "in_market_hours" in data
        assert "current_time_et" in data
        assert "market_open" in data
        assert "market_close" in data


class TestDrawdownCheckEndpoint:
    """Test POST /v1/risk/drawdown-check."""

    def test_drawdown_check_reports_no_breach_for_flat_paper_account(self, client):
        """
        The default paper broker returns the same fixed equity every call,
        so a first-of-the-day check against it must report zero drawdown
        and never touch the kill switch.
        """
        response = client.post("/v1/risk/drawdown-check", params={"account": "primary"})

        assert response.status_code == 200
        data = response.json()
        assert data["account"] == "primary"
        assert data["breached"] is False
        assert data["drawdown_pct"] == 0
        assert data["baseline_equity"] == data["current_equity"]

        status = client.get("/v1/kill-switch/status")
        assert status.json()["enabled"] is False
