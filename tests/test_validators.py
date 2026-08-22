from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.models.orders import TradeProposal


def test_limit_requires_limit_price():
    with pytest.raises(ValidationError):
        TradeProposal(
            decision_id="edge-20260821-001",
            account="primary",
            symbol="QQQ",
            asset_type="ETF",
            instruction="BUY",
            quantity=10,
            order_type="LIMIT",
        )


def test_market_rejects_limit_price():
    with pytest.raises(ValidationError):
        TradeProposal(
            decision_id="edge-20260821-001",
            account="primary",
            symbol="QQQ",
            asset_type="ETF",
            instruction="BUY",
            quantity=10,
            order_type="MARKET",
            limit_price=Decimal("10.00"),
        )


def test_unknown_field_rejected():
    with pytest.raises(ValidationError):
        TradeProposal.model_validate(
            {
                "decision_id": "edge-20260821-001",
                "account": "primary",
                "symbol": "QQQ",
                "asset_type": "ETF",
                "instruction": "BUY",
                "quantity": 10,
                "order_type": "LIMIT",
                "limit_price": "10.00",
                "free_form": "buy aggressively now",
            }
        )
