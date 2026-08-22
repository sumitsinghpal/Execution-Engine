"""
Reconciliation service: poll broker for order status updates.
Updates local order state machine deterministically.
"""

from datetime import datetime
from typing import Optional

from sqlmodel import SQLModel, Field, Session, select

from src.brokers.base import BrokerAdapter
from src.brokers.paper import PaperBrokerAdapter
from src.config import get_settings
from src.logging_config import get_logger
from src.models.orders import OrderStatus

logger = get_logger(__name__)


class ReconciliationEvent(SQLModel, table=True):
    """Record of reconciliation checks and status updates."""
    
    __tablename__ = "reconciliation_events"
    
    id: Optional[int] = Field(default=None, primary_key=True)
    decision_id: str = Field(index=True)
    execution_id: str = Field(index=True)
    old_status: str
    new_status: str
    broker_status: str
    filled_quantity: int = 0
    average_fill_price: Optional[str] = None
    broker_response_raw: str
    checked_at: datetime = Field(default_factory=datetime.utcnow)


class ReconciliationService:
    """Reconcile broker order status with local records."""
    
    # State machine: valid transitions
    VALID_TRANSITIONS = {
        OrderStatus.PREVIEWED: [OrderStatus.APPROVED, OrderStatus.REJECTED],
        OrderStatus.APPROVED: [OrderStatus.SUBMITTED, OrderStatus.REJECTED],
        OrderStatus.SUBMITTED: [
            OrderStatus.ACKNOWLEDGED,
            OrderStatus.PARTIAL_FILL,
            OrderStatus.REJECTED,
            OrderStatus.FAILED,
        ],
        OrderStatus.ACKNOWLEDGED: [
            OrderStatus.PARTIAL_FILL,
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
        ],
        OrderStatus.PARTIAL_FILL: [
            OrderStatus.PARTIAL_FILL,  # Multiple partial fills
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
        ],
        OrderStatus.FILLED: [],  # Terminal
        OrderStatus.CANCELED: [],  # Terminal
        OrderStatus.REJECTED: [],  # Terminal
        OrderStatus.FAILED: [],  # Terminal
    }
    
    def __init__(self, session: Session, broker: Optional[BrokerAdapter] = None):
        self.session = session
        self.settings = get_settings()
        self.broker = broker or PaperBrokerAdapter()
    
    async def reconcile_order(self, decision_id: str, account_alias: str, execution_id: str) -> bool:
        """
        Poll broker for order status and update local record.
        Return True if status changed, False otherwise.
        """
        
        # Get current broker status
        profile = self.settings.get_account_profile(account_alias)
        broker_status = await self.broker.get_order_status(profile, execution_id)
        
        logger.info(
            "broker_status_polled",
            decision_id=decision_id,
            execution_id=execution_id,
            broker_status=broker_status.get("status"),
        )
        
        # Map broker status to our status enum
        local_status = self._map_broker_status(broker_status.get("status"))
        
        # Import OrderRecord here to avoid circular imports
        from src.execution.executor import OrderRecord
        
        # Get local order record
        stmt = select(OrderRecord).where(OrderRecord.decision_id == decision_id)
        order = self.session.exec(stmt).first()
        
        if not order:
            logger.warning("order_not_found_during_reconciliation", decision_id=decision_id)
            return False
        
        old_status = OrderStatus(order.status)
        
        # Validate transition
        if local_status not in self.VALID_TRANSITIONS.get(old_status, []):
            logger.error(
                "invalid_status_transition",
                decision_id=decision_id,
                from_status=old_status.value,
                to_status=local_status.value,
            )
            return False
        
        # Status changed
        if old_status != local_status:
            # Record reconciliation event
            event = ReconciliationEvent(
                decision_id=decision_id,
                execution_id=execution_id,
                old_status=old_status.value,
                new_status=local_status.value,
                broker_status=broker_status.get("status", "UNKNOWN"),
                filled_quantity=broker_status.get("filledQuantity", 0),
                average_fill_price=str(broker_status.get("averageFillPrice")) if broker_status.get("averageFillPrice") else None,
                broker_response_raw=str(broker_status),
            )
            self.session.add(event)
            
            # Update order record
            order.status = local_status.value
            order.broker_status = broker_status.get("status")
            order.filled_quantity = broker_status.get("filledQuantity", 0)
            if broker_status.get("averageFillPrice"):
                order.average_fill_price = str(broker_status.get("averageFillPrice"))
            order.updated_at = datetime.utcnow()
            
            self.session.add(order)
            self.session.commit()
            
            logger.info(
                "order_status_updated",
                decision_id=decision_id,
                old_status=old_status.value,
                new_status=local_status.value,
            )
            
            return True
        
        return False
    
    @staticmethod
    def _map_broker_status(broker_status: str) -> OrderStatus:
        """Map Schwab broker status to our OrderStatus enum."""
        mapping = {
            "ACCEPTED": OrderStatus.ACKNOWLEDGED,
            "FILLED": OrderStatus.FILLED,
            "PARTIALLY_FILLED": OrderStatus.PARTIAL_FILL,
            "CANCELED": OrderStatus.CANCELED,
            "REJECTED": OrderStatus.REJECTED,
            "EXPIRED": OrderStatus.REJECTED,
            "FAILED": OrderStatus.FAILED,
        }
        return mapping.get(broker_status, OrderStatus.ACKNOWLEDGED)
