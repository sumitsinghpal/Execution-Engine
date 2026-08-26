"""
Per-agent daily notional exposure cap: a second, independent backstop
alongside the account-level DrawdownGuard (drawdown_guard.py), scoped to
one agent instead of one account.

Why this exists in addition to DrawdownGuard: the account-level guard
answers "has this account lost too much today" from the broker's own
reported equity — the right question for protecting the account as a
whole, but it can't attribute the loss to a specific agent, and it only
reacts after a loss has actually happened. This guard answers a different,
narrower question that's useful precisely because several agents may be
trading the same account at once: "has this one agent alone committed more
capital today than it's allowed to" — a redundant, agent-scoped tripwire
that can catch one misbehaving agent (e.g. stuck in a buy loop) well before
its trades move the account's overall equity enough for DrawdownGuard to
notice, and localizes the halt to that agent via its own kill switch scope
(see kill_switch_state.py) rather than the whole account.

This is exposure (capital committed), not realized P&L — the codebase has
no fill-price time series to compute true per-agent P&L from yet. Opt-in
per agent via AgentRiskProfile.max_daily_notional_usd; an agent with no
configured cap is never checked.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional

from sqlmodel import Session, select

from src.config import get_settings
from src.logging_config import get_logger
from src.models.orders import OrderStatus

logger = get_logger(__name__)

# Orders that represent capital actually committed to the broker — not a
# preview that was never approved, and not one that was submitted and then
# definitively didn't happen.
COMMITTED_ORDER_STATUSES = {
    OrderStatus.SUBMITTED.value,
    OrderStatus.ACKNOWLEDGED.value,
    OrderStatus.PARTIAL_FILL.value,
    OrderStatus.FILLED.value,
}


@dataclass
class AgentExposureReport:
    agent_id: str
    trading_date: str
    committed_notional_usd: Decimal
    cap_usd: Optional[Decimal]
    breached: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "trading_date": self.trading_date,
            "committed_notional_usd": str(self.committed_notional_usd),
            "cap_usd": str(self.cap_usd) if self.cap_usd is not None else None,
            "breached": self.breached,
        }


class AgentExposureGuard:
    """Sums one agent's committed notional for the day and compares it to its own cap."""

    def __init__(self, session: Session):
        self.session = session
        self.settings = get_settings()

    @staticmethod
    def _today() -> str:
        return datetime.utcnow().date().isoformat()

    def _committed_notional_today(self, agent_id: str) -> Decimal:
        from src.execution.executor import OrderRecord  # deferred: avoid circular import

        today = self._today()
        stmt = select(OrderRecord).where(
            OrderRecord.agent_id == agent_id,
            OrderRecord.status.in_(COMMITTED_ORDER_STATUSES),
        )
        total = Decimal("0")
        for order in self.session.exec(stmt).all():
            if order.created_at.date().isoformat() != today:
                continue
            if not order.estimated_notional_usd:
                continue
            try:
                total += Decimal(order.estimated_notional_usd)
            except InvalidOperation:
                logger.warning(
                    "agent_exposure_unparseable_notional",
                    agent_id=agent_id,
                    decision_id=order.decision_id,
                    raw_value=order.estimated_notional_usd,
                )
        return total

    def check_exposure(self, agent_id: str) -> AgentExposureReport:
        """Computes today's committed notional for the agent and compares it against its own cap, if any."""
        cap = self.settings.get_agent_risk_profile(agent_id).max_daily_notional_usd
        committed = self._committed_notional_today(agent_id)
        breached = cap is not None and committed > cap

        report = AgentExposureReport(
            agent_id=agent_id,
            trading_date=self._today(),
            committed_notional_usd=committed,
            cap_usd=cap,
            breached=breached,
        )

        if breached:
            logger.critical("agent_exposure_limit_breached", **report.to_dict())
        else:
            logger.info("agent_exposure_check_ok", **report.to_dict())

        return report

    def check_and_halt(self, agent_id: str, halted_by: str = "agent_exposure_guard") -> AgentExposureReport:
        """
        Evaluates exposure and, on breach, halts this agent's own kill
        switch scope only — never the fleet-wide switch, and never another
        agent's scope. Trading for this agent stays halted until a human
        clears it via the admin-gated /v1/kill-switch/agents/{agent_id}/off.
        """
        from src.execution.kill_switch_state import KillSwitchService  # deferred: avoid circular import

        report = self.check_exposure(agent_id)
        if report.breached:
            KillSwitchService(self.session).set_state(
                enabled=True,
                set_by=halted_by,
                reason=(
                    f"Auto-halt: agent '{agent_id}' committed notional "
                    f"${report.committed_notional_usd} exceeded its daily cap ${report.cap_usd}"
                ),
                scope=agent_id,
            )
        return report


__all__ = ["AgentExposureReport", "AgentExposureGuard", "COMMITTED_ORDER_STATUSES"]
