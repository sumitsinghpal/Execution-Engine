"""
Position reconciliation: compare what this system believes it holds against
what the broker actually reports, before trading resumes each session.

This is distinct from ReconciliationService (reconciliation.py), which polls
and updates the status of one individual order at a time. This module
answers a different, session-level question — "does our aggregate picture
of the account match reality?" — which per-order status polling alone never
checks: two orders could each individually reconcile to FILLED correctly
and the account could still be out of sync (a manual trade placed directly
with the broker outside this system, a corporate action, a missed fill
event, a bug). Nothing in this codebase asked that question before this.

In PAPER mode, only the "what do we believe we hold" half of that question
is answerable — PaperBrokerAdapter has no independent position ledger to
compare against (see PositionReconciliationService.reconcile()'s
docstring), so a mismatch there can never be a real drift signal and never
trips the kill switch. This check earns its keep once a real broker
(Schwab) is connected, where get_positions() reflects a genuinely
independent source of truth.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlmodel import Session, select

from src.accounts.profiles import AccountProfile
from src.brokers.base import BrokerAdapter
from src.brokers.paper import PaperBrokerAdapter
from src.config import get_settings
from src.logging_config import get_logger
from src.models.orders import OrderStatus

logger = get_logger(__name__)

_FILLED_STATUSES = {OrderStatus.FILLED.value, OrderStatus.PARTIAL_FILL.value}


@dataclass
class PositionMismatch:
    symbol: str
    local_quantity: int
    broker_quantity: int

    @property
    def delta(self) -> int:
        return self.broker_quantity - self.local_quantity


@dataclass
class PositionReconciliationReport:
    account: str
    checked_at: str
    matched: bool
    local_positions: Dict[str, int]
    broker_positions: Dict[str, int]
    mismatches: List[PositionMismatch] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "account": self.account,
            "checked_at": self.checked_at,
            "matched": self.matched,
            "local_positions": self.local_positions,
            "broker_positions": self.broker_positions,
            "mismatches": [
                {"symbol": m.symbol, "local_quantity": m.local_quantity, "broker_quantity": m.broker_quantity, "delta": m.delta}
                for m in self.mismatches
            ],
        }


class PositionReconciliationService:
    """
    Sums this system's own fill history into a believed position per symbol,
    fetches the broker's actual reported positions, and diffs the two.
    """

    def __init__(self, session: Session, broker: Optional[BrokerAdapter] = None):
        self.session = session
        self.settings = get_settings()
        self.broker = broker or PaperBrokerAdapter()

    def get_local_positions(self, account: str) -> Dict[str, int]:
        """
        Public, read-only accessor for this system's own believed
        positions — no broker call, no kill-switch side effects. Unlike
        reconcile()/reconcile_or_halt(), this never talks to the broker
        at all, so it's safe for any purely-read-only caller (e.g. an
        external analysis service, like signal-integrity-layer's
        portfolio-impact check) that just wants "what do we currently
        hold" without risking the broker-comparison/auto-halt behavior
        those methods carry for a real, non-paper broker.
        """
        return self._compute_local_positions(account)

    def _compute_local_positions(self, account: str) -> Dict[str, int]:
        """
        Sums filled/partially-filled order quantities for the account,
        signed by instruction (BUY adds, SELL subtracts).
        """
        from src.execution.executor import OrderRecord  # deferred: avoid circular import

        stmt = select(OrderRecord).where(OrderRecord.account == account)
        orders = self.session.exec(stmt).all()

        positions: Dict[str, int] = {}
        for order in orders:
            if order.status not in _FILLED_STATUSES:
                continue
            quantity = order.filled_quantity or 0
            if quantity <= 0:
                continue
            sign = 1 if order.instruction.upper() == "BUY" else -1
            positions[order.symbol] = positions.get(order.symbol, 0) + sign * quantity

        return {symbol: qty for symbol, qty in positions.items() if qty != 0}

    @staticmethod
    def _parse_broker_positions(raw_positions: List[Dict[str, Any]]) -> Dict[str, int]:
        positions: Dict[str, int] = {}
        for entry in raw_positions:
            instrument = entry.get("instrument", {})
            symbol = instrument.get("symbol")
            if not symbol:
                continue
            long_qty = int(entry.get("longQuantity", 0) or 0)
            short_qty = int(entry.get("shortQuantity", 0) or 0)
            net = long_qty - short_qty
            if net != 0:
                positions[symbol] = positions.get(symbol, 0) + net
        return positions

    async def reconcile(self, account: str) -> PositionReconciliationReport:
        """
        Runs the comparison and returns a full report. Does not itself
        decide what to do about a mismatch — see reconcile_or_halt below for
        the pre-session gate that actually acts on this.

        PaperBrokerAdapter is a deliberate special case: it has no
        independent position ledger of its own (get_positions() always
        returns [] — see its docstring; a real position simulation would
        need to make it stateful, a bigger change than this check needs)
        while local_positions is now genuinely populated from real fills
        (see Executor.execute_order()). Comparing "what we believe we
        hold" against a broker that structurally cannot report anything
        would flag every single paper trade as a "mismatch" and
        auto-halt the kill switch on the very first one — not a real
        drift signal, just an artifact of what this broker adapter can
        answer. local_positions is still computed and reported (useful
        on its own — "here's what this account currently holds"); it's
        only compared against broker_positions, and only capable of
        tripping the kill switch, for a broker with a genuine ledger to
        check against.
        """
        profile = self.settings.get_account_profile(account)
        raw_broker_positions = await self.broker.get_positions(profile)
        broker_positions = self._parse_broker_positions(raw_broker_positions)
        local_positions = self._compute_local_positions(account)

        if isinstance(self.broker, PaperBrokerAdapter):
            mismatches: List[PositionMismatch] = []
        else:
            all_symbols = sorted(set(local_positions) | set(broker_positions))
            mismatches = [
                PositionMismatch(
                    symbol=symbol,
                    local_quantity=local_positions.get(symbol, 0),
                    broker_quantity=broker_positions.get(symbol, 0),
                )
                for symbol in all_symbols
                if local_positions.get(symbol, 0) != broker_positions.get(symbol, 0)
            ]

        report = PositionReconciliationReport(
            account=account,
            checked_at=datetime.utcnow().isoformat(),
            matched=not mismatches,
            local_positions=local_positions,
            broker_positions=broker_positions,
            mismatches=mismatches,
        )

        if report.matched:
            logger.info("position_reconciliation_matched", account=account, positions=local_positions)
        else:
            logger.critical(
                "position_reconciliation_mismatch",
                account=account,
                mismatches=[m.__dict__ for m in mismatches],
            )

        return report

    async def reconcile_or_halt(self, account: str, halted_by: str = "system") -> PositionReconciliationReport:
        """
        Intended entry point for "before every trading session": runs the
        comparison and, on any mismatch, automatically trips the kill switch
        rather than merely logging it — a positions mismatch means this
        system's belief about the account is provably wrong, which is
        exactly the situation the kill switch exists for. Trading stays
        halted until a human investigates and manually clears it.
        """
        from src.execution.kill_switch_state import KillSwitchService  # deferred: avoid circular import

        report = await self.reconcile(account)
        if not report.matched:
            KillSwitchService(self.session).set_state(
                enabled=True,
                set_by=halted_by,
                reason=(
                    f"Auto-halt: pre-session position reconciliation mismatch for "
                    f"account '{account}': {[m.symbol for m in report.mismatches]}"
                ),
            )
        return report


__all__ = ["PositionMismatch", "PositionReconciliationReport", "PositionReconciliationService"]
