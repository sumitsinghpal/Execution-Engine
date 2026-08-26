"""Tests for risk management."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from src.models.orders import TradeProposal, AssetType, Instruction, OrderType
from src.risk.limits import RiskChecker


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
    
    def test_all_checks_pass(self, sample_trade_proposal, sample_quote):
        """Valid order, with a fresh live quote to check its limit price against, passes all checks."""
        checker = RiskChecker()
        verdict = checker.evaluate(sample_trade_proposal, kill_switch_on=False, quote=sample_quote)

        assert verdict.approved
        assert all(verdict.checks.values())
        assert len(verdict.rejections) == 0

    def test_limit_price_far_from_quote_is_rejected(self, sample_quote):
        """A LIMIT price wildly different from the live quote is rejected as likely mispriced."""
        proposal = TradeProposal(
            decision_id="edge-mispriced-test",
            account="primary",
            symbol="QQQ",
            asset_type=AssetType.ETF,
            instruction=Instruction.BUY,
            quantity=1,
            order_type=OrderType.LIMIT,
            limit_price=Decimal("1000.00"),  # nowhere near the $270.24 quote
        )

        checker = RiskChecker()
        verdict = checker.evaluate(proposal, kill_switch_on=False, quote=sample_quote)

        assert not verdict.approved
        assert not verdict.checks["limit_price_reasonable"]
        assert "likely mispriced" in verdict.rejections[-1]

    def test_stale_quote_is_rejected(self, sample_trade_proposal):
        """A quote older than the configured max age fails the freshness check."""
        stale_quote = {
            "symbol": "QQQ",
            "bid": Decimal("270.11"),
            "ask": Decimal("270.37"),
            "last": Decimal("270.24"),
            "quote_time": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
            "mode": "PAPER",
        }

        checker = RiskChecker()
        verdict = checker.evaluate(sample_trade_proposal, kill_switch_on=False, quote=stale_quote)

        assert not verdict.approved
        assert not verdict.checks["quote_fresh"]
        assert "stale" in verdict.rejections[0]

    def test_market_order_notional_uses_live_quote_not_zero(self, sample_market_order, sample_quote):
        """
        A MARKET order (no limit_price) must have its notional estimated from
        the live quote, not silently pass as $0 regardless of size.
        """
        sample_market_order.quantity = 1000  # 1000 * ~270.24 quote price is well over the $100k limit
        quote = dict(sample_quote)
        quote["symbol"] = sample_market_order.symbol

        checker = RiskChecker()
        verdict = checker.evaluate(sample_market_order, kill_switch_on=False, quote=quote)

        assert not verdict.checks["notional_limit"]
        assert "exceeds limit" in " ".join(verdict.rejections)

    def test_market_order_with_no_quote_fails_closed(self, sample_market_order):
        """
        A MARKET order with no quote available at all must be rejected, not
        silently approved with an unverifiable $0 notional.
        """
        checker = RiskChecker()
        verdict = checker.evaluate(sample_market_order, kill_switch_on=False, quote=None)

        assert not verdict.approved
        assert not verdict.checks["quote_fresh"]
