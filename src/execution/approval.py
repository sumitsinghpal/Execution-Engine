"""
Approval workflow: verify human/admin approval before execution.
"""

from datetime import datetime, timedelta
from typing import Optional

from sqlmodel import SQLModel, Session, select, Field

from src.logging_config import get_logger

logger = get_logger(__name__)


class ApprovalRecord(SQLModel, table=True):
    """Record of approvals for audit trail."""
    
    __tablename__ = "approvals"
    
    idempotency_key: str = Field(primary_key=True)
    preview_id: str = Field(index=True)
    decision_id: str = Field(index=True)
    approved_by: str
    approved_at: datetime
    attestation: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ApprovalManager:
    """Manage approval records and verification."""
    
    MAX_APPROVAL_AGE_MINUTES = 30
    
    def __init__(self, session: Session):
        self.session = session
    
    def record_approval(
        self,
        preview_id: str,
        decision_id: str,
        approved_by: str,
        approved_at: datetime,
        attestation: str,
        idempotency_key: str,
    ) -> ApprovalRecord:
        """Record an approval."""
        approval = ApprovalRecord(
            preview_id=preview_id,
            decision_id=decision_id,
            approved_by=approved_by,
            approved_at=approved_at,
            attestation=attestation,
            idempotency_key=idempotency_key,
        )
        
        self.session.add(approval)
        self.session.commit()
        self.session.refresh(approval)
        
        logger.info(
            "approval_recorded",
            preview_id=preview_id,
            decision_id=decision_id,
            approved_by=approved_by,
        )
        
        return approval
    
    def verify_approval(self, preview_id: str, decision_id: str) -> bool:
        """
        Verify that an approval exists for this preview/decision.
        Approvals must be recent (within MAX_APPROVAL_AGE_MINUTES).
        """
        cutoff = datetime.utcnow() - timedelta(minutes=self.MAX_APPROVAL_AGE_MINUTES)
        
        stmt = select(ApprovalRecord).where(
            (ApprovalRecord.preview_id == preview_id)
            & (ApprovalRecord.decision_id == decision_id)
            & (ApprovalRecord.approved_at >= cutoff)
        )
        
        approval = self.session.exec(stmt).first()
        
        if approval:
            logger.info(
                "approval_verified",
                preview_id=preview_id,
                decision_id=decision_id,
                approved_by=approval.approved_by,
            )
        else:
            logger.warning(
                "approval_not_found",
                preview_id=preview_id,
                decision_id=decision_id,
            )
        
        return approval is not None
    
    def get_approval(self, preview_id: str, decision_id: str) -> Optional[ApprovalRecord]:
        """Get approval record if it exists."""
        stmt = select(ApprovalRecord).where(
            (ApprovalRecord.preview_id == preview_id)
            & (ApprovalRecord.decision_id == decision_id)
        )
        return self.session.exec(stmt).first()
