from __future__ import annotations

from sqlmodel import Session, select

from src.models.orders import IdempotencyKeyRecord, IdempotencyOperation


class IdempotencyConflictError(ValueError):
    pass


def get_existing_record(
    session: Session, decision_id: str, operation: IdempotencyOperation
) -> IdempotencyKeyRecord | None:
    return session.exec(
        select(IdempotencyKeyRecord).where(
            IdempotencyKeyRecord.decision_id == decision_id,
            IdempotencyKeyRecord.operation == operation.value,
        )
    ).first()


def get_existing_response(session: Session, decision_id: str, operation: IdempotencyOperation) -> dict | None:
    record = get_existing_record(session, decision_id, operation)
    return None if record is None else record.response_json


def store_response(
    session: Session,
    *,
    decision_id: str,
    operation: IdempotencyOperation,
    idempotency_key: str,
    response_json: dict,
) -> None:
    existing = get_existing_record(session, decision_id, operation)
    if existing is not None:
        if existing.idempotency_key != idempotency_key:
            raise IdempotencyConflictError(
                f"idempotency key mismatch for decision_id={decision_id}, operation={operation.value}"
            )
        return
    session.add(
        IdempotencyKeyRecord(
            decision_id=decision_id,
            operation=operation.value,
            idempotency_key=idempotency_key,
            response_json=response_json,
        )
    )
