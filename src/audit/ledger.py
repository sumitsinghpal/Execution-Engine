"""
Audit ledger: append-only immutable audit trail.
All significant events are logged with full context.
"""

from datetime import datetime
from typing import Optional

from sqlmodel import SQLModel, Session, Field

from src.logging_config import get_logger

logger = get_logger(__name__)


class AuditLedger(SQLModel, table=True):
    """
    Append-only audit ledger.
    Records all significant events with full forensic context.
    """
    
    __tablename__ = "audit_ledger"
    
    id: Optional[int] = Field(default=None, primary_key=True)
    timestamp: datetime = Field(default_factory=datetime.utcnow, index=True)
    correlation_id: str = Field(index=True)
    decision_id: str = Field(index=True)
    actor: str
    action: str
    resource_type: str
    resource_id: str
    before_state: Optional[str] = None
    after_state: Optional[str] = None
    payload_hash: Optional[str] = None
    result: str = "SUCCESS"
    error_message: Optional[str] = None


class AuditLedgerManager:
    """Manage audit ledger entries."""
    
    def __init__(self, session: Session):
        self.session = session
    
    def log_event(
        self,
        correlation_id: str,
        decision_id: str,
        actor: str,
        action: str,
        resource_type: str,
        resource_id: str,
        before_state: Optional[str] = None,
        after_state: Optional[str] = None,
        payload_hash: Optional[str] = None,
        result: str = "SUCCESS",
        error_message: Optional[str] = None,
    ) -> AuditLedger:
        """
        Log an audit event.
        All parameters except session are required.
        """
        
        entry = AuditLedger(
            timestamp=datetime.utcnow(),
            correlation_id=correlation_id,
            decision_id=decision_id,
            actor=actor,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            before_state=before_state,
            after_state=after_state,
            payload_hash=payload_hash,
            result=result,
            error_message=error_message,
        )
        
        self.session.add(entry)
        self.session.commit()
        
        logger.info(
            "audit_logged",
            correlation_id=correlation_id,
            decision_id=decision_id,
            action=action,
            result=result,
        )
        
        return entry
