"""
The "armed for today" state that gates the autonomous trader's NEW
entries — separate from both the master autonomous_trading_enabled
setting and the kill switch, which both still apply on top of this.
Three independent layers now stop the autonomous trader from opening a
new position, each for a different reason:

1. settings.autonomous_trading_enabled (env-level, admin-configured):
   the loop doesn't run its cycle at all when this is off.
2. The kill switch (src/execution/kill_switch_state.py): an immediate,
   admin-gated emergency halt, enforced at order-preview time regardless
   of anything else.
3. This module (day-to-day, user-controlled): even with the loop
   running and the kill switch off, scan_for_entries() only opens a NEW
   position while an active DailyPlanRecord exists — i.e. only after the
   user has reviewed a strategy ranking and explicitly armed it, with a
   quantity and a strategy set THEY chose, not a default baked into
   settings.

Deliberately does not touch settings.autonomous_strategy_ids or
settings.autonomous_notional_per_trade_usd — those stay the static
fallback for anyone running this without the daily-ranking workflow at
all. An active plan, when present, simply takes priority in
scan_for_entries().

Managing/exiting positions already open is NEVER gated by this — see
autonomous_trader.manage_open_positions(), which always runs regardless
of armed state. Disarming (or letting the plan expire) stops new trades;
it was never meant to abandon a position already working.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Optional

from sqlmodel import Field, Session, SQLModel, select

from src.logging_config import get_logger

logger = get_logger(__name__)

DEFAULT_PLAN_TTL_HOURS = 24


class DailyPlanRecord(SQLModel, table=True):
    """
    One row per "arm" action. A new arm() call deactivates any prior
    active row rather than deleting it — the history of what was armed,
    by whom, and when is itself useful audit trail, same reasoning as
    every other *_state table in this codebase.
    """

    __tablename__ = "daily_plan"

    id: Optional[int] = Field(default=None, primary_key=True)
    strategy_ids_csv: str  # comma-separated — the strategies chosen to trade today
    notional_per_trade_usd: str  # Decimal as string, matching OrderRecord's own convention
    armed_by: str
    armed_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: datetime
    active: bool = Field(default=True, index=True)
    disarmed_at: Optional[datetime] = None
    disarmed_by: Optional[str] = None

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
            "expires_at": self.expires_at,
            "active": self.active,
            "disarmed_at": self.disarmed_at,
            "disarmed_by": self.disarmed_by,
        }


class DailyPlanService:
    def __init__(self, session: Session):
        self.session = session

    def get_active_plan(self) -> Optional[DailyPlanRecord]:
        """
        The plan scan_for_entries() should actually use right now — None
        means "not armed," whether because nothing was ever armed, the
        last arm was explicitly stopped, or it quietly expired. Expiry is
        checked here (not just at arm time) so a plan nobody disarmed
        stops authorizing new trades the moment it's past expires_at,
        without needing a separate background sweep to flip `active`.
        """
        stmt = select(DailyPlanRecord).where(DailyPlanRecord.active == True).order_by(DailyPlanRecord.armed_at.desc())  # noqa: E712
        plan = self.session.exec(stmt).first()
        if plan is None:
            return None
        if datetime.utcnow() >= plan.expires_at:
            return None
        return plan

    def arm(
        self,
        strategy_ids: list[str],
        notional_per_trade_usd: Decimal,
        armed_by: str,
        ttl_hours: float = DEFAULT_PLAN_TTL_HOURS,
    ) -> DailyPlanRecord:
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
            expires_at=datetime.utcnow() + timedelta(hours=ttl_hours),
        )
        self.session.add(plan)
        self.session.commit()
        self.session.refresh(plan)
        logger.info(
            "daily_plan_armed",
            strategy_ids=strategy_ids,
            notional_per_trade_usd=str(notional_per_trade_usd),
            armed_by=armed_by,
            expires_at=plan.expires_at.isoformat(),
        )
        return plan

    def disarm(self, disarmed_by: str) -> Optional[DailyPlanRecord]:
        """Stops new entries immediately — this is the "stop the trade" action. Already-open positions are untouched; see this module's docstring."""
        plan = self._deactivate_current(disarmed_by=disarmed_by)
        if plan:
            logger.info("daily_plan_disarmed", disarmed_by=disarmed_by, plan_id=plan.id)
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


__all__ = ["DEFAULT_PLAN_TTL_HOURS", "DailyPlanRecord", "DailyPlanService"]
