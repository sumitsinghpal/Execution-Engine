"""Tests for risk management."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from src.agents.profiles import AgentRiskProfile
from src.models.occ_symbol import format_occ_symbol
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


class TestPerAgentRiskOverrides:
    """
    An agent's own AgentRiskProfile can only tighten the fleet-wide
    defaults, never loosen them — allowlist is intersected, denylist is
    unioned, and the notional cap is the min of the two.
    """

    def test_agent_symbol_allowlist_narrows_the_fleet_wide_one(self, sample_trade_proposal, sample_quote):
        """An agent-specific allowlist that excludes this symbol rejects it even though the fleet allows it."""
        checker = RiskChecker()
        assert sample_trade_proposal.symbol in checker.settings.symbol_allowlist  # sanity: fleet allows QQQ

        proposal = sample_trade_proposal.model_copy(update={"agent_id": "narrow-agent"})
        checker.settings.agent_risk_profiles["narrow-agent"] = AgentRiskProfile(symbol_allowlist=["SPY"])

        verdict = checker.evaluate(proposal, kill_switch_on=False, quote=sample_quote)

        assert not verdict.approved
        assert not verdict.checks["symbol_allowed"]

    def test_agent_symbol_allowlist_cannot_add_a_symbol_the_fleet_denies(self, sample_quote):
        """An agent's own allowlist is intersected with the fleet's, not substituted for it."""
        proposal = TradeProposal(
            decision_id="edge-agent-allowlist-cannot-loosen",
            agent_id="loosening-agent",
            account="primary",
            symbol="XOMXX",  # valid symbol shape, but not in the fleet-wide allowlist
            asset_type=AssetType.ETF,
            instruction=Instruction.BUY,
            quantity=1,
            order_type=OrderType.MARKET,
        )
        checker = RiskChecker()
        checker.settings.agent_risk_profiles["loosening-agent"] = AgentRiskProfile(symbol_allowlist=["XOMXX"])

        quote = dict(sample_quote)
        quote["symbol"] = "XOMXX"
        verdict = checker.evaluate(proposal, kill_switch_on=False, quote=quote)

        assert not verdict.approved
        assert not verdict.checks["symbol_allowed"], (
            "a symbol not in the fleet-wide allowlist must stay rejected even if an agent's own list includes it"
        )

    def test_agent_symbol_denylist_adds_to_the_fleet_wide_one(self, sample_trade_proposal, sample_quote):
        """An agent-specific denylist entry rejects a symbol the fleet-wide denylist doesn't touch."""
        checker = RiskChecker()
        proposal = sample_trade_proposal.model_copy(update={"agent_id": "extra-denylist-agent"})
        checker.settings.agent_risk_profiles["extra-denylist-agent"] = AgentRiskProfile(
            symbol_denylist=[sample_trade_proposal.symbol]
        )

        verdict = checker.evaluate(proposal, kill_switch_on=False, quote=sample_quote)

        assert not verdict.approved
        assert not verdict.checks["symbol_not_denied"]

    def test_agent_notional_cap_is_the_tighter_of_agent_and_fleet(self, sample_quote):
        """A lower agent-specific notional cap rejects an order the fleet-wide cap alone would approve."""
        proposal = TradeProposal(
            decision_id="edge-agent-notional-cap",
            agent_id="tight-cap-agent",
            account="primary",
            symbol="QQQ",
            asset_type=AssetType.ETF,
            instruction=Instruction.BUY,
            quantity=10,
            order_type=OrderType.LIMIT,
            limit_price=Decimal("270.50"),  # $2,705 total — well under the $100k fleet default
        )
        checker = RiskChecker()
        checker.settings.agent_risk_profiles["tight-cap-agent"] = AgentRiskProfile(
            max_order_notional_usd=Decimal("1000")
        )

        verdict = checker.evaluate(proposal, kill_switch_on=False, quote=sample_quote)

        assert not verdict.approved
        assert not verdict.checks["notional_limit"]

    def test_unconfigured_agent_uses_fleet_wide_defaults_unchanged(self, sample_trade_proposal, sample_quote):
        """An agent with no profile entry at all behaves exactly like single-agent callers always did."""
        checker = RiskChecker()
        proposal = sample_trade_proposal.model_copy(update={"agent_id": "never-configured-agent"})

        verdict = checker.evaluate(proposal, kill_switch_on=False, quote=sample_quote)

        assert verdict.approved


