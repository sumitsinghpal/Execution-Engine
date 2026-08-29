"""
The "armed" state that gates the autonomous trader's NEW entries —
separate from both the master autonomous_trading_enabled setting and the
kill switch, which both still apply on top of this. Three independent
layers now stop the autonomous trader from opening a new position, each
for a different reason:

1. settings.autonomous_trading_enabled (env-level, admin-configured):
   the loop doesn't run its cycle at all when this is off.
2. The kill switch (src/execution/kill_switch_state.py): an immediate,
   admin-gated emergency halt, enforced at order-preview time regardless
   of anything else.
3. This module (user-controlled, ongoing): even with the loop running
   and the kill switch off, scan_for_entries() only opens a NEW position
   while an active DailyPlanRecord exists.

This is a ONE-TIME arm, not a daily chore: a user hands over a quantity
once (arm()) and it stays authorized indefinitely — no expiry, no
re-confirmation — until they explicitly disarm() it. What keeps "trading
the best strategies" honest over time without asking the user again is
rotate_strategies(): a background loop (see run_strategy_rotation_loop)
periodically re-ranks recent performance (src/execution/strategy_ranking.py)
and updates WHICH strategies the existing plan trades, in place — same
row, same quantity, same armed_by/armed_at, just a refreshed strategy
list. The user set the budget and said "go"; which specific strategies
earn that budget on a given day is the system's job, not something they
should have to click through repeatedly.

Deliberately does not touch settings.autonomous_strategy_ids or
settings.autonomous_notional_per_trade_usd — those stay the static
fallback for anyone running this without the ranking/arming workflow at
all. An active plan, when present, simply takes priority in
scan_for_entries().

Managing/exiting positions already open is NEVER gated by this — see
autonomous_trader.manage_open_positions(), which always runs regardless
of armed state. Disarming stops new trades; it was never meant to
abandon a position already working.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from sqlmodel import Field, Session, SQLModel, select

from src.logging_config import get_logger

logger = get_logger(__name__)


class DailyPlanRecord(SQLModel, table=True):
    """
    One row per "arm" action, kept active (and its strategy_ids
    periodically rotated in place — see rotate_strategies()) until
    disarmed. A new arm() call deactivates any prior active row rather
    than deleting it — the history of what was armed, by whom, and when
    is itself useful audit trail, same reasoning as every other *_state
    table in this codebase.
    """

    __tablename__ = "daily_plan"

    id: Optional[int] = Field(default=None, primary_key=True)
    strategy_ids_csv: str  # comma-separated — the strategies currently trading this plan's budget; refreshed by rotate_strategies()
    notional_per_trade_usd: str  # Decimal as string, matching OrderRecord's own convention — set once at arm() time, untouched by rotation
    armed_by: str
    armed_at: datetime = Field(default_factory=datetime.utcnow)
    active: bool = Field(default=True, index=True)
    disarmed_at: Optional[datetime] = None
    disarmed_by: Optional[str] = None
    last_rotated_at: Optional[datetime] = None  # None until the first automatic rotation actually changes something

    @property
    def strategy_ids(self) -> list[str]:
        return [s for s in self.strategy_ids_csv.split(",") if s]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "strategy_ids": self.strategy_ids,
            "notional_per_trade_usd": self.notional_per_trade_usd,
            "armed_by": self.armed_by,
            "armed_at": self.armed_at,
            "active": self.active,
            "disarmed_at": self.disarmed_at,
            "disarmed_by": self.disarmed_by,
            "last_rotated_at": self.last_rotated_at,
        }


class DailyPlanService:
    def __init__(self, session: Session):
        self.session = session

    def get_active_plan(self) -> Optional[DailyPlanRecord]:
        """The plan scan_for_entries() should actually use right now — None means "not armed" (never armed, or explicitly disarmed)."""
        stmt = select(DailyPlanRecord).where(DailyPlanRecord.active == True).order_by(DailyPlanRecord.armed_at.desc())  # noqa: E712
        return self.session.exec(stmt).first()

    def arm(
        self,
        strategy_ids: list[str],
        notional_per_trade_usd: Decimal,
        armed_by: str,
    ) -> DailyPlanRecord:
        """One-time authorization: this quantity trades these strategies until disarm() is called. No expiry — see this module's docstring for why, and rotate_strategies() for how the strategy list stays current without asking again."""
        if not strategy_ids:
            raise ValueError("Cannot arm with an empty strategy list")
        if notional_per_trade_usd <= 0:
            raise ValueError("notional_per_trade_usd must be positive")

        # Only one plan is ever active at a time — arming replaces
        # whatever was armed before rather than stacking, so there's
        # never ambiguity about which quantity/strategy set is actually
        # in force.
        self._deactivate_current(disarmed_by=armed_by)

        plan = DailyPlanRecord(
            strategy_ids_csv=",".join(strategy_ids),
            notional_per_trade_usd=str(notional_per_trade_usd),
            armed_by=armed_by,
        )
        self.session.add(plan)
        self.session.commit()
        self.session.refresh(plan)
        logger.info(
            "daily_plan_armed",
            strategy_ids=strategy_ids,
            notional_per_trade_usd=str(notional_per_trade_usd),
            armed_by=armed_by,
        )
        return plan

    def disarm(self, disarmed_by: str) -> Optional[DailyPlanRecord]:
        """Stops new entries immediately — this is the "stop the trade" action. Already-open positions are untouched; see this module's docstring."""
        plan = self._deactivate_current(disarmed_by=disarmed_by)
        if plan:
            logger.info("daily_plan_disarmed", disarmed_by=disarmed_by, plan_id=plan.id)
        return plan

    def rotate_strategies(self, strategy_ids: list[str]) -> Optional[DailyPlanRecord]:
        """
        Updates the currently active plan's strategy list IN PLACE —
        same row, same notional_per_trade_usd, same armed_by/armed_at —
        so an ongoing arm session keeps trading whatever's ranking best
        without the user re-arming. Called by run_strategy_rotation_loop
        on a timer; a no-op (returns None) when nothing is currently
        armed, since there's nothing to rotate. An empty new strategy_ids
        list is refused the same way arm() refuses one, rather than
        silently leaving the plan with no strategies to trade.
        """
        if not strategy_ids:
            raise ValueError("Cannot rotate to an empty strategy list")

        plan = self.get_active_plan()
        if plan is None:
            return None

        old_ids = plan.strategy_ids
        plan.strategy_ids_csv = ",".join(strategy_ids)
        plan.last_rotated_at = datetime.utcnow()
        self.session.add(plan)
        self.session.commit()
        self.session.refresh(plan)
        if set(old_ids) != set(strategy_ids):
            logger.info("daily_plan_rotated", plan_id=plan.id, old_strategy_ids=old_ids, new_strategy_ids=strategy_ids)
        return plan

    def _deactivate_current(self, disarmed_by: str) -> Optional[DailyPlanRecord]:
        # Deactivates every active row, not just one — arm() always
        # deactivates before inserting, so at most one should ever be
        # active, but this stays correct even if that invariant were
        # ever violated (e.g. a manual DB edit).
        stmt = select(DailyPlanRecord).where(DailyPlanRecord.active == True)  # noqa: E712
        rows = list(self.session.exec(stmt).all())
        if not rows:
            return None
        now = datetime.utcnow()
        for row in rows:
            row.active = False
            row.disarmed_at = now
            row.disarmed_by = disarmed_by
            self.session.add(row)
        self.session.commit()
        for row in rows:
            self.session.refresh(row)
        return rows[0]


__all__ = ["DailyPlanRecord", "DailyPlanService"]
