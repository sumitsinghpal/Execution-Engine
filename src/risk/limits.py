"""
Risk management: hard safety checks before order submission.
All checks must pass; reject-by-default philosophy.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from src.config import get_settings
from src.logging_config import get_logger
from src.models.orders import Instruction, OrderType, TradeProposal

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

    def evaluate(
        self,
        proposal: TradeProposal,
        kill_switch_on: bool,
        quote: Optional[dict[str, Any]] = None,
    ) -> RiskVerdict:
        """
        Run all risk checks on a trade proposal.

        `quote` should be a fresh dict from BrokerAdapter.get_quote() (see
        src/brokers/base.py) — pass None only when no quote could be
        fetched at all, which itself fails the quote-freshness check below
        for anything that isn't a pre-priced LIMIT order.
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

        # 5. Quote freshness — a stale or missing quote invalidates any
        # price-derived check below, so this runs before them and they
        # degrade gracefully (not silently pass) when it fails.
        quote_ok, quote_reject_reason = self._check_quote_freshness(proposal, quote)
        checks["quote_fresh"] = quote_ok
        if quote_reject_reason:
            rejections.append(quote_reject_reason)

        # 6. Limit price sanity — reject a LIMIT order priced too far from
        # the live quote rather than routing a likely fat-finger price.
        if proposal.order_type == OrderType.LIMIT and proposal.limit_price is not None:
            price_ok, price_reject_reason = self._check_limit_price_reasonable(proposal, quote, quote_ok)
            checks["limit_price_reasonable"] = price_ok
            if price_reject_reason:
                rejections.append(price_reject_reason)

        # 7. Order notional limit
        notional = self._calculate_notional(proposal, quote, quote_ok)
        if notional is None:
            checks["notional_limit"] = False
            rejections.append(
                "Cannot verify order notional against the limit: no valid price available "
                "(no limit_price and no usable live quote)"
            )
        else:
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

    def _check_quote_freshness(
        self, proposal: TradeProposal, quote: Optional[dict[str, Any]]
    ) -> tuple[bool, Optional[str]]:
        """
        A LIMIT order with its own price doesn't strictly need a live quote
        to be *submittable*, but everything else (MARKET orders, and the
        limit-price sanity check above) does. Missing/stale quotes fail
        this check either way — the caller decides what that costs them.
        """
        if quote is None:
            if proposal.order_type == OrderType.LIMIT and proposal.limit_price is not None:
                return True, None  # priced order, no live quote required
            return False, f"No live quote available for '{proposal.symbol}'"

        quote_time_raw = quote.get("quote_time")
        if not quote_time_raw:
            return False, f"Quote for '{proposal.symbol}' has no timestamp — cannot verify freshness"

        try:
            quote_time = datetime.fromisoformat(quote_time_raw)
        except (TypeError, ValueError):
            return False, f"Quote for '{proposal.symbol}' has an unparseable timestamp: {quote_time_raw!r}"

        if quote_time.tzinfo is None:
            quote_time = quote_time.replace(tzinfo=UTC)

        age_seconds = (datetime.now(UTC) - quote_time).total_seconds()
        if age_seconds > self.settings.max_quote_age_seconds:
            return False, (
                f"Quote for '{proposal.symbol}' is stale ({age_seconds:.1f}s old; "
                f"max allowed {self.settings.max_quote_age_seconds}s)"
            )
        if age_seconds < -5:  # small tolerance for clock skew
            return False, f"Quote for '{proposal.symbol}' has a timestamp in the future"

        return True, None

    def _check_limit_price_reasonable(
        self, proposal: TradeProposal, quote: Optional[dict[str, Any]], quote_fresh: bool
    ) -> tuple[bool, Optional[str]]:
        if not quote_fresh or quote is None:
            # Can't sanity-check against a quote we don't trust; the quote
            # freshness rejection above already covers why this doesn't pass.
            return False, None

        reference_price = quote.get("last") or quote.get("bid") or quote.get("ask")
        if not reference_price:
            return False, f"Quote for '{proposal.symbol}' has no usable reference price"

        try:
            reference = Decimal(str(reference_price))
            deviation = abs(proposal.limit_price - reference) / reference
        except (InvalidOperation, ZeroDivisionError):
            return False, f"Could not compute limit price deviation for '{proposal.symbol}'"

        if deviation > self.settings.max_limit_price_deviation_pct:
            return False, (
                f"Limit price {proposal.limit_price} deviates {deviation:.1%} from live quote "
                f"{reference} for '{proposal.symbol}' (max allowed "
                f"{self.settings.max_limit_price_deviation_pct:.1%}) — likely mispriced"
            )
        return True, None

    @staticmethod
    def _calculate_notional(
        proposal: TradeProposal, quote: Optional[dict[str, Any]], quote_fresh: bool
    ) -> Optional[Decimal]:
        """
        Estimate order notional value. Prefers the order's own limit price
        (an explicit commitment); falls back to a fresh live quote for
        MARKET/STOP orders. Returns None — not a silent $0 — when neither
        is available, since a $0 notional previously made the notional-limit
        check vacuously true for every MARKET order regardless of size.
        """
        if proposal.limit_price:
            return proposal.limit_price * Decimal(proposal.quantity)

        if quote_fresh and quote is not None:
            reference_price = quote.get("last") or quote.get("bid") or quote.get("ask")
            if reference_price:
                try:
                    return Decimal(str(reference_price)) * Decimal(proposal.quantity)
                except InvalidOperation:
                    return None

        return None


class PreTradeValidator:
    """Lightweight, defensive pre-trade structural checks."""

    @staticmethod
    def validate_instruction_format(proposal: TradeProposal) -> bool:
        """Ensure no free-form text instructions."""
        # Pydantic already enforces enum; this is defensive
        return proposal.instruction in [Instruction.BUY, Instruction.SELL]
