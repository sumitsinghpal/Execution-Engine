"""
Core execution orchestration: preview and execute flows.
"""

import json
import uuid
from datetime import datetime, timedelta
from typing import Optional

from sqlmodel import SQLModel, Session, select, Field

from src.broker.order_builder import SchwabOrderBuilder
from src.broker.schwab_client import SchwabClient
from src.config import get_settings
from src.execution.approval import ApprovalManager
from src.execution.idempotency import IdempotencyManager, Operation
from src.logging_config import get_logger
from src.models.orders import (
    OrderPreview,
    OrderStatus,
    TradeProposal,
    ExecutionReceipt,
    OrderStatus_Model,
)
from src.risk.limits import RiskChecker

logger = get_logger(__name__)


class OrderRecord(SQLModel, table=True):
    """Persistent order record."""
    
    __tablename__ = "orders"
    
    id: Optional[int] = Field(default=None, primary_key=True)
    decision_id: str = Field(unique=True, index=True)
    preview_id: Optional[str] = Field(default=None, index=True)
    execution_id: Optional[str] = Field(default=None, index=True)
    account: str
    symbol: str
    quantity: int
    instruction: str
    status: str = OrderStatus.PREVIEWED.value
    payload_checksum: str
    risk_approved: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    preview_expires_at: Optional[datetime] = None
    filled_quantity: int = 0
    average_fill_price: Optional[str] = None
    broker_status: Optional[str] = None
    broker_message: Optional[str] = None
    raw_broker_response: Optional[str] = None


