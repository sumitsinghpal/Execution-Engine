"""Tests for data models and validation."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.models.orders import (
    AssetType,
    Instruction,
    OrderType,
    TradeProposal,
)


class TestTradeProposalValidation:
    """Test TradeProposal schema validation."""
    
    def test_valid_limit_order(self, sample_trade_proposal):
        """Valid LIMIT order with limit_price."""
        assert sample_trade_proposal.order_type == OrderType.LIMIT
        assert sample_trade_proposal.limit_price == Decimal("270.50")
        assert sample_trade_proposal.stop_price is None
    
    def test_valid_market_order(self, sample_market_order):
        """Valid MARKET order without prices."""
        assert sample_market_order.order_type == OrderType.MARKET
        assert sample_market_order.limit_price is None
        assert sample_market_order.stop_price is None
    
    def test_limit_order_requires_limit_price(self):
        """LIMIT order must have limit_price."""
        with pytest.raises(ValidationError) as exc_info:
            TradeProposal(
                decision_id="edge-20260821-004",
                account="primary",
                symbol="QQQ",
                asset_type=AssetType.ETF,
                instruction=Instruction.BUY,
                quantity=10,
                order_type=OrderType.LIMIT,
                # Missing limit_price
            )
        assert "limit_price is required" in str(exc_info.value)
    
    def test_stop_order_requires_stop_price(self):
        """STOP order must have stop_price."""
        with pytest.raises(ValidationError) as exc_info:
            TradeProposal(
                decision_id="edge-20260821-005",
                account="primary",
                symbol="QQQ",
                asset_type=AssetType.ETF,
                instruction=Instruction.BUY,
                quantity=10,
                order_type=OrderType.STOP,
                # Missing stop_price
            )
        assert "stop_price is required" in str(exc_info.value)
    
    def test_symbol_uppercase_validation(self):
        """Symbol must be uppercase."""
        with pytest.raises(ValidationError):
            TradeProposal(
                decision_id="edge-20260821-006",
                account="primary",
                symbol="qqq",  # lowercase
                asset_type=AssetType.ETF,
                instruction=Instruction.BUY,
                quantity=10,
                order_type=OrderType.MARKET,
            )
    
    def test_quantity_must_be_positive(self):
        """Quantity must be positive."""
        with pytest.raises(ValidationError):
            TradeProposal(
                decision_id="edge-20260821-007",
                account="primary",
                symbol="QQQ",
                asset_type=AssetType.ETF,
                instruction=Instruction.BUY,
                quantity=-10,  # negative
                order_type=OrderType.MARKET,
            )
    
    def test_reject_unknown_fields(self):
        """Unknown fields must be rejected."""
        with pytest.raises(ValidationError):
            TradeProposal(
                decision_id="edge-20260821-008",
                account="primary",
                symbol="QQQ",
                asset_type=AssetType.ETF,
                instruction=Instruction.BUY,
                quantity=10,
                order_type=OrderType.MARKET,
                unknown_field="should fail",  # Unknown field
            )
    
    def test_strict_enums(self):
        """Enum fields must be strict."""
        with pytest.raises(ValidationError):
            TradeProposal(
                decision_id="edge-20260821-009",
                account="primary",
                symbol="QQQ",
                asset_type="INVALID",  # Invalid asset type
                instruction=Instruction.BUY,
                quantity=10,
                order_type=OrderType.MARKET,
            )


class TestOrderTypeValidation:
    """Test order type specific validation."""
    
    def test_stop_limit_requires_both_prices(self):
        """STOP_LIMIT requires both stop and limit prices."""
        proposal = TradeProposal(
            decision_id="edge-20260821-010",
            account="primary",
            symbol="QQQ",
            asset_type=AssetType.ETF,
            instruction=Instruction.BUY,
            quantity=10,
            order_type=OrderType.STOP_LIMIT,
            stop_price=Decimal("720.00"),
            limit_price=Decimal("721.50"),
        )
        assert proposal.stop_price == Decimal("720.00")
        assert proposal.limit_price == Decimal("721.50")
