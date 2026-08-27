"""
End-to-end tests for the EDGE-TF claim/report hook inside POST
/v1/orders/execute (src/api/server.py) — the part src/test_edge_tf_connector.py
can't reach, since that file exercises the connector module directly rather
than through the actual route.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from src.execution.external_signals import ExternalSignalService, ExternalSignalStatus
from src.integrations.edge_tf_client import EdgeTFGatewayError
from src.models.orders import AssetType, Instruction, OrderType, TradeProposal


@pytest.fixture
def client(app_with_test_db):
    return TestClient(app_with_test_db)


class _FakeEdgeTFClient:
    def __init__(self, claim_error=None):
        self._claim_error = claim_error
        self.claimed = []
        self.reported = []

    async def claim(self, trade_id, *, executor_id):
        if self._claim_error is not None:
            raise self._claim_error
        self.claimed.append((trade_id, executor_id))
        return {"trade_id": trade_id}

    async def report(self, trade_id, report):
        self.reported.append((trade_id, report))
        return {"recorded": True}


def _seed_external_signal(session, trade_id: str, quote_price: str):
    service = ExternalSignalService(session)
    return service.record_if_new(
        "edge-tf",
        {
            "instruction_id": f"instr-{trade_id}",
            "trade_id": trade_id,
            "symbol": "QQQ",
            "side": "BUY",
            "quantity": 10.0,
            "order_type": "LIMIT",
            "limit_price": float(quote_price),
            "estimated_notional": float(quote_price) * 10,
            "currency": "USD",
            "thesis_id": "thesis-int-1",
            "strategy_module": "quant_engine.iav_calculator",
            "rationale": "integration test",
            "intent_hash": "hash-int",
            "approved_fingerprint": "fp-int",
            "approval_expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "idempotency_key": f"idem-{trade_id}",
        },
    )


class TestExecuteClaimsAndReportsToEdgeTF:
    def test_successful_execute_claims_then_reports_and_marks_executed(
        self, client, app_with_test_db, test_db_engine_and_session, sample_trade_proposal, monkeypatch
    ):
        _, session = test_db_engine_and_session
        trade_id = "EDGE-INT-OK"
        _seed_external_signal(session, trade_id, str(sample_trade_proposal.limit_price))

        fake = _FakeEdgeTFClient()
        monkeypatch.setattr("src.execution.edge_tf_connector._client", lambda s: fake)

        proposal = sample_trade_proposal.model_copy(update={"decision_id": trade_id})
        preview_resp = client.post("/v1/orders/preview", json=proposal.model_dump(mode="json"))
        assert preview_resp.status_code == 200
        preview_id = preview_resp.json()["preview_id"]

        exec_resp = client.post(
            "/v1/orders/execute",
            json={
                "decision_id": trade_id,
                "preview_id": preview_id,
                "approval": {
                    "preview_id": preview_id,
                    "approved_by": "test_operator",
                    "approved_at": datetime.utcnow().isoformat(),
                    "attestation": "Approved for integration test",
                    "idempotency_key": f"{trade_id}:exec:1",
                },
            },
        )

        assert exec_resp.status_code == 200
        assert fake.claimed == [(trade_id, "execution-engine")]
        assert len(fake.reported) == 1
        assert fake.reported[0][0] == trade_id

        record = ExternalSignalService(session).get_by_trade_id(trade_id)
        assert record.status == ExternalSignalStatus.EXECUTED

    def test_upstream_claim_refusal_blocks_local_execution(
        self, client, app_with_test_db, test_db_engine_and_session, sample_trade_proposal, monkeypatch
    ):
        """
        If EDGE-TF refuses the claim (already claimed elsewhere, expired,
        mutated), this system must NOT proceed to submit the order to the
        broker — otherwise the same upstream trade could execute twice.
        """
        _, session = test_db_engine_and_session
        trade_id = "EDGE-INT-CONFLICT"
        _seed_external_signal(session, trade_id, str(sample_trade_proposal.limit_price))

        fake = _FakeEdgeTFClient(claim_error=EdgeTFGatewayError(409, "CLAIM_CONFLICT", "already claimed"))
        monkeypatch.setattr("src.execution.edge_tf_connector._client", lambda s: fake)

        proposal = sample_trade_proposal.model_copy(update={"decision_id": trade_id})
        preview_resp = client.post("/v1/orders/preview", json=proposal.model_dump(mode="json"))
        assert preview_resp.status_code == 200
        preview_id = preview_resp.json()["preview_id"]

        exec_resp = client.post(
            "/v1/orders/execute",
            json={
                "decision_id": trade_id,
                "preview_id": preview_id,
                "approval": {
                    "preview_id": preview_id,
                    "approved_by": "test_operator",
                    "approved_at": datetime.utcnow().isoformat(),
                    "attestation": "Approved for integration test",
                    "idempotency_key": f"{trade_id}:exec:1",
                },
            },
        )

        assert exec_resp.status_code == 409
        assert fake.claimed == []
        assert fake.reported == []

        # The preview itself created the OrderRecord (status PREVIEWED); it
        # never advanced past that, confirming Executor.execute_order() was
        # never reached.
        status_resp = client.get(f"/v1/orders/{trade_id}")
        assert status_resp.status_code == 200
        assert status_resp.json()["status"] == "PREVIEWED"

    def test_execute_for_a_non_edge_tf_decision_id_never_touches_the_gateway(
        self, client, sample_trade_proposal, monkeypatch
    ):
        """A normal, non-EDGE-TF-sourced order must execute exactly as before — no claim, no report."""

        def _unexpected_client(settings):
            raise AssertionError("edge_tf_connector._client() should not be called for a non-external decision_id")

        monkeypatch.setattr("src.execution.edge_tf_connector._client", _unexpected_client)

        proposal = sample_trade_proposal.model_copy(update={"decision_id": "regular-manual-order-1"})
        preview_resp = client.post("/v1/orders/preview", json=proposal.model_dump(mode="json"))
        preview_id = preview_resp.json()["preview_id"]

        exec_resp = client.post(
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

        assert exec_resp.status_code == 200
