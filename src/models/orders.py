"""
Core data models and domain objects for the execution engine.

This module defines all Pydantic models for API contracts and internal data structures.
All price/quantity handling uses Decimal to avoid floating-point precision issues.
"""

import re
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional, Any

from pydantic import BaseModel, Field, field_validator, model_validator, ConfigDict, field_serializer


class AssetType(str, Enum):
    """Allowed asset types."""
    EQUITY = "EQUITY"
    ETF = "ETF"
    OPTION = "OPTION"
    BOND = "BOND"


class Instruction(str, Enum):
    """Trade instructions."""
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    """Order types supported by execution engine."""
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"


class OrderStatus(str, Enum):
    """Order state machine."""
    PREVIEWED = "PREVIEWED"
    APPROVED = "APPROVED"
    SUBMITTED = "SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIAL_FILL = "PARTIAL_FILL"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


class TradeProposal(BaseModel):
    """
    Incoming trade instruction from EDGE-TF.
    
    All fields are required and strictly validated.
    Unknown fields are rejected (model_config.extra = 'forbid').
    """
    decision_id: str = Field(..., description="Unique decision ID from EDGE-TF (e.g., 'edge-20260821-001')")
    agent_id: str = Field(
        default="default",
        description=(
            "Identifier of the originating agent, for deployments running multiple "
            "coordinating agents (e.g. distinct strategies/asset classes). Defaults "
            "to 'default' for single-agent callers. Each agent can be halted "
            "independently via /v1/kill-switch/agents/{agent_id} without affecting "
            "any other agent, in addition to the fleet-wide kill switch."
        ),
    )
    account: str = Field(..., description="Target account identifier")
    symbol: str = Field(..., pattern="^[A-Z]{1,5}$", description="Trading symbol (uppercase, max 5 chars)")
    asset_type: AssetType = Field(..., description="Type of asset being traded")
    instruction: Instruction = Field(..., description="BUY or SELL")
    quantity: int = Field(..., gt=0, description="Quantity to trade (positive integer)")
    order_type: OrderType = Field(..., description="Order type (MARKET, LIMIT, STOP, STOP_LIMIT)")
    limit_price: Optional[Decimal] = Field(default=None, description="Limit price (required for LIMIT orders)")
    stop_price: Optional[Decimal] = Field(default=None, description="Stop price (for STOP/STOP_LIMIT)")

    # Advisory risk-management figures from src/strategy — informational
    # only. Neither is enforced as a broker-side bracket/OCO order (this
    # system doesn't build those); they ride along for display and the
    # audit trail so a signal-sourced order's intended plan is visible
    # wherever the order itself is visible.
    strategy_id: Optional[str] = Field(default=None, description="Strategy that generated this proposal, if any")
    strategy_stop_loss_price: Optional[Decimal] = Field(default=None, description="Advisory stop-loss level")
    strategy_take_profit_price: Optional[Decimal] = Field(default=None, description="Advisory take-profit level")

    model_config = ConfigDict(extra="forbid")  # Reject unknown fields

    @field_serializer("limit_price", "stop_price", "strategy_stop_loss_price", "strategy_take_profit_price", when_used="json")
    def serialize_decimal(self, value: Optional[Decimal]) -> Optional[str]:
        """Convert Decimal to string for JSON serialization."""
        return str(value) if value is not None else None
    
    @field_validator("symbol")
    @classmethod
    def validate_symbol_uppercase(cls, v: str) -> str:
        if not v.isupper():
            raise ValueError("Symbol must be uppercase")
        return v

    @field_validator("agent_id")
    @classmethod
    def validate_agent_id(cls, v: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", v):
            raise ValueError("agent_id must be 1-64 characters of letters, digits, '-', or '_'")
        if v == "__global__":
            raise ValueError("agent_id '__global__' is reserved for the fleet-wide kill switch scope")
        return v
    
    @model_validator(mode="after")
    def validate_order_type_prices(self) -> "TradeProposal":
        """Enforce price requirements for different order types."""
        if self.order_type == OrderType.LIMIT:
            if self.limit_price is None:
                raise ValueError("limit_price is required for LIMIT orders")
        elif self.order_type == OrderType.STOP:
            if self.stop_price is None:
                raise ValueError("stop_price is required for STOP orders")
        elif self.order_type == OrderType.STOP_LIMIT:
            if self.limit_price is None:
                raise ValueError("limit_price is required for STOP_LIMIT orders")
            if self.stop_price is None:
                raise ValueError("stop_price is required for STOP_LIMIT orders")
        return self


class OrderPreview(BaseModel):
    """Response from order preview endpoint."""
    preview_id: str = Field(..., description="Unique preview identifier")
    decision_id: str = Field(..., description="Echoed decision_id from request")
    preview_details: dict = Field(..., description="Raw Schwab preview response")
    estimated_commission: Decimal = Field(..., decimal_places=2, description="Estimated commission")
    estimated_cost: Decimal = Field(..., decimal_places=2, description="Estimated total cost/proceeds")
    risk_verdict: str = Field(..., description="APPROVED or REJECTED")
    risk_details: dict = Field(..., description="Risk check results")
    payload_checksum: str = Field(..., description="SHA256 checksum of normalized trade proposal")
    expires_at: datetime = Field(..., description="Expiration time for this preview")


class ApprovalArtifact(BaseModel):
    """Approval information required to execute."""
    preview_id: str = Field(..., description="Must match the preview_id from preview response")
    approved_by: str = Field(..., description="User/system identifier granting approval")
    approved_at: datetime = Field(..., description="Approval timestamp")
    attestation: str = Field(..., description="Human-readable or signature attestation")
    idempotency_key: str = Field(..., description="Idempotency key for execution")


class ExecutionRequest(BaseModel):
    """Request to execute a previously-approved order."""
    decision_id: str = Field(..., description="Original decision_id")
    preview_id: str = Field(..., description="Must reference an active preview")
    approval: ApprovalArtifact = Field(..., description="Approval artifact")
    
    model_config = {"extra": "forbid"}


class ExecutionReceipt(BaseModel):
    """Response after successful order submission."""
    decision_id: str = Field(..., description="Original decision ID")
    execution_id: str = Field(..., description="Broker order ID")
    status: OrderStatus = Field(..., description="Current order status")
    submitted_at: datetime = Field(..., description="Submission timestamp")
    broker_response: dict = Field(..., description="Raw Schwab response")


class OrderStatus_Model(BaseModel):
    """Status query response."""
    decision_id: str
    agent_id: str = "default"
    execution_id: Optional[str]
    status: OrderStatus
    created_at: datetime
    updated_at: datetime
    filled_quantity: int = 0
    average_fill_price: Optional[Decimal] = None
    broker_status: Optional[str] = None
    broker_message: Optional[str] = None


class KillSwitchStatus(BaseModel):
    """Kill switch state."""
    enabled: bool = Field(..., description="True = trading disabled, False = trading enabled")
    set_by: str = Field(..., description="User/system that toggled the switch")
    set_at: datetime = Field(..., description="Timestamp of last change")
    reason: Optional[str] = Field(default=None, description="Reason for state change")


class HealthStatus(BaseModel):
    """Service health check response."""
    status: str = Field(..., enum=["healthy", "degraded", "unhealthy"])
    timestamp: datetime
    database: str = Field(..., enum=["ok", "error"])
    broker_connectivity: str = Field(..., enum=["ok", "error", "untested"])
    details: Optional[dict] = None
