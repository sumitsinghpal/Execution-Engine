from __future__ import annotations

from datetime import UTC, datetime


def _proposal(decision_id: str):
    return {
        "decision_id": decision_id,
        "account": "primary",
        "symbol": "QQQ",
        "asset_type": "ETF",
        "instruction": "BUY",
        "quantity": 10,
        "order_type": "LIMIT",
        "limit_price": "721.50",
    }


def test_preview_execute_and_get_order(client):
    preview_response = client.post("/v1/orders/preview", json=_proposal("edge-20991231-001"))
    assert preview_response.status_code == 200
    preview_body = preview_response.json()

    execute_response = client.post(
        "/v1/orders/execute",
        json={
            "decision_id": "edge-20991231-001",
            "preview_id": preview_body["preview_id"],
            "idempotency_key": "idem-12345678",
            "approval": {
                "approved_by": "human.approver",
                "approved_at": datetime.now(UTC).isoformat(),
                "attestation": "I approve execution of this trade",
                "signature": "signed",
            },
        },
    )
    assert execute_response.status_code == 200

    order_response = client.get("/v1/orders/edge-20991231-001")
    assert order_response.status_code == 200
    assert order_response.json()["status"] == "SUBMITTED"


def test_kill_switch_enforced(client):
    on = client.post("/v1/kill-switch/on", headers={"x-api-key": "test-admin-key"})
    assert on.status_code == 200

    preview_response = client.post("/v1/orders/preview", json=_proposal("edge-20991231-002"))
    assert preview_response.status_code == 422
    assert preview_response.json()["error"]["code"] == "RISK_REJECTED"


def test_execute_requires_approval_language(client):
    preview_response = client.post("/v1/orders/preview", json=_proposal("edge-20991231-003"))
    preview_body = preview_response.json()

    execute_response = client.post(
        "/v1/orders/execute",
        json={
            "decision_id": "edge-20991231-003",
            "preview_id": preview_body["preview_id"],
            "idempotency_key": "idem-abcdef12",
            "approval": {
                "approved_by": "human.approver",
                "approved_at": datetime.now(UTC).isoformat(),
                "attestation": "looks good",
                "signature": "signed",
            },
        },
    )
    assert execute_response.status_code == 422
    assert execute_response.json()["error"]["code"] == "INVALID_APPROVAL"


def test_idempotency_collision(client):
    preview_response = client.post("/v1/orders/preview", json=_proposal("edge-20991231-004"))
    preview_body = preview_response.json()

    request = {
        "decision_id": "edge-20991231-004",
        "preview_id": preview_body["preview_id"],
        "idempotency_key": "idem-11111111",
        "approval": {
            "approved_by": "human.approver",
            "approved_at": datetime.now(UTC).isoformat(),
            "attestation": "I approve execution of this trade",
            "signature": "signed",
        },
    }
    first = client.post("/v1/orders/execute", json=request)
    assert first.status_code == 200

    request["idempotency_key"] = "idem-22222222"
    second = client.post("/v1/orders/execute", json=request)
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
