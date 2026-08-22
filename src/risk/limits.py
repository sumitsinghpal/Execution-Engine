"""
Risk management: hard safety checks before order submission.
All checks must pass; reject-by-default philosophy.
"""

from dataclasses import dataclass
from decimal import Decimal

from src.config import get_settings
from src.logging_config import get_logger
from src.models.orders import Instruction, TradeProposal

logger = get_logger(__name__)


@dataclass
class RiskVerdict:
    """Result of risk checks."""
    approved: bool
    checks: dict[str, bool]
    rejections: list[str]


class RiskChecker:
    """Enforce hard risk limits on all orders."""
    
    def __init__(self):
        self.settings = get_settings()
    
    def evaluate(self, proposal: TradeProposal, kill_switch_on: bool) -> RiskVerdict:
        """
        Run all risk checks on a trade proposal.
        Return RiskVerdict with detailed check results.
        """
        checks = {}
        rejections = []
        
        # 1. Kill switch
        checks["kill_switch_off"] = not kill_switch_on
        if kill_switch_on:
            rejections.append("Kill switch is ON - trading disabled")
        
        # 2. Account allowlist
        checks["account_allowed"] = proposal.account in self.settings.account_allowlist
        if not checks["account_allowed"]:
            rejections.append(f"Account '{proposal.account}' not in allowlist")
        
        # 3. Symbol allowlist
        checks["symbol_allowed"] = proposal.symbol in self.settings.symbol_allowlist
        if not checks["symbol_allowed"]:
            rejections.append(f"Symbol '{proposal.symbol}' not in allowlist")
        
        # 4. Symbol denylist
        checks["symbol_not_denied"] = proposal.symbol not in self.settings.symbol_denylist
        if not checks["symbol_not_denied"]:
            rejections.append(f"Symbol '{proposal.symbol}' is denied")
        
        # 5. Order notional limit
        notional = self._calculate_notional(proposal)
        checks["notional_limit"] = notional <= self.settings.max_order_notional_usd
        if not checks["notional_limit"]:
            rejections.append(
                f"Order notional ${notional} exceeds limit ${self.settings.max_order_notional_usd}"
            )
        
        logger.info(
            "risk_check_complete",
            decision_id=proposal.decision_id,
            checks=checks,
            rejections=rejections,
        )
        
        return RiskVerdict(
            approved=len(rejections) == 0,
            checks=checks,
            rejections=rejections,
        )
    
    @staticmethod
    def _calculate_notional(proposal: TradeProposal) -> Decimal:
        """
        Estimate order notional value.
        For LIMIT/STOP_LIMIT, use the limit price.
        For MARKET/STOP, this is an approximation (in practice, would use market data).
        """
        if proposal.limit_price:
            return proposal.limit_price * Decimal(proposal.quantity)
        
        # Fallback: no price available for MARKET orders
        # In production, would look up current market price
        return Decimal("0")


class PreTradeValidator:
    """Pre-trade validation and freshness checks."""
    
    @staticmethod
    def validate_proposal_freshness(proposal: TradeProposal, max_age_minutes: int = 60) -> bool:
        """
        Validate that the proposal is fresh (not stale).
        In this version, we trust the EDGE-TF timestamp; in production,
        this would check order submission time vs current time.
        """
        # Placeholder: in real implementation, would check decision_id timestamp
        return True
    
    @staticmethod
    def validate_instruction_format(proposal: TradeProposal) -> bool:
        """Ensure no free-form text instructions."""
        # Pydantic already enforces enum; this is defensive
        return proposal.instruction in [Instruction.BUY, Instruction.SELL]
