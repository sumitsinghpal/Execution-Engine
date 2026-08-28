"""
Persisted kill switch state — the single source of truth for both the admin
API endpoints (src/api/server.py) and the actual order-blocking check
(src/execution/executor.py).

This exists because those two were previously disconnected: server.py kept
its own in-process module-level global that POST /v1/kill-switch/on and
/off toggled, while Executor._get_kill_switch_state() read a completely
different value — settings.kill_switch_enabled, a static default sourced
from .env — that the API endpoints never touched. Turning the kill switch
"on" through the API updated what /v1/kill-switch/status reported, but did
not, in fact, stop a single new order from being previewed or executed.

Backing this with the database (rather than an in-process global) also
fixes a second latent issue for free: an in-process global's state is lost
on every restart and isn't shared across worker processes, both of which
matter for something whose entire job is "reliably stop trading when told
to."

Scoped, redundant halts: a system running multiple coordinating agents
can't rely on one central switch alone — a single misbehaving agent should
be stoppable without an all-stop, and the fleet-wide switch still needs to
exist as a backstop that overrides everything regardless of per-agent
state. So this is a table of scoped rows (one row per scope) rather than a
single singleton: scope == GLOBAL_SCOPE is the fleet-wide switch every
order is always subject to; scope == an agent_id is that one agent's own
switch. is_halted() below is what callers should actually use to decide
whether an order for a given agent may proceed — it is the OR of both.
"""

from datetime import datetime
from typing import Optional

from sqlmodel import Field, Session, SQLModel, select

GLOBAL_SCOPE = "__global__"


class KillSwitchRecord(SQLModel, table=True):
    """One row per kill switch scope: the fleet-wide switch, or one agent's own switch."""

    __tablename__ = "kill_switch_state"

    scope: str = Field(default=GLOBAL_SCOPE, primary_key=True)
    enabled: bool = False
    set_by: str = "system"
    set_at: datetime = Field(default_factory=datetime.utcnow)
    reason: Optional[str] = None


class KillSwitchService:
    """Reads and writes kill switch records, one per scope."""

    def __init__(self, session: Session):
        self.session = session

    def _get_or_create(self, scope: str) -> KillSwitchRecord:
        record = self.session.get(KillSwitchRecord, scope)
        if record is None:
            record = KillSwitchRecord(scope=scope, enabled=False, set_by="system")
            self.session.add(record)
            self.session.commit()
            self.session.refresh(record)
        return record

    def get_state(self, scope: str = GLOBAL_SCOPE) -> KillSwitchRecord:
        return self._get_or_create(scope)

    def is_enabled(self, scope: str = GLOBAL_SCOPE) -> bool:
        """Whether this specific scope's own switch is on — does not consult any other scope."""
        return self._get_or_create(scope).enabled

    def is_halted(self, agent_id: str) -> bool:
        """
        The check order execution actually cares about: is this agent
        blocked from trading right now, for any reason? True if either the
        fleet-wide switch is on, or this agent's own switch is on — halting
        one agent never requires (and never implies) halting the others,
        and the fleet-wide switch always overrides every agent's own state.
        """
        if self.is_enabled(GLOBAL_SCOPE):
            return True
        if agent_id == GLOBAL_SCOPE:
            return True  # not a real agent id; treat as already covered above
        return self.is_enabled(agent_id)

    def set_state(
        self, enabled: bool, set_by: str, reason: Optional[str] = None, scope: str = GLOBAL_SCOPE
    ) -> KillSwitchRecord:
        record = self._get_or_create(scope)
        was_enabled = record.enabled
        record.enabled = enabled
        record.set_by = set_by
        record.set_at = datetime.utcnow()
        record.reason = reason
        self.session.add(record)
        self.session.commit()
        self.session.refresh(record)
        # Every trip/clear goes through this one method regardless of
        # trigger — manual admin action, DrawdownGuard.check_and_halt, or
        # PositionReconciliationService.reconcile_or_halt — so hooking the
        # notification here covers all of them without a call site in each
        # trigger. Only on an actual transition, not a redundant re-set of
        # the same state, so re-confirming an already-on switch doesn't
        # spam the webhook.
        if record.enabled != was_enabled:
            _notify_kill_switch_change(scope, record)
        return record

    def known_agent_scopes(self) -> list[str]:
        """
        Every non-global scope that has ever had a kill switch row created
        for it — i.e. every agent that has ever been explicitly halted or
        cleared. This is a record of switch activity, not a registry of
        every agent that has ever traded (an agent that has never been
        touched via the per-agent endpoints has no row and won't appear
        here); see /v1/agents/status in src/api/server.py, which merges
        this with agent_ids actually seen in order history for a fuller
        picture.
        """
        stmt = select(KillSwitchRecord.scope).where(KillSwitchRecord.scope != GLOBAL_SCOPE)
        return list(self.session.exec(stmt).all())


def _notify_kill_switch_change(scope: str, record: KillSwitchRecord) -> None:
    """Deferred imports — config/notifications importing back into execution-layer modules elsewhere makes an eager import here risk a cycle; this module has no need of either at import time."""
    from src.config import get_settings
    from src.notifications.webhook import notify_sync

    scope_label = "fleet-wide" if scope == GLOBAL_SCOPE else f"agent '{scope}'"
    state_label = "ENABLED — trading halted" if record.enabled else "disabled — trading resumed"
    text = f":octagonal_sign: Kill switch {state_label} ({scope_label}), by {record.set_by}"
    if record.reason:
        text += f"\n> {record.reason}"
    notify_sync(get_settings(), text)


__all__ = ["KillSwitchRecord", "KillSwitchService", "GLOBAL_SCOPE"]
