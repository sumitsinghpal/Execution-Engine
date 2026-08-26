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

Backing this with the database (a singleton row) rather than another
in-process global also fixes a second latent issue for free: an in-process
global's state is lost on every restart and isn't shared across worker
processes, both of which matter for something whose entire job is "reliably
stop trading when told to."
"""

from datetime import datetime
from typing import Optional

from sqlmodel import Field, Session, SQLModel

_SINGLETON_ID = 1


class KillSwitchRecord(SQLModel, table=True):
    """Singleton row holding the current kill switch state."""

    __tablename__ = "kill_switch_state"

    id: int = Field(default=_SINGLETON_ID, primary_key=True)
    enabled: bool = False
    set_by: str = "system"
    set_at: datetime = Field(default_factory=datetime.utcnow)
    reason: Optional[str] = None


class KillSwitchService:
    """Reads and writes the one persisted kill switch record."""

    def __init__(self, session: Session):
        self.session = session

    def _get_or_create(self) -> KillSwitchRecord:
        record = self.session.get(KillSwitchRecord, _SINGLETON_ID)
        if record is None:
            record = KillSwitchRecord(id=_SINGLETON_ID, enabled=False, set_by="system")
            self.session.add(record)
            self.session.commit()
            self.session.refresh(record)
        return record

    def get_state(self) -> KillSwitchRecord:
        return self._get_or_create()

    def is_enabled(self) -> bool:
        return self._get_or_create().enabled

    def set_state(self, enabled: bool, set_by: str, reason: Optional[str] = None) -> KillSwitchRecord:
        record = self._get_or_create()
        record.enabled = enabled
        record.set_by = set_by
        record.set_at = datetime.utcnow()
        record.reason = reason
        self.session.add(record)
        self.session.commit()
        self.session.refresh(record)
        return record


__all__ = ["KillSwitchRecord", "KillSwitchService"]