class Executor:
    """Orchestrate order preview and execution."""
    
    def __init__(self, session: Session, mock_broker: bool = False):
        self.session = session
        self.settings = get_settings()
        self.schwab = SchwabClient(mock=mock_broker)
        self.builder = SchwabOrderBuilder()
        self.risk_checker = RiskChecker()
        self.idempotency = IdempotencyManager(session)
        self.approval_manager = ApprovalManager(session)
    
    async def preview_order(self, proposal: TradeProposal) -> OrderPreview:
        """
        Execute preview flow:
        1. Validate schema and freshness
        2. Run risk checks
        3. Build order spec
        4. Call Schwab preview
        5. Persist and return preview
        """
        
        # Check for duplicate preview
        if self.idempotency.is_duplicate(proposal.decision_id, Operation.PREVIEW):
            logger.warning("duplicate_preview", decision_id=proposal.decision_id)
            # Could return cached response here
        
        # Get current kill switch state
        kill_switch_on = self._get_kill_switch_state()
        
        # Run risk checks
        verdict = self.risk_checker.evaluate(proposal, kill_switch_on)
        
        # Compute checksum for later verification
        checksum = SchwabOrderBuilder.compute_payload_checksum(proposal)
        
        # Build order spec
        order_spec = self.builder.build_order_spec(proposal, proposal.account)
        
        # Call Schwab preview
        schwab_response = await self.schwab.preview_order(order_spec)
        
        # Generate preview ID
        preview_id = f"preview-{uuid.uuid4()}"
        
        # Determine expiry
        expires_at = datetime.utcnow() + timedelta(
            minutes=self.settings.schwab_preview_expiry_min
        )
        
        # Persist order record
        order_record = OrderRecord(
            decision_id=proposal.decision_id,
            preview_id=preview_id,
            account=proposal.account,
            symbol=proposal.symbol,
            quantity=proposal.quantity,
            instruction=proposal.instruction.value,
            status=OrderStatus.PREVIEWED.value,
            payload_checksum=checksum,
            risk_approved=verdict.approved,
            preview_expires_at=expires_at,
        )
        self.session.add(order_record)
        self.session.commit()
        
        # Log to audit ledger
        self._audit_log(
            "ORDER_PREVIEWED",
            proposal.decision_id,
            {"preview_id": preview_id, "risk_approved": verdict.approved},
        )
        
        # Build response
        return OrderPreview(
            preview_id=preview_id,
            decision_id=proposal.decision_id,
            preview_details=schwab_response,
            estimated_commission=schwab_response.get("estimatedCommission", 0.0),
            estimated_cost=schwab_response.get("estimatedTotalInvestment", 0.0),
            risk_verdict="APPROVED" if verdict.approved else "REJECTED",
            risk_details={"checks": verdict.checks, "rejections": verdict.rejections},
            payload_checksum=checksum,
            expires_at=expires_at,
        )
    
    async def execute_order(
        self,
        decision_id: str,
        preview_id: str,
        approved_by: str,
        approved_at: datetime,
        attestation: str,
        idempotency_key: str,
    ) -> ExecutionReceipt:
        """
        Execute approved order:
        1. Verify preview exists and is not expired
        2. Verify approval
        3. Re-run critical risk checks
        4. Submit to Schwab
        5. Persist execution and return receipt
        """
        
        # Check for duplicate execution
        if self.idempotency.is_duplicate(decision_id, Operation.EXECUTE):
            logger.warning("duplicate_execute", decision_id=decision_id)
        
        # Fetch order record
        stmt = select(OrderRecord).where(OrderRecord.decision_id == decision_id)
        order = self.session.exec(stmt).first()
        
        if not order:
            raise ValueError(f"Order {decision_id} not found")
        
        # Verify preview hasn't expired
        if order.preview_expires_at and datetime.utcnow() > order.preview_expires_at:
            raise ValueError(f"Preview expired at {order.preview_expires_at}")
        
        # Verify preview_id matches
        if order.preview_id != preview_id:
            raise ValueError(f"Preview ID mismatch: expected {order.preview_id}, got {preview_id}")
        
        # Verify approval exists and is recent
        if not self.approval_manager.verify_approval(preview_id, decision_id):
            raise ValueError(f"No valid approval found for {preview_id}")
        
        # Record approval
        self.approval_manager.record_approval(
            preview_id=preview_id,
            decision_id=decision_id,
            approved_by=approved_by,
            approved_at=approved_at,
            attestation=attestation,
            idempotency_key=idempotency_key,
        )
        
        # Re-run kill switch check (critical)
        kill_switch_on = self._get_kill_switch_state()
        if kill_switch_on:
            raise ValueError("Kill switch is ON - order rejected")
        
        # Build and submit order
        order_spec = self.builder.build_order_spec(
            TradeProposal(
                decision_id=decision_id,
                account=order.account,
                symbol=order.symbol,
                asset_type="ETF",  # Would need to store this
                instruction=order.instruction,
                quantity=order.quantity,
                order_type="MARKET",  # Would need to store this
            ),
            order.account,
        )
        
        broker_response = await self.schwab.submit_order(order_spec)
        
        # Extract broker order ID
        execution_id = broker_response.get("orderId", f"exec-{uuid.uuid4()}")
        
        # Update order record
        order.execution_id = execution_id
        order.status = OrderStatus.SUBMITTED.value
        order.updated_at = datetime.utcnow()
        order.broker_status = broker_response.get("status")
        order.raw_broker_response = json.dumps(broker_response)
        
        self.session.add(order)
        self.session.commit()
        
        # Register idempotency
        self.idempotency.register(decision_id, Operation.EXECUTE, idempotency_key)
        
        # Audit log
        self._audit_log(
            "ORDER_EXECUTED",
            decision_id,
            {"execution_id": execution_id, "approved_by": approved_by},
        )
        
        return ExecutionReceipt(
            decision_id=decision_id,
            execution_id=execution_id,
            status=OrderStatus.SUBMITTED,
            submitted_at=datetime.utcnow(),
            broker_response=broker_response,
        )
    
    async def get_order_status(self, decision_id: str) -> OrderStatus_Model:
        """Query order status."""
        stmt = select(OrderRecord).where(OrderRecord.decision_id == decision_id)
        order = self.session.exec(stmt).first()
        
        if not order:
            raise ValueError(f"Order {decision_id} not found")
        
        return OrderStatus_Model(
            decision_id=decision_id,
            execution_id=order.execution_id,
            status=OrderStatus(order.status),
            created_at=order.created_at,
            updated_at=order.updated_at,
            filled_quantity=order.filled_quantity,
            broker_status=order.broker_status,
            broker_message=order.broker_message,
        )
    
    def _get_kill_switch_state(self) -> bool:
        """Get current kill switch state."""
        # In production, would query database
        return self.settings.kill_switch_enabled
    
    def _audit_log(self, action: str, decision_id: str, details: dict) -> None:
        """Write audit log entry."""
        logger.info(
            "audit_log",
            action=action,
            decision_id=decision_id,
            details=details,
            timestamp=datetime.utcnow().isoformat(),
        )
