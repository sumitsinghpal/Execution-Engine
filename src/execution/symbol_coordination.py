"""
Cross-agent order coordination: the one piece of the multi-agent
differentiator that isn't "give each agent its own copy of a safety
control" (see kill_switch_state.py and agent_exposure_guard.py for those).
This is the opposite direction — several agents can each individually be
well within their own limits and still, together, over-concentrate an
account in one symbol without any single one of them ever seeing that.

Unlike AgentExposureGuard and DrawdownGuard, this check runs synchronously
inside Executor.preview_order() rather than being polled — a coordination
problem needs to be caught at the moment a new order would make it worse,
not discovered afterward on the next poll. It composes with, and is
checked in addition to, every agent's own individual notional cap.

Concurrency note, stated plainly rather than glossed over: this reads
today's committed notional, adds the proposed order's notional, and
compares to the cap — a classic check-then-act sequence with no database-
level locking around it. Two previews for the same (account, symbol)
arriving genuinely concurrently could both read the same "before" total
and both pass, landing combined exposure over the cap anyway. Every other
notional/risk check in this codebase has the same property (there is no
row-level locking anywhere here, SQLite or otherwise), so this isn't a
regression — but it means this control, like the others, is a strong
deterrent under realistic sequential/lightly-concurrent load, not a
cryptographically tight guarantee under adversarial concurrency. Closing
that gap for real would need a database that supports row locks (e.g.
`SELECT ... FOR UPDATE` on a per-symbol counter row) or an application-
level mutex per (account, symbol) — flagged here rather than silently
assumed away.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional

from sqlmodel import Session, select

from src.config import Settings, get_settings
from src.execution.agent_exposure_guard import COMMITTED_ORDER_STATUSES
from src.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class SymbolExposureReport:
    account: str
    symbol: str
    trading_date: str
    committed_notional_usd: Decimal
    proposed_notional_usd: Decimal
    combined_notional_usd: Decimal
    cap_usd: Optional[Decimal]
    breached: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "account": self.account,
            "symbol": self.symbol,
            "trading_date": self.trading_date,
            "committed_notional_usd": str(self.committed_notional_usd),
            "proposed_notional_usd": str(self.proposed_notional_usd),
            "combined_notional_usd": str(self.combined_notional_usd),
            "cap_usd": str(self.cap_usd) if self.cap_usd is not None else None,
            "breached": self.breached,
        }


class SymbolCoordinationGuard:
    """Combined, cross-agent committed-notional check for one (account, symbol) pair."""

    def __init__(self, session: Session, settings: Optional[Settings] = None):
        """
        `settings` lets a caller that already has its own Settings instance
        (e.g. Executor, which builds the proposed order's notional using
        its own settings) reuse it here instead of this guard silently
        constructing an independent one — get_settings() isn't cached, so
        two separately-constructed instances can drift from each other
        (most visibly in tests that mutate one instance's fields directly).
        Defaults to a fresh get_settings() for standalone callers (e.g. the
        read-only /v1/risk/symbol-exposure endpoint).
        """
        self.session = session
        self.settings = settings or get_settings()

    @staticmethod
    def _today() -> str:
        return datetime.utcnow().date().isoformat()

    def _committed_notional_today(self, account: str, symbol: str) -> Decimal:
        from src.execution.executor import OrderRecord  # deferred: avoid circular import

        today = self._today()
        stmt = select(OrderRecord).where(
            OrderRecord.account == account,
            OrderRecord.symbol == symbol,
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
                    "symbol_coordination_unparseable_notional",
                    account=account,
                    symbol=symbol,
                    decision_id=order.decision_id,
                    raw_value=order.estimated_notional_usd,
                )
        return total

    def check(self, account: str, symbol: str, proposed_notional_usd: Decimal) -> SymbolExposureReport:
        """
        Whether adding proposed_notional_usd (this new order, not yet
        persisted) to today's already-committed notional for this
        (account, symbol) — summed across every agent, not just the one
        proposing this order — would exceed the combined cap, if one is
        configured.
        """
        cap = self.settings.max_combined_symbol_notional_usd
        committed = self._committed_notional_today(account, symbol)
        combined = committed + proposed_notional_usd
        breached = cap is not None and combined > cap

        report = SymbolExposureReport(
            account=account,
            symbol=symbol,
            trading_date=self._today(),
            committed_notional_usd=committed,
            proposed_notional_usd=proposed_notional_usd,
            combined_notional_usd=combined,
            cap_usd=cap,
            breached=breached,
        )

        if breached:
            logger.warning("symbol_coordination_limit_breached", **report.to_dict())

        return report


__all__ = ["SymbolExposureReport", "SymbolCoordinationGuard"]
