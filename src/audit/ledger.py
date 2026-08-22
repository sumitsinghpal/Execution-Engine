from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

from sqlmodel import Session

from src.models.orders import AuditLedgerEntry


def payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return sha256(encoded).hexdigest()


def append_audit_event(
    session: Session,
    *,
    actor: str,
    action: str,
    decision_id: str | None,
    payload: dict[str, Any],
    correlation_id: str,
    before_status: str | None = None,
    after_status: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    session.add(
        AuditLedgerEntry(
            actor=actor,
            action=action,
            decision_id=decision_id,
            payload_hash=payload_hash(payload),
            before_status=before_status,
            after_status=after_status,
            correlation_id=correlation_id,
            details=details or {},
        )
    )
