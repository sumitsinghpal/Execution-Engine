from __future__ import annotations

from decimal import Decimal

from src.broker.order_builder import build_schwab_order_payload
from src.models.orders import TradeProposal


def test_order_builder_deterministic_limit_payload():
    proposal = TradeProposal(
        decision_id="edge-20260821-001",
        account="primary",
        symbol="qqq",
        asset_type="ETF",
        instruction="BUY",
        quantity=10,
        order_type="LIMIT",
        limit_price=Decimal("721.5"),
    )
    payload = build_schwab_order_payload(proposal)
    assert payload["price"] == "721.50"
    assert payload["orderLegCollection"][0]["instrument"]["symbol"] == "QQQ"
