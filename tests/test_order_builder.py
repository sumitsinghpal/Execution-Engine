"""Tests for order building and checksums."""

from decimal import Decimal

from src.broker.order_builder import OrderBuilder
from src.models.orders import AssetType, Instruction, OrderType, TradeProposal


class TestOrderBuilder:
    """Test order spec construction."""
    
    def test_build_limit_order_spec(self, sample_trade_proposal):
        """Build valid LIMIT order spec."""
        builder = OrderBuilder()
        spec = builder.build_order_spec(sample_trade_proposal, "primary")
        
        assert spec["orderId"] == sample_trade_proposal.decision_id
        assert spec["symbol"] == "QQQ"
        assert spec["quantity"] == 10
        assert spec["instruction"] == "BUY"
        assert spec["orderType"] == "LIMIT"
        assert spec["limitPrice"] == "270.50"
        assert "stopPrice" not in spec
    
    def test_build_market_order_spec(self, sample_market_order):
        """Build valid MARKET order spec."""
        builder = OrderBuilder()
        spec = builder.build_order_spec(sample_market_order, "primary")
        
        assert spec["orderType"] == "MARKET"
        assert "limitPrice" not in spec
        assert "stopPrice" not in spec
    
    def test_build_stop_order_spec(self, sample_stop_order):
        """Build valid STOP order spec."""
        builder = OrderBuilder()
        spec = builder.build_order_spec(sample_stop_order, "primary")
        
        assert spec["orderType"] == "STOP"
        assert spec["stopPrice"] == "200.00"
        assert "limitPrice" not in spec
    
    def test_build_stop_limit_order_spec(self):
        """Build valid STOP_LIMIT order spec."""
        proposal = TradeProposal(
            decision_id="edge-stop-limit",
            account="primary",
            symbol="SPY",
            asset_type=AssetType.ETF,
            instruction=Instruction.BUY,
            quantity=5,
            order_type=OrderType.STOP_LIMIT,
            stop_price=Decimal("450.00"),
            limit_price=Decimal("451.00"),
        )
        
        builder = OrderBuilder()
        spec = builder.build_order_spec(proposal, "primary")
        
        assert spec["orderType"] == "STOP_LIMIT"
        assert spec["stopPrice"] == "450.00"
        assert spec["limitPrice"] == "451.00"


class TestPayloadChecksum:
    """Test deterministic checksum generation."""
    
    def test_same_proposal_same_checksum(self, sample_trade_proposal):
        """Same proposal generates same checksum."""
        builder = OrderBuilder()
        
        checksum1 = builder.compute_payload_checksum(sample_trade_proposal)
        checksum2 = builder.compute_payload_checksum(sample_trade_proposal)
        
        assert checksum1 == checksum2
        assert len(checksum1) == 64  # SHA256 hex
    
    def test_different_proposals_different_checksums(self, sample_trade_proposal, sample_market_order):
        """Different proposals generate different checksums."""
        builder = OrderBuilder()
        
        checksum1 = builder.compute_payload_checksum(sample_trade_proposal)
        checksum2 = builder.compute_payload_checksum(sample_market_order)
        
        assert checksum1 != checksum2
    
    def test_checksum_is_hex_string(self, sample_trade_proposal):
        """Checksum is valid hex string."""
        builder = OrderBuilder()
        checksum = builder.compute_payload_checksum(sample_trade_proposal)
        
        assert isinstance(checksum, str)
        assert len(checksum) == 64
        assert all(c in "0123456789abcdef" for c in checksum)
    
    def test_checksum_changes_with_price(self):
        """Checksum changes when price changes."""
        proposal1 = TradeProposal(
            decision_id="edge-price-test",
            account="primary",
            symbol="QQQ",
            asset_type=AssetType.ETF,
            instruction=Instruction.BUY,
            quantity=10,
            order_type=OrderType.LIMIT,
            limit_price=Decimal("721.50"),
        )
        
        proposal2 = TradeProposal(
            decision_id="edge-price-test",
            account="primary",
            symbol="QQQ",
            asset_type=AssetType.ETF,
            instruction=Instruction.BUY,
            quantity=10,
            order_type=OrderType.LIMIT,
            limit_price=Decimal("721.51"),  # Different price
        )
        
        builder = OrderBuilder()
        checksum1 = builder.compute_payload_checksum(proposal1)
        checksum2 = builder.compute_payload_checksum(proposal2)
        
        assert checksum1 != checksum2
