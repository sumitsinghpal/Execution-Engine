"""
Persisted, human-reviewable signals sourced from an external decision engine
— the counterpart to src/execution/strategy_signals.py (which holds signals
this system's own internal strategy catalog fired), for trades a *separate*
brain has already approved on its own side. EDGE-TF is the first and only
source wired up today (src/execution/edge_tf_connector.py polls its
execution gateway); `source` exists so a second decision engine — e.g.
EP-Edge-Earnings-Engine — can land signals in this same table later without
a schema change.

Same safety shape as the internal strategy signals: landing here has no
side effect beyond a database row. A human reviews it in the dashboard,
loads it into the order ticket, and only from there does it run through the
ordinary preview -> risk checks -> approve -> execute gate. The one thing
specific to an *external* signal is what happens at execute time — see
src/execution/edge_tf_connector.py's claim_upstream/report_upstream — this
module only stores what was polled and tracks whether it's been acted on.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from sqlmodel import Field, Session, SQLModel, select

from src.logging_config import get_logger

logger = get_logger(__name__)


def _parse_datetime(value: Any) -> datetime:
    """
    EDGE-TF's gateway serializes timestamps as JSON strings; SQLAlchemy's
    DateTime column (unlike Pydantic) does not parse them itself. Accepts a
    datetime unchanged so this stays a no-op for anything already parsed
    upstream (e.g. by a Pydantic model in a future caller).
    """
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


class ExternalSignalStatus:
    PENDING = "PENDING"      # polled, not yet loaded into an order ticket
    CLAIMED = "CLAIMED"      # a human triggered execute; upstream claim succeeded, broker call in flight
    EXECUTED = "EXECUTED"    # broker execution completed (any terminal OrderRecord status) and reported upstream
    FAILED = "FAILED"        # upstream claim was refused, or local execution errored before completion
    DISMISSED = "DISMISSED"  # reviewed and declined, never loaded


class ExternalSignalRecord(SQLModel, table=True):
    """One trade instruction polled from an external decision engine's gateway."""

    __tablename__ = "external_signals"

    id: Optional[int] = Field(default=None, primary_key=True)

    # Provenance
    source: str = Field(index=True)  # "edge-tf" today
    external_trade_id: str = Field(index=True, unique=True)  # EDGE-TF's trade_id; becomes this system's decision_id if loaded
    instruction_id: str  # binds the approved economics at poll time; a re-approval upstream yields a new one

    # What EDGE-TF approved
    symbol: str
    side: str  # "BUY" | "SELL"
    quantity: float
    order_type: str  # "LIMIT" | "MARKET"
    limit_price: Optional[float] = None
    estimated_notional: float = 0.0
    currency: str = "USD"

    # Audit / rationale, carried through unchanged for display
    thesis_id: str
    strategy_module: str
    rationale: Optional[str] = None

    # Needed to report back accurately and to know when a stale instruction should be re-polled
    intent_hash: str
    approved_fingerprint: str
    approval_expires_at: datetime
    idempotency_key: str

    status: str = Field(default=ExternalSignalStatus.PENDING, index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "external_trade_id": self.external_trade_id,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "order_type": self.order_type,
            "limit_price": self.limit_price,
            "estimated_notional": self.estimated_notional,
            "currency": self.currency,
            "thesis_id": self.thesis_id,
            "strategy_module": self.strategy_module,
            "rationale": self.rationale,
            "approval_expires_at": self.approval_expires_at,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def to_trade_proposal_dict(self, *, account: str, agent_id: str = "default") -> dict[str, Any]:
        """
        Shapes this signal as a TradeProposal payload (src/models/orders.py)
        ready for POST /v1/orders/preview. `decision_id` is set to the
        upstream trade_id on purpose — that's the join key
        claim_upstream/report_upstream use at execute time to find this
        record again.

        Only whole-share quantities are supported today (TradeProposal.quantity
        is int); a fractional upstream quantity is rounded to the nearest
        whole share rather than rejected outright, since EDGE-TF's own
        quantity is itself a planning estimate, not a broker-precise figure.
        """
        proposal: dict[str, Any] = {
            "decision_id": self.external_trade_id,
            "agent_id": agent_id,
            "account": account,
            "symbol": self.symbol,
            "asset_type": "EQUITY",
            "instruction": self.side,
            "quantity": max(1, round(self.quantity)),
            "order_type": self.order_type,
            "strategy_id": f"{self.source}:{self.strategy_module}",
        }
        if self.limit_price is not None:
            proposal["limit_price"] = str(Decimal(str(self.limit_price)))
        return proposal


class ExternalSignalService:
    def __init__(self, session: Session):
        self.session = session

    def record_if_new(self, source: str, instruction: dict[str, Any]) -> Optional[ExternalSignalRecord]:
        """
        Persists one polled ExecutionInstruction unless its trade_id is
        already known — the connector polls on a timer, and without this a
        still-unclaimed upstream trade would spawn a duplicate row every
        poll interval.
        """
        trade_id = instruction["trade_id"]
        existing = self.session.exec(
            select(ExternalSignalRecord).where(ExternalSignalRecord.external_trade_id == trade_id)
        ).first()
        if existing is not None:
            return None

        record = ExternalSignalRecord(
            source=source,
            external_trade_id=trade_id,
            instruction_id=instruction["instruction_id"],
            symbol=instruction["symbol"],
            side=instruction["side"],
            quantity=instruction["quantity"],
            order_type=instruction.get("order_type", "LIMIT"),
            limit_price=instruction.get("limit_price"),
            estimated_notional=instruction.get("estimated_notional", 0.0),
            currency=instruction.get("currency", "USD"),
            thesis_id=instruction["thesis_id"],
            strategy_module=instruction["strategy_module"],
            rationale=instruction.get("rationale"),
            intent_hash=instruction["intent_hash"],
            approved_fingerprint=instruction["approved_fingerprint"],
            approval_expires_at=_parse_datetime(instruction["approval_expires_at"]),
            idempotency_key=instruction["idempotency_key"],
        )
        self.session.add(record)
        self.session.commit()
        self.session.refresh(record)
        logger.info(
            "external_signal_recorded",
            source=source,
            trade_id=trade_id,
            symbol=record.symbol,
            side=record.side,
        )
        return record

    def list_signals(self, status: Optional[str] = None, limit: int = 50) -> list[ExternalSignalRecord]:
        stmt = select(ExternalSignalRecord)
        if status:
            stmt = stmt.where(ExternalSignalRecord.status == status)
        stmt = stmt.order_by(ExternalSignalRecord.created_at.desc()).limit(max(1, min(limit, 200)))
        return list(self.session.exec(stmt).all())

    def get_by_trade_id(self, trade_id: str) -> Optional[ExternalSignalRecord]:
        return self.session.exec(
            select(ExternalSignalRecord).where(ExternalSignalRecord.external_trade_id == trade_id)
        ).first()

    def dismiss(self, signal_id: int) -> ExternalSignalRecord:
        record = self.session.get(ExternalSignalRecord, signal_id)
        if record is None:
            raise ValueError(f"External signal {signal_id} not found")
        record.status = ExternalSignalStatus.DISMISSED
        record.updated_at = datetime.utcnow()
        self.session.add(record)
        self.session.commit()
        self.session.refresh(record)
        return record

    def mark_status(self, trade_id: str, status: str) -> Optional[ExternalSignalRecord]:
        record = self.get_by_trade_id(trade_id)
        if record is None:
            return None
        record.status = status
        record.updated_at = datetime.utcnow()
        self.session.add(record)
        self.session.commit()
        self.session.refresh(record)
        return record


__all__ = ["ExternalSignalRecord", "ExternalSignalService", "ExternalSignalStatus"]
