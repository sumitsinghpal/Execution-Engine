from __future__ import annotations

from datetime import UTC, datetime

from sqlmodel import Session

from src.broker.schwab_client import SchwabClient
from src.models.orders import ExecutionOrderRecord, OrderStatus, ReconciliationEventRecord

_ALLOWED_TRANSITIONS = {
    OrderStatus.PREVIEWED.value: {OrderStatus.APPROVED.value},
    OrderStatus.APPROVED.value: {OrderStatus.SUBMITTED.value},
    OrderStatus.SUBMITTED.value: {
        OrderStatus.ACKNOWLEDGED.value,
        OrderStatus.REJECTED.value,
        OrderStatus.CANCELED.value,
    },
    OrderStatus.ACKNOWLEDGED.value: {
        OrderStatus.PARTIAL_FILL.value,
        OrderStatus.FILLED.value,
        OrderStatus.CANCELED.value,
        OrderStatus.REJECTED.value,
    },
    OrderStatus.PARTIAL_FILL.value: {
        OrderStatus.PARTIAL_FILL.value,
        OrderStatus.FILLED.value,
        OrderStatus.CANCELED.value,
    },
    OrderStatus.FILLED.value: set(),
    OrderStatus.CANCELED.value: set(),
    OrderStatus.REJECTED.value: set(),
}


def validate_transition(before_status: str, after_status: str) -> bool:
    return after_status in _ALLOWED_TRANSITIONS.get(before_status, set())


def map_broker_status(status: str) -> str:
    mapping = {
        "ACKNOWLEDGED": OrderStatus.ACKNOWLEDGED.value,
        "PARTIAL_FILL": OrderStatus.PARTIAL_FILL.value,
        "FILLED": OrderStatus.FILLED.value,
        "CANCELED": OrderStatus.CANCELED.value,
        "REJECTED": OrderStatus.REJECTED.value,
    }
    return mapping.get(status.upper(), OrderStatus.ACKNOWLEDGED.value)


async def reconcile_execution(session: Session, client: SchwabClient, order: ExecutionOrderRecord, correlation_id: str) -> str:
    if not order.broker_order_id:
        return order.status
    broker_response = await client.get_order_status(order.broker_order_id)
    next_status = map_broker_status(str(broker_response.get("status", "ACKNOWLEDGED")))
    if validate_transition(order.status, next_status):
        previous = order.status
        order.status = next_status
        order.updated_at = datetime.now(UTC)
        session.add(order)
        session.add(
            ReconciliationEventRecord(
                decision_id=order.decision_id,
                status_before=previous,
                status_after=next_status,
                correlation_id=correlation_id,
                broker_response=broker_response,
            )
        )
    return order.status