class TestOptionRiskChecks:
    """RiskChecker's option-specific behavior: underlying-based allowlist, the expiration window, and the 100x contract multiplier."""

    @staticmethod
    def _occ_proposal(underlying="QQQ", days_out=45, limit_price="2.50", quantity=2):
        symbol = format_occ_symbol(underlying, date.today() + timedelta(days=days_out), "C", Decimal("400"))
        return TradeProposal(
            decision_id=f"edge-option-risk-{underlying}-{days_out}",
            account="primary",
            symbol=symbol,
            asset_type=AssetType.OPTION,
            instruction=Instruction.SELL,
            quantity=quantity,
            order_type=OrderType.LIMIT,
            limit_price=Decimal(limit_price),
        )

    @staticmethod
    def _quote_for(proposal):
        return {
            "symbol": proposal.symbol,
            "bid": proposal.limit_price,
            "ask": proposal.limit_price,
            "last": proposal.limit_price,
            "quote_time": datetime.now(UTC).isoformat(),
            "mode": "PAPER",
        }

    def test_option_on_an_allowed_underlying_passes_symbol_checks(self):
        proposal = self._occ_proposal(underlying="QQQ")  # QQQ is in the default fleet-wide allowlist
        checker = RiskChecker()

        verdict = checker.evaluate(proposal, kill_switch_on=False, quote=self._quote_for(proposal))

        assert verdict.checks["symbol_allowed"]
        assert verdict.checks["symbol_not_denied"]

    def test_option_on_a_non_allowed_underlying_is_rejected(self):
        proposal = self._occ_proposal(underlying="NVDA")  # not in the default fleet-wide allowlist
        checker = RiskChecker()

        verdict = checker.evaluate(proposal, kill_switch_on=False, quote=self._quote_for(proposal))

        assert not verdict.approved
        assert not verdict.checks["symbol_allowed"]

    def test_option_expiring_too_soon_is_rejected(self):
        proposal = self._occ_proposal(days_out=0)  # expires today; default min is 1 day
        checker = RiskChecker()

        verdict = checker.evaluate(proposal, kill_switch_on=False, quote=self._quote_for(proposal))

        assert not verdict.approved
        assert verdict.checks["expiration_allowed"] is False

    def test_option_expiring_too_far_out_is_rejected(self):
        proposal = self._occ_proposal(days_out=1000)  # beyond the default 730-day max
        checker = RiskChecker()

        verdict = checker.evaluate(proposal, kill_switch_on=False, quote=self._quote_for(proposal))

        assert not verdict.approved
        assert verdict.checks["expiration_allowed"] is False

    def test_option_within_the_expiration_window_passes(self):
        proposal = self._occ_proposal(days_out=45)
        checker = RiskChecker()

        verdict = checker.evaluate(proposal, kill_switch_on=False, quote=self._quote_for(proposal))

        assert verdict.checks["expiration_allowed"] is True

    def test_equity_orders_have_no_expiration_check_at_all(self, sample_trade_proposal, sample_quote):
        checker = RiskChecker()
        verdict = checker.evaluate(sample_trade_proposal, kill_switch_on=False, quote=sample_quote)
        assert "expiration_allowed" not in verdict.checks

    def test_option_notional_uses_the_100x_contract_multiplier(self):
        """2 contracts at $2.50 premium is really $500 notional (2 * 100 * $2.50), not $5."""
        proposal = self._occ_proposal(limit_price="2.50", quantity=2)
        checker = RiskChecker()
        checker.settings.max_order_notional_usd = Decimal("400")  # below $500, above $5

        verdict = checker.evaluate(proposal, kill_switch_on=False, quote=self._quote_for(proposal))

        assert not verdict.approved
        assert not verdict.checks["notional_limit"]
        assert verdict.notional_usd == Decimal("500.00")

    def test_option_notional_multiplier_does_not_apply_to_equities(self, sample_trade_proposal, sample_quote):
        checker = RiskChecker()
        verdict = checker.evaluate(sample_trade_proposal, kill_switch_on=False, quote=sample_quote)
        expected = sample_trade_proposal.limit_price * sample_trade_proposal.quantity
        assert verdict.notional_usd == expected
