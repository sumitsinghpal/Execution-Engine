"""Tests for risk management."""

import pytest

from src.models.orders import TradeProposal, AssetType, Instruction, OrderType
from src.risk.limits import RiskChecker
from decimal import Decimal


class TestRiskChecker:
    """Test risk evaluation."""
    
    def test_kill_switch_blocks_all_orders(self, sample_trade_proposal):
        """Kill switch ON blocks all orders."""
        checker = RiskChecker()
        verdict = checker.evaluate(sample_trade_proposal, kill_switch_on=True)
        
        assert not verdict.approved
        assert not verdict.checks["kill_switch_off"]
        assert "Kill switch is ON" in verdict.rejections[0]
    
    def test_account_not_in_allowlist(self, sample_trade_proposal):
        """Account not in allowlist is rejected."""
        sample_trade_proposal.account = "forbidden_account"
        checker = RiskChecker()
        verdict = checker.evaluate(sample_trade_proposal, kill_switch_on=False)
        
        assert not verdict.approved
        assert not verdict.checks["account_allowed"]
        assert "not in allowlist" in verdict.rejections[0]
    
    def test_symbol_not_in_allowlist(self, sample_trade_proposal):
        """Symbol not in allowlist is rejected."""
        sample_trade_proposal.symbol = "FORBIDDEN"
        checker = RiskChecker()
        verdict = checker.evaluate(sample_trade_proposal, kill_switch_on=False)
        
        assert not verdict.approved
        assert not verdict.checks["symbol_allowed"]
    
    def test_symbol_in_denylist(self):
        """Symbol in denylist is rejected."""
        proposal = TradeProposal(
            decision_id="edge-test-denylist",
            account="primary",
            symbol="QQQ",
            asset_type=AssetType.ETF,
            instruction=Instruction.BUY,
            quantity=10,
            order_type=OrderType.MARKET,
        )
        
        # Temporarily add to denylist (in production, this would be config)
        checker = RiskChecker()
        original_denylist = checker.settings.symbol_denylist.copy()
        checker.settings.symbol_denylist = ["QQQ"]
        
        verdict = checker.evaluate(proposal, kill_switch_on=False)
        
        assert not verdict.approved
        assert not verdict.checks["symbol_not_denied"]
        
        # Restore
        checker.settings.symbol_denylist = original_denylist
    
    def test_notional_exceeds_limit(self):
        """Order notional exceeding limit is rejected."""
        proposal = TradeProposal(
            decision_id="edge-notional-test",
            account="primary",
            symbol="QQQ",
            asset_type=AssetType.ETF,
            instruction=Instruction.BUY,
            quantity=200,  # 200 * 721.50 = $144,300
            order_type=OrderType.LIMIT,
            limit_price=Decimal("721.50"),
        )
        
        checker = RiskChecker()
        verdict = checker.evaluate(proposal, kill_switch_on=False)
        
        assert not verdict.approved
        assert not verdict.checks["notional_limit"]
        assert "exceeds limit" in verdict.rejections[-1]
    
    def test_all_checks_pass(self, sample_trade_proposal):
        """Valid order passes all checks."""
        checker = RiskChecker()
        verdict = checker.evaluate(sample_trade_proposal, kill_switch_on=False)
        
        assert verdict.approved
        assert all(verdict.checks.values())
        assert len(verdict.rejections) == 0
