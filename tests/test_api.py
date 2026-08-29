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
    """Create FastAPI test client with test database. Most endpoints now require the admin key (see verify_admin_key) — sent as a default header here rather than per-call."""
    return TestClient(app_with_test_db, headers={"x-admin-key": "change-me-in-prod"})


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


class TestMultiLegEndpoints:
    """
    POST /v1/orders/multi-leg/preview and /execute — a 2-leg options combo
    previewed and executed against the app's real broker (PaperBrokerAdapter,
    which prices OPTION symbols with real synthetic option pricing — see
    src/brokers/paper.py). Runs the full HTTP round trip specifically to
    catch a JSON-serialization-boundary bug the way
    tests/test_backtest_api.py caught the profit_factor=inf bug: a
    pure-Python-level test of src/execution/multi_leg.py wouldn't cross
    that boundary at all.
    """

    @staticmethod
    def _occ(underlying="QQQ", days_out=45, right="C", strike="400"):
        from src.models.occ_symbol import format_occ_symbol
        return format_occ_symbol(underlying, (datetime.utcnow() + timedelta(days=days_out)).date(), right, Decimal(strike))

    def _leg_payload(self, decision_id, symbol, instruction, quantity=1):
        return {
            "decision_id": decision_id, "agent_id": "default", "account": "primary",
            "symbol": symbol, "instruction": instruction, "quantity": quantity, "order_type": "MARKET",
        }

    def test_preview_a_vertical_spread_returns_combined_risk_figures(self, client):
        long_leg = self._occ(strike="400")
        short_leg = self._occ(strike="410")

        response = client.post(
            "/v1/orders/multi-leg/preview",
            json={
                "combo_type": "vertical_spread",
                "legs": [
                    self._leg_payload("mlapi-vd-1", long_leg, "BUY"),
                    self._leg_payload("mlapi-vd-2", short_leg, "SELL"),
                ],
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["combo_type"] == "vertical_spread"
        assert data["risk_verdict"] == "APPROVED"
        assert len(data["legs"]) == 2
        assert isinstance(data["net_debit_or_credit_usd"], (int, float))
        assert isinstance(data["max_loss_usd"], (int, float))
        assert isinstance(data["max_profit_usd"], (int, float))
        # Standard vertical spread identity: what you could lose plus what you could still gain covers the full $1000 strike width (100 x $10 x 1 contract).
        assert data["max_loss_usd"] + data["max_profit_usd"] == pytest.approx(1000.0)

    def test_preview_rejects_a_structurally_invalid_combo(self, client):
        same_symbol = self._occ(strike="400")

        response = client.post(
            "/v1/orders/multi-leg/preview",
            json={
                "combo_type": "vertical_spread",
                "legs": [
                    self._leg_payload("mlapi-bad-1", same_symbol, "BUY"),
                    self._leg_payload("mlapi-bad-2", same_symbol, "SELL"),
                ],
            },
        )

        assert response.status_code == 400

    def test_preview_then_execute_a_straddle_end_to_end(self, client):
        call_leg = self._occ(strike="400", right="C")
        put_leg = self._occ(strike="400", right="P")

        preview_response = client.post(
            "/v1/orders/multi-leg/preview",
            json={
                "combo_type": "straddle",
                "legs": [
                    self._leg_payload("mlapi-st-1", call_leg, "BUY"),
                    self._leg_payload("mlapi-st-2", put_leg, "BUY"),
                ],
            },
        )
        assert preview_response.status_code == 200
        preview = preview_response.json()
        assert preview["risk_verdict"] == "APPROVED"

        execute_response = client.post(
            "/v1/orders/multi-leg/execute",
            json={
                "combo_id": preview["combo_id"],
                "legs": [{"decision_id": leg["decision_id"], "preview_id": leg["preview_id"]} for leg in preview["legs"]],
                "approved_by": "test_operator",
                "attestation": "Approved straddle for integration test",
            },
        )

        assert execute_response.status_code == 200
        result = execute_response.json()
        assert result["fully_executed"] is True
        assert len(result["executed_legs"]) == 2
        assert result["failed_leg_index"] is None

    def test_execute_with_an_unknown_decision_id_returns_404(self, client):
        response = client.post(
            "/v1/orders/multi-leg/execute",
            json={
                "combo_id": "combo-does-not-exist",
                "legs": [
                    {"decision_id": "never-previewed-1", "preview_id": "preview-fake-1"},
                    {"decision_id": "never-previewed-2", "preview_id": "preview-fake-2"},
                ],
                "approved_by": "test_operator",
                "attestation": "should not reach execution",
            },
        )

        assert response.status_code == 404


class TestDailyPlanEndpoints:
    """
    POST /v1/autonomous/rank-strategies, /arm, /disarm, and GET
    /v1/autonomous/plan — the "which strategies trade my money today"
    workflow (see src/execution/daily_plan.py and
    src/execution/strategy_ranking.py). rank-strategies hits real
    yfinance data (network); the others are pure DB operations tested
    without mocking anything.
    """

    def test_arm_then_plan_shows_it_active(self, client):
        arm_response = client.post(
            "/v1/autonomous/arm",
            json={"strategy_ids": ["golden_cross"], "notional_per_trade_usd": "750", "armed_by": "sumit"},
        )
        assert arm_response.status_code == 200
        armed = arm_response.json()
        assert armed["strategy_ids"] == ["golden_cross"]
        assert armed["notional_per_trade_usd"] == "750"
        assert armed["active"] is True

        plan_response = client.get("/v1/autonomous/plan")
        assert plan_response.status_code == 200
        assert plan_response.json()["active_plan"]["strategy_ids"] == ["golden_cross"]

    def test_arm_rejects_an_unknown_strategy_id(self, client):
        response = client.post(
            "/v1/autonomous/arm",
            json={"strategy_ids": ["not-a-real-strategy"], "notional_per_trade_usd": "500", "armed_by": "sumit"},
        )
        assert response.status_code == 400
        assert "not-a-real-strategy" in response.json()["detail"]

    def test_arm_rejects_an_intraday_strategy(self, client):
        response = client.post(
            "/v1/autonomous/arm",
            json={"strategy_ids": ["orb"], "notional_per_trade_usd": "500", "armed_by": "sumit"},
        )
        assert response.status_code == 400
        assert "daily-bar" in response.json()["detail"]

    def test_arm_rejects_non_positive_notional(self, client):
        response = client.post(
            "/v1/autonomous/arm",
            json={"strategy_ids": ["golden_cross"], "notional_per_trade_usd": "0", "armed_by": "sumit"},
        )
        assert response.status_code == 422  # caught by the request model's gt=0 constraint

    def test_disarm_clears_the_plan(self, client):
        client.post(
            "/v1/autonomous/arm",
            json={"strategy_ids": ["golden_cross"], "notional_per_trade_usd": "500", "armed_by": "sumit"},
        )

        disarm_response = client.post("/v1/autonomous/disarm", json={"disarmed_by": "sumit"})
        assert disarm_response.status_code == 200
        assert disarm_response.json()["was_active"] is True

        plan_response = client.get("/v1/autonomous/plan")
        assert plan_response.json()["active_plan"] is None

    def test_plan_is_null_when_nothing_armed(self, client):
        client.post("/v1/autonomous/disarm", json={"disarmed_by": "test-setup"})  # ensure a clean slate

        response = client.get("/v1/autonomous/plan")

        assert response.status_code == 200
        assert response.json()["active_plan"] is None

    def test_autonomous_status_reflects_the_active_plan(self, client):
        client.post(
            "/v1/autonomous/arm",
            json={"strategy_ids": ["rsi2_connors"], "notional_per_trade_usd": "300", "armed_by": "sumit"},
        )

        response = client.get("/v1/autonomous/status")

        assert response.status_code == 200
        assert response.json()["active_plan"]["strategy_ids"] == ["rsi2_connors"]

    def test_arm_has_no_expiry_field(self, client):
        """The one-time "take this money and trade it" flow — no ttl_hours, no expires_at. See src/execution/daily_plan.py's module docstring."""
        response = client.post(
            "/v1/autonomous/arm",
            json={"strategy_ids": ["golden_cross"], "notional_per_trade_usd": "500", "armed_by": "sumit"},
        )
        assert response.status_code == 200
        assert "expires_at" not in response.json()
        assert "ttl_hours" not in response.json()

    def test_rotate_now_is_a_no_op_when_nothing_armed(self, client):
        client.post("/v1/autonomous/disarm", json={"disarmed_by": "test-setup"})

        response = client.post("/v1/autonomous/rotate-now")

        assert response.status_code == 200
        assert response.json() == {"rotated": False, "plan": None}

    def test_start_rejects_when_ranking_finds_nothing(self, client, monkeypatch):
        import src.api.server as server

        async def fake_empty_ranking(symbols, **kwargs):
            from src.execution.strategy_ranking import StrategyRanking
            return StrategyRanking(lookback_days=90, symbols=symbols, computed_for_date="2026-01-01", rankings=[], top_picks=[], errors=[])

        monkeypatch.setattr(server, "rank_strategies_by_recent_performance", fake_empty_ranking)

        response = client.post("/v1/autonomous/start", json={"notional_per_trade_usd": "1000", "started_by": "sumit"})

        assert response.status_code == 422
        plan_response = client.get("/v1/autonomous/plan")
        assert plan_response.json()["active_plan"] is None  # nothing got armed

    def test_start_arms_the_top_picks_from_the_ranking(self, client, monkeypatch):
        import src.api.server as server

        async def fake_ranking(symbols, **kwargs):
            from src.execution.strategy_ranking import StrategyRanking
            return StrategyRanking(
                lookback_days=90, symbols=symbols, computed_for_date="2026-01-01",
                rankings=[], top_picks=["turtle_donchian", "macd_crossover"], errors=[],
            )

        monkeypatch.setattr(server, "rank_strategies_by_recent_performance", fake_ranking)

        response = client.post("/v1/autonomous/start", json={"notional_per_trade_usd": "1000", "started_by": "sumit"})

        assert response.status_code == 200
        body = response.json()
        assert body["plan"]["strategy_ids"] == ["turtle_donchian", "macd_crossover"]
        assert body["plan"]["notional_per_trade_usd"] == "1000"
        assert body["plan"]["armed_by"] == "sumit"
        assert "ranking" in body


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
        # The `client` fixture sends a valid key by default — override it here to test the rejection path.
        resp = client.post("/v1/kill-switch/agents/some-agent/on", headers={"x-admin-key": ""})
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

    def test_health_check_broker_connectivity_is_a_real_check_not_a_stub(self, client):
        """
        broker_connectivity used to be a hardcoded "untested" label
        regardless of configuration. In the test settings' default PAPER
        mode it must now report a real "ok" — PaperBrokerAdapter.list_accounts()
        was actually called and returned successfully.
        """
        response = client.get("/v1/health")

        assert response.json()["broker_connectivity"] == "ok"


class TestKillSwitchEndpoints:
    """Test kill switch endpoints."""
    
    def test_kill_switch_on_requires_admin_key(self, client):
        """kill-switch/on requires admin key."""
        # The `client` fixture sends a valid key by default — override it here to test the rejection path.
        response = client.post("/v1/kill-switch/on", headers={"x-admin-key": ""})

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


class TestReadOnlyAccountEndpoints:
    """Test GET /v1/account/{account}/balances, /positions, and GET /v1/orders — all side-effect-free."""

    def test_balances_returns_paper_broker_values(self, client):
        resp = client.get("/v1/account/primary/balances")
        assert resp.status_code == 200
        data = resp.json()
        assert data["account"] == "primary"
        assert data["balances"]["net_liquidation_value"] > 0

    def test_positions_returns_a_list(self, client):
        resp = client.get("/v1/account/primary/positions")
        assert resp.status_code == 200
        assert resp.json()["account"] == "primary"
        assert isinstance(resp.json()["positions"], list)

    def test_unknown_account_alias_is_404_not_500(self, client):
        resp = client.get("/v1/account/not-a-real-alias/balances")
        assert resp.status_code == 404

    def test_list_orders_reflects_a_just_placed_preview(self, client, sample_trade_proposal):
        proposal = sample_trade_proposal.model_copy(
            update={"decision_id": f"{sample_trade_proposal.decision_id}-list-orders"}
        )
        client.post("/v1/orders/preview", json=proposal.model_dump(mode="json"))

        resp = client.get("/v1/orders", params={"account": proposal.account, "limit": 5})

        assert resp.status_code == 200
        decision_ids = [o["decision_id"] for o in resp.json()["orders"]]
        assert proposal.decision_id in decision_ids

    def test_list_orders_filters_by_agent_id(self, client, sample_trade_proposal):
        proposal = sample_trade_proposal.model_copy(
            update={"decision_id": f"{sample_trade_proposal.decision_id}-agent-filter", "agent_id": "list-orders-probe-agent"}
        )
        client.post("/v1/orders/preview", json=proposal.model_dump(mode="json"))

        resp = client.get("/v1/orders", params={"agent_id": "list-orders-probe-agent"})

        orders = resp.json()["orders"]
        assert len(orders) >= 1
        assert all(o["agent_id"] == "list-orders-probe-agent" for o in orders)


class TestStrategyEndpoints:
    """
    Test the strategy catalog, on-demand scan, and signal review endpoints.
    None of these preview or execute an order — see
    TestStrategySignalIntoOrderTicket below for how a signal actually
    becomes a (still human-approved) order.
    """

    def test_list_strategies_returns_the_full_catalog_grouped_by_category(self, client):
        resp = client.get("/v1/strategies")
        assert resp.status_code == 200
        strategies = resp.json()["strategies"]
        assert len(strategies) == 8

        categories = {s["category"] for s in strategies}
        assert categories == {"INTRADAY", "MULTI_DAY", "OTHER"}
        assert all({"id", "name", "risk_reward_label", "stop_rule", "target_rule"} <= s.keys() for s in strategies)

    def test_scan_unknown_strategy_is_404(self, client):
        resp = client.post("/v1/strategies/not-a-real-strategy/scan", params={"symbol": "QQQ"})
        assert resp.status_code == 404

    def test_scan_on_demand_never_places_or_previews_an_order(self, client):
        """Whether or not a signal fires, this must never create anything in the order history."""
        before = client.get("/v1/orders").json()["orders"]
        client.post("/v1/strategies/orb/scan", params={"symbol": "QQQ"})
        after = client.get("/v1/orders").json()["orders"]

        assert len(after) == len(before)

    def test_scan_all_returns_watchlist_and_a_count(self, client):
        resp = client.post("/v1/strategies/scan-all")
        assert resp.status_code == 200
        data = resp.json()
        assert "new_signals" in data
        assert isinstance(data["watchlist"], list)

    def test_signals_endpoint_defaults_to_pending(self, client):
        resp = client.get("/v1/strategies/signals")
        assert resp.status_code == 200
        assert all(s["status"] == "PENDING" for s in resp.json()["signals"])

    def test_dismiss_unknown_signal_is_404(self, client):
        resp = client.post("/v1/strategies/signals/999999999/dismiss")
        assert resp.status_code == 404

    def test_dismiss_marks_a_signal_dismissed(self, client):
        client.post("/v1/strategies/scan-all")
        pending = client.get("/v1/strategies/signals").json()["signals"]
        if not pending:
            pytest.skip("no signal fired in this run to dismiss (synthetic data is randomized)")

        signal_id = pending[0]["id"]
        resp = client.post(f"/v1/strategies/signals/{signal_id}/dismiss")

        assert resp.status_code == 200
        assert resp.json()["status"] == "DISMISSED"


class TestStrategyMetadataOnTradeProposal:
    """A signal-sourced order carries its strategy plan through preview, purely as audit metadata."""

    def test_preview_persists_and_returns_strategy_metadata(self, client, sample_trade_proposal):
        proposal_dict = sample_trade_proposal.model_dump(mode="json")
        proposal_dict["decision_id"] = f"{sample_trade_proposal.decision_id}-strategy-meta"
        proposal_dict["strategy_id"] = "golden_cross"
        proposal_dict["strategy_stop_loss_price"] = "260.00"
        proposal_dict["strategy_take_profit_price"] = "290.00"

        resp = client.post("/v1/orders/preview", json=proposal_dict)
        assert resp.status_code == 200

        orders = client.get("/v1/orders", params={"limit": 5}).json()["orders"]
        match = next(o for o in orders if o["decision_id"] == proposal_dict["decision_id"])
        assert match["strategy_id"] == "golden_cross"
        assert match["strategy_stop_loss_price"] == "260.00"
        assert match["strategy_take_profit_price"] == "290.00"

    def test_strategy_metadata_is_optional(self, client, sample_trade_proposal):
        """A plain, non-strategy order (every other test in this file) must keep working unchanged."""
        proposal_dict = sample_trade_proposal.model_dump(mode="json")
        proposal_dict["decision_id"] = f"{sample_trade_proposal.decision_id}-no-strategy"

        resp = client.post("/v1/orders/preview", json=proposal_dict)

        assert resp.status_code == 200


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
