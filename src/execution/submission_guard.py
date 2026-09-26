"""Durable submission ownership and immutable preview routing bindings."""
import hashlib
import json
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlmodel import SQLModel, Field


class SubmissionClaim(SQLModel, table=True):
    __tablename__ = "submission_claims"
    decision_id: str = Field(primary_key=True)
    preview_id: str
    idempotency_key: str = Field(unique=True)
    claimed_at: datetime = Field(default_factory=datetime.utcnow)


class PreviewBinding(SQLModel, table=True):
    __tablename__ = "preview_bindings"
    decision_id: str = Field(primary_key=True)
    route_hash: str
    terms_json: str


def route_hash(profile, mode):
    value = {"profile": profile.model_dump(mode="json"), "mode": mode.upper()}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def order_terms(order):
    keys = ("account", "agent_id", "symbol", "asset_type", "instruction", "quantity",
            "order_type", "limit_price", "stop_price", "strategy_id", "strategy_stop_loss_price",
            "strategy_take_profit_price", "algo_duration_minutes", "algo_slices", "payload_checksum")
    return json.dumps({k: getattr(order, k) for k in keys}, sort_keys=True)


def bind_preview(session, order, profile, mode):
    session.add(PreviewBinding(decision_id=order.decision_id, route_hash=route_hash(profile, mode),
                               terms_json=order_terms(order)))
    session.commit()


def check_binding(session, order, profile, mode):
    binding = session.get(PreviewBinding, order.decision_id)
    if binding is None:
        raise ValueError("Legacy or incomplete preview: create a new decision and obtain approval")
    if binding.route_hash != route_hash(profile, mode) or binding.terms_json != order_terms(order):
        raise ValueError("Preview terms or broker routing changed: new preview and approval required")


def claim_submission(session, decision_id, preview_id, idempotency_key):
    session.add(SubmissionClaim(decision_id=decision_id, preview_id=preview_id,
                                idempotency_key=idempotency_key))
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise ValueError("Submission already claimed; retrieve status and reconcile, do not resubmit") from exc
