from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    "payload",
    [
        {
            "decision_id": "edge-20991231-005",
            "account": "primary",
            "symbol": "QQQ",
            "asset_type": "ETF",
            "instruction": "BUY IT NOW",
            "quantity": 10,
            "order_type": "LIMIT",
            "limit_price": "721.50",
        },
        {
            "decision_id": "edge-20991231-006",
            "account": "primary",
            "symbol": "QQQ",
            "asset_type": "ETF",
            "instruction": "BUY",
            "quantity": -1,
            "order_type": "LIMIT",
            "limit_price": "721.50",
        },
        {
            "decision_id": "invalid-format",
            "account": "primary",
            "symbol": "QQQ",
            "asset_type": "ETF",
            "instruction": "BUY",
            "quantity": 1,
            "order_type": "LIMIT",
            "limit_price": "721.50",
        },
    ],
)
def test_malformed_trade_proposals_rejected(client, payload):
    response = client.post("/v1/orders/preview", json=payload)
    assert response.status_code == 422
