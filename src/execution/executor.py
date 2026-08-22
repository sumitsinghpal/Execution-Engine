from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Any
from uuid import uuid4

from sqlmodel import Session, select

from src.audit.ledger import append_audit_event
from src.broker.order_builder import build_schwab_order_payload
from src.broker.schwab_client import SchwabClient
from src.config import get_settings
from src.execution.approval import verify_approval_artifact
from src.execution.idempotency import (
    IdempotencyConflictError,
    get_existing_record,
    get_existing_response,
    store_response,
)
from src.models.orders import (
    ApprovalRecord,
    AssetType,
    ExecuteOrderRequest,
    ExecutionOrderRecord,
    IdempotencyOperation,
    Instruction,
    OrderStatus,
    OrderType,
    PreviewRecord,
    TradeProposal,
    TradeProposalRecord,
)
from src.risk.pretrade import run_pretrade_checks


class ExecutionServiceError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


def _checksum(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return sha256(canonical.encode("utf-8")).hexdigest()


def _ensure_decision_freshness(decision_id: str) -> None:
    settings = get_settings()
    try:
        decision_date = datetime.strptime(decision_id.split("-")[1], "%Y%m%d").replace(tzinfo=UTC)
    except Exception as exc:  # pragma: no cover - schema already validates pattern
        raise ExecutionServiceError("INVALID_DECISION_ID", "decision_id date segment is invalid", 422) from exc
    max_age = timedelta(days=settings.decision_max_age_days)
    if datetime.now(UTC) - decision_date > max_age:
        raise ExecutionServiceError("STALE_DECISION", "decision_id is outside allowed freshness window", 422)


def _proposal_to_record(proposal: TradeProposal) -> TradeProposalRecord:
    return TradeProposalRecord(
        decision_id=proposal.decision_id,
        account=proposal.account,
        symbol=proposal.symbol.upper(),
        asset_type=proposal.asset_type.value,
        instruction=proposal.instruction.value,
        quantity=proposal.quantity,
        order_type=proposal.order_type.value,
        limit_price=None if proposal.limit_price is None else f"{proposal.limit_price:.2f}",
    )


async def preview_trade(session: Session, proposal: TradeProposal, correlation_id: str) -> dict[str, Any]:
    existing = get_existing_response(session, proposal.decision_id, IdempotencyOperation.PREVIEW)
    if existing is not None:
        return existing

    _ensure_decision_freshness(proposal.decision_id)

    verdict = run_pretrade_checks(session, proposal)
    if not verdict.passed:
        raise ExecutionServiceError("RISK_REJECTED", "pretrade risk checks rejected proposal", 422, verdict.to_dict())

    existing_proposal = session.exec(
        select(TradeProposalRecord).where(TradeProposalRecord.decision_id == proposal.decision_id)
    ).first()
    if existing_proposal is not None:
        raise ExecutionServiceError("DECISION_ALREADY_USED", "decision_id has already been used", 409)

    normalized_order = build_schwab_order_payload(proposal)
    client = SchwabClient()
    broker_preview = await client.preview_order(normalized_order)

    preview_id = str(uuid4())
    expires_at = datetime.now(UTC) + timedelta(minutes=get_settings().preview_ttl_minutes)
    checksum = _checksum(normalized_order)

    session.add(_proposal_to_record(proposal))
    session.add(
        PreviewRecord(
            id=preview_id,
            decision_id=proposal.decision_id,
            checksum=checksum,
            expires_at=expires_at,
            risk_verdict=verdict.to_dict(),
            normalized_order=normalized_order,
            broker_preview=broker_preview,
            status=OrderStatus.PREVIEWED.value,
        )
    )

    response = {
        "decision_id": proposal.decision_id,
        "preview_id": preview_id,
        "checksum": checksum,
        "expires_at": expires_at.isoformat(),
        "risk_verdict": verdict.to_dict(),
        "broker_preview": broker_preview,
    }
    store_response(
        session,
        decision_id=proposal.decision_id,
        operation=IdempotencyOperation.PREVIEW,
        idempotency_key=proposal.decision_id,
        response_json=response,
    )
    append_audit_event(
        session,
        actor="system",
        action="ORDER_PREVIEWED",
        decision_id=proposal.decision_id,
        payload=normalized_order,
        correlation_id=correlation_id,
        before_status=None,
        after_status=OrderStatus.PREVIEWED.value,
        details={"risk": verdict.to_dict()},
    )
    return response


async def execute_trade(session: Session, request: ExecuteOrderRequest, correlation_id: str) -> dict[str, Any]:
    existing = get_existing_record(session, request.decision_id, IdempotencyOperation.EXECUTE)
    if existing is not None:
        if existing.idempotency_key != request.idempotency_key:
            raise ExecutionServiceError(
                "IDEMPOTENCY_CONFLICT",
                "idempotency key mismatch for existing decision_id/operation",
                409,
            )
        return existing.response_json

    preview = session.exec(
        select(PreviewRecord).where(PreviewRecord.id == request.preview_id, PreviewRecord.decision_id == request.decision_id)
    ).first()
    if preview is None:
        raise ExecutionServiceError("PREVIEW_NOT_FOUND", "preview record not found", 404)
    expires_at = preview.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at < datetime.now(UTC):
        raise ExecutionServiceError("PREVIEW_EXPIRED", "preview has expired", 409)

    proposal_record = session.exec(
        select(TradeProposalRecord).where(TradeProposalRecord.decision_id == request.decision_id)
    ).first()
    if proposal_record is None:
        raise ExecutionServiceError("PROPOSAL_NOT_FOUND", "trade proposal not found", 404)

    proposal = TradeProposal(
        decision_id=proposal_record.decision_id,
        account=proposal_record.account,
        symbol=proposal_record.symbol,
        asset_type=AssetType(proposal_record.asset_type),
        instruction=Instruction(proposal_record.instruction),
        quantity=proposal_record.quantity,
        order_type=OrderType(proposal_record.order_type),
        limit_price=None if proposal_record.limit_price is None else Decimal(proposal_record.limit_price),
    )
    recalculated = _checksum(preview.normalized_order)
    if recalculated != preview.checksum:
        raise ExecutionServiceError("CHECKSUM_MISMATCH", "preview checksum mismatch", 409)

    verdict = run_pretrade_checks(session, proposal)
    if not verdict.passed:
        raise ExecutionServiceError("RISK_REJECTED", "pretrade risk checks rejected execute", 422, verdict.to_dict())

    try:
        verify_approval_artifact(request.approval)
    except ValueError as exc:
        raise ExecutionServiceError("INVALID_APPROVAL", str(exc), 422) from exc

    session.add(
        ApprovalRecord(
            decision_id=request.decision_id,
            preview_id=request.preview_id,
            approved_by=request.approval.approved_by,
            approved_at=request.approval.approved_at,
            attestation=request.approval.attestation,
            signature=request.approval.signature,
        )
    )

    client = SchwabClient()
    broker_response = await client.submit_order(preview.normalized_order)
    execution_status = OrderStatus.SUBMITTED.value

    existing_execution = session.exec(
        select(ExecutionOrderRecord).where(ExecutionOrderRecord.decision_id == request.decision_id)
    ).first()
    if existing_execution is None:
        session.add(
            ExecutionOrderRecord(
                decision_id=request.decision_id,
                preview_id=request.preview_id,
                broker_order_id=broker_response.get("broker_order_id"),
                status=execution_status,
                broker_response=broker_response,
            )
        )
    else:
        existing_execution.broker_order_id = broker_response.get("broker_order_id")
        existing_execution.status = execution_status
        existing_execution.broker_response = broker_response
        existing_execution.updated_at = datetime.now(UTC)
        session.add(existing_execution)

    response = {
        "decision_id": request.decision_id,
        "preview_id": request.preview_id,
        "execution_id": str(uuid4()),
        "broker_order_id": broker_response.get("broker_order_id"),
        "status": execution_status,
        "submitted_at": broker_response.get("submitted_at", datetime.now(UTC).isoformat()),
    }
    try:
        store_response(
            session,
            decision_id=request.decision_id,
            operation=IdempotencyOperation.EXECUTE,
            idempotency_key=request.idempotency_key,
            response_json=response,
        )
    except IdempotencyConflictError as exc:
        raise ExecutionServiceError("IDEMPOTENCY_CONFLICT", str(exc), 409) from exc

    append_audit_event(
        session,
        actor=request.approval.approved_by,
        action="ORDER_EXECUTED",
        decision_id=request.decision_id,
        payload=preview.normalized_order,
        correlation_id=correlation_id,
        before_status=OrderStatus.APPROVED.value,
        after_status=execution_status,
        details={"broker_order_id": broker_response.get("broker_order_id")},
    )

    return response


def order_status(session: Session, decision_id: str) -> dict[str, Any]:
    execution = session.exec(
        select(ExecutionOrderRecord).where(ExecutionOrderRecord.decision_id == decision_id)
    ).first()
    if execution is not None:
        return {
            "decision_id": decision_id,
            "status": execution.status,
            "broker_order_id": execution.broker_order_id,
            "updated_at": execution.updated_at.isoformat(),
        }

    preview = session.exec(select(PreviewRecord).where(PreviewRecord.decision_id == decision_id)).first()
    if preview is not None:
        return {
            "decision_id": decision_id,
            "status": preview.status,
            "preview_id": preview.id,
            "updated_at": preview.created_at.isoformat(),
        }

    raise ExecutionServiceError("ORDER_NOT_FOUND", "decision_id not found", 404)
