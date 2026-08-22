from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import JSON, Column, String, UniqueConstraint
from sqlmodel import Field as SQLField
from sqlmodel import SQLModel


class AssetType(str, Enum):
    ETF = "ETF"
    STOCK = "STOCK"


class Instruction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderStatus(str, Enum):
    PREVIEWED = "PREVIEWED"
    APPROVED = "APPROVED"
    SUBMITTED = "SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIAL_FILL = "PARTIAL_FILL"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"


class IdempotencyOperation(str, Enum):
    PREVIEW = "PREVIEW"
    EXECUTE = "EXECUTE"


class TradeProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    decision_id: str = Field(pattern=r"^edge-\d{8}-\d{3,}$")
    account: str = Field(min_length=1)
    symbol: str = Field(min_length=1, max_length=10)
    asset_type: AssetType
    instruction: Instruction
    quantity: int = Field(gt=0)
    order_type: OrderType
    limit_price: Decimal | None = None

    @model_validator(mode="after")
    def validate_by_order_type(self) -> TradeProposal:
        if self.order_type == OrderType.LIMIT and self.limit_price is None:
            raise ValueError("limit_price is required for LIMIT orders")
        if self.order_type == OrderType.MARKET and self.limit_price is not None:
            raise ValueError("limit_price is not allowed for MARKET orders")
        return self


class ApprovalArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    approved_by: str = Field(min_length=3)
    approved_at: datetime
    attestation: str = Field(min_length=10)
    signature: str | None = None


class ExecuteOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    decision_id: str = Field(pattern=r"^edge-\d{8}-\d{3,}$")
    preview_id: str
    approval: ApprovalArtifact
    idempotency_key: str = Field(min_length=8, max_length=128)


class TradeProposalRecord(SQLModel, table=True):
    __tablename__ = "trade_proposals"

    id: str = SQLField(default_factory=lambda: str(uuid4()), primary_key=True)
    decision_id: str = SQLField(sa_column=Column(String, unique=True, nullable=False))
    account: str
    symbol: str
    asset_type: str
    instruction: str
    quantity: int
    order_type: str
    limit_price: str | None = None
    created_at: datetime = SQLField(default_factory=lambda: datetime.now(UTC))


class PreviewRecord(SQLModel, table=True):
    __tablename__ = "previews"

    id: str = SQLField(default_factory=lambda: str(uuid4()), primary_key=True)
    decision_id: str = SQLField(index=True)
    checksum: str
    expires_at: datetime
    risk_verdict: dict[str, Any] = SQLField(sa_column=Column(JSON, nullable=False))
    normalized_order: dict[str, Any] = SQLField(sa_column=Column(JSON, nullable=False))
    broker_preview: dict[str, Any] = SQLField(sa_column=Column(JSON, nullable=False))
    status: str = SQLField(default=OrderStatus.PREVIEWED.value)
    created_at: datetime = SQLField(default_factory=lambda: datetime.now(UTC))


class ApprovalRecord(SQLModel, table=True):
    __tablename__ = "approvals"

    id: str = SQLField(default_factory=lambda: str(uuid4()), primary_key=True)
    decision_id: str = SQLField(index=True)
    preview_id: str = SQLField(index=True)
    approved_by: str
    approved_at: datetime
    attestation: str
    signature: str | None = None
    created_at: datetime = SQLField(default_factory=lambda: datetime.now(UTC))


class ExecutionOrderRecord(SQLModel, table=True):
    __tablename__ = "executions"

    id: str = SQLField(default_factory=lambda: str(uuid4()), primary_key=True)
    decision_id: str = SQLField(sa_column=Column(String, unique=True, nullable=False))
    preview_id: str
    broker_order_id: str | None = None
    status: str = SQLField(default=OrderStatus.SUBMITTED.value)
    broker_response: dict[str, Any] = SQLField(sa_column=Column(JSON, nullable=False))
    created_at: datetime = SQLField(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = SQLField(default_factory=lambda: datetime.now(UTC))


class ReconciliationEventRecord(SQLModel, table=True):
    __tablename__ = "reconciliation_events"

    id: str = SQLField(default_factory=lambda: str(uuid4()), primary_key=True)
    decision_id: str = SQLField(index=True)
    status_before: str
    status_after: str
    correlation_id: str
    broker_response: dict[str, Any] = SQLField(sa_column=Column(JSON, nullable=False))
    created_at: datetime = SQLField(default_factory=lambda: datetime.now(UTC))


class IdempotencyKeyRecord(SQLModel, table=True):
    __tablename__ = "idempotency_keys"
    __table_args__ = (UniqueConstraint("decision_id", "operation", name="uq_decision_operation"),)

    id: str = SQLField(default_factory=lambda: str(uuid4()), primary_key=True)
    decision_id: str = SQLField(index=True)
    operation: str
    idempotency_key: str
    response_json: dict[str, Any] = SQLField(sa_column=Column(JSON, nullable=False))
    created_at: datetime = SQLField(default_factory=lambda: datetime.now(UTC))


class KillSwitchState(SQLModel, table=True):
    __tablename__ = "kill_switch"

    id: int = SQLField(default=1, primary_key=True)
    is_on: bool = False
    updated_at: datetime = SQLField(default_factory=lambda: datetime.now(UTC))


class AuditLedgerEntry(SQLModel, table=True):
    __tablename__ = "audit_ledger"

    id: str = SQLField(default_factory=lambda: str(uuid4()), primary_key=True)
    timestamp: datetime = SQLField(default_factory=lambda: datetime.now(UTC), index=True)
    actor: str
    action: str
    decision_id: str | None = None
    payload_hash: str
    before_status: str | None = None
    after_status: str | None = None
    correlation_id: str
    details: dict[str, Any] = SQLField(sa_column=Column(JSON, nullable=False))
