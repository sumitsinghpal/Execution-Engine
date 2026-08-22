"""
Idempotency control: prevent duplicate order submissions.
Keyed by (decision_id, operation).
"""

from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

from sqlmodel import SQLModel, Session, select, Field

from src.logging_config import get_logger

logger = get_logger(__name__)


class Operation(str, Enum):
    """Idempotency operation types."""
    PREVIEW = "PREVIEW"
    EXECUTE = "EXECUTE"


class IdempotencyRecord(SQLModel, table=True):
    """Idempotency table to prevent duplicate operations."""
    
    __tablename__ = "idempotency_records"
    
    idempotency_key: str = Field(primary_key=True)
    decision_id: str
    operation: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: datetime
    response_body: Optional[str] = None


class IdempotencyManager:
    """Manage idempotency records."""
    
    EXPIRY_MINUTES = 1440  # 24 hours
    
    def __init__(self, session: Session):
        self.session = session
    
    def is_duplicate(self, decision_id: str, operation: Operation) -> bool:
        """Check if operation already exists for this decision_id."""
        stmt = select(IdempotencyRecord).where(
            (IdempotencyRecord.decision_id == decision_id)
            & (IdempotencyRecord.operation == operation.value)
            & (IdempotencyRecord.expires_at > datetime.utcnow())
        )
        existing = self.session.exec(stmt).first()
        return existing is not None
    
    def register(
        self,
        decision_id: str,
        operation: Operation,
        idempotency_key: str,
        response_body: Optional[str] = None,
    ) -> IdempotencyRecord:
        """Register an operation for idempotency."""
        expires_at = datetime.utcnow() + timedelta(minutes=self.EXPIRY_MINUTES)
        
        record = IdempotencyRecord(
            decision_id=decision_id,
            operation=operation.value,
            idempotency_key=idempotency_key,
            expires_at=expires_at,
            response_body=response_body,
        )
        
        self.session.add(record)
        self.session.commit()
        self.session.refresh(record)
        
        logger.info(
            "idempotency_registered",
            decision_id=decision_id,
            operation=operation.value,
            idempotency_key=idempotency_key,
        )
        
        return record
    
    def get_existing_response(self, decision_id: str, operation: Operation) -> Optional[str]:
        """Get cached response for duplicate request."""
        stmt = select(IdempotencyRecord).where(
            (IdempotencyRecord.decision_id == decision_id)
            & (IdempotencyRecord.operation == operation.value)
            & (IdempotencyRecord.expires_at > datetime.utcnow())
        )
        existing = self.session.exec(stmt).first()
        return existing.response_body if existing else None
