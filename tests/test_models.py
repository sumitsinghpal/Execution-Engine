"""Tests for data models and validation."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.models.occ_symbol import format_occ_symbol
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
        assert sample_trade_proposal.limit_price > 0  # live synthetic quote, not a fixed number — see conftest.py
        assert sample_trade_proposal.stop_price is None
    
    def test_valid_market_order(self, sample_market_order):
        """Valid MARKET order without prices."""
        assert sample_market_order.order_type == OrderType.MARKET
        assert sample_market_order.limit_price is None
        assert sample_market_order.stop_price is None

    def test_agent_id_defaults_to_default_for_single_agent_callers(self, sample_trade_proposal):
        """A caller that doesn't know about multi-agent deployments still works unchanged."""
        assert sample_trade_proposal.agent_id == "default"

    def test_agent_id_accepts_a_reasonable_custom_value(self, sample_trade_proposal):
        proposal = sample_trade_proposal.model_copy(update={"agent_id": "momentum-agent_01"})
        assert proposal.agent_id == "momentum-agent_01"

    def test_agent_id_rejects_disallowed_characters(self, sample_trade_proposal):
        # model_copy(update=...) skips validation entirely in Pydantic v2,
        # so this must go through model_validate to actually exercise the
        # field_validator.
        with pytest.raises(ValidationError):
            TradeProposal.model_validate(
                {**sample_trade_proposal.model_dump(), "agent_id": "bad agent id!"}
            )

    def test_agent_id_rejects_reserved_global_scope(self, sample_trade_proposal):
        with pytest.raises(ValidationError):
            TradeProposal.model_validate(
                {**sample_trade_proposal.model_dump(), "agent_id": "__global__"}
            )
    
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


class TestOptionTradeProposal:
    """TradeProposal for asset_type=OPTION uses a full 21-char OCC symbol instead of a plain ticker."""

    def _occ(self, underlying="NVDA", days_out=45, right="C", strike="120"):
        expiration = date.today() + timedelta(days=days_out)
        return format_occ_symbol(underlying, expiration, right, Decimal(strike))

    def test_valid_option_proposal(self):
        proposal = TradeProposal(
            decision_id="edge-option-001",
            account="primary",
            symbol=self._occ(),
            asset_type=AssetType.OPTION,
            instruction=Instruction.SELL,
            quantity=2,
            order_type=OrderType.LIMIT,
            limit_price=Decimal("2.50"),
        )
        assert proposal.asset_type == AssetType.OPTION
        assert len(proposal.symbol) == 21

    def test_underlying_symbol_extracts_the_occ_root(self):
        proposal = TradeProposal(
            decision_id="edge-option-002",
            account="primary",
            symbol=self._occ(underlying="SPY"),
            asset_type=AssetType.OPTION,
            instruction=Instruction.SELL,
            quantity=1,
            order_type=OrderType.MARKET,
        )
        assert proposal.underlying_symbol == "SPY"

    def test_equity_proposal_underlying_symbol_is_itself(self, sample_trade_proposal):
        assert sample_trade_proposal.underlying_symbol == sample_trade_proposal.symbol

    def test_option_asset_type_rejects_a_plain_ticker(self):
        with pytest.raises(ValidationError, match="Invalid OCC option symbol"):
            TradeProposal(
                decision_id="edge-option-003",
                account="primary",
                symbol="QQQ",
                asset_type=AssetType.OPTION,
                instruction=Instruction.SELL,
                quantity=1,
                order_type=OrderType.MARKET,
            )

    def test_equity_asset_type_rejects_an_occ_symbol(self):
        with pytest.raises(ValidationError):
            TradeProposal(
                decision_id="edge-option-004",
                account="primary",
                symbol=self._occ(),
                asset_type=AssetType.ETF,
                instruction=Instruction.BUY,
                quantity=1,
                order_type=OrderType.MARKET,
            )

    def test_malformed_occ_symbol_is_rejected(self):
        with pytest.raises(ValidationError):
            TradeProposal(
                decision_id="edge-option-005",
                account="primary",
                symbol="NOTAVALIDOCCSYMBOLXX",  # 20 chars, wrong length
                asset_type=AssetType.OPTION,
                instruction=Instruction.SELL,
                quantity=1,
                order_type=OrderType.MARKET,
            )
