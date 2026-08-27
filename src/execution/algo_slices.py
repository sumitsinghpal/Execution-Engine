"""
TWAP/VWAP execution: a large order isn't sent to the broker as one order —
it's split into several child MARKET slices submitted over time, which is
what these order types actually mean. This module builds the slice plan
and runs it in the background after Executor.execute_order() has already
validated approval and the kill switch once for the whole parent order.

Safety property this exists to preserve: a multi-minute TWAP/VWAP can span
a kill-switch halt that happens mid-flight. The one-time check in
execute_order() is not enough — this loop re-checks the kill switch before
EVERY slice and stops submitting the moment it's on, so "halt trading"
still means what it says even for an order that's still executing.

This runs after the original HTTP request has already returned (the
request can't block for the whole execution window), so it cannot reuse
that request's DB session — every slice opens and closes its own.
"""

import asyncio
from datetime import datetime
from typing import Any, Callable, Optional

from sqlmodel import Field, Session, SQLModel, select

from src.brokers.factory import build_broker_adapter
from src.config import get_settings
from src.database import SessionLocal
from src.execution.kill_switch_state import KillSwitchService
from src.logging_config import get_logger
from src.models.orders import OrderStatus

logger = get_logger(__name__)

# Keeps fire-and-forget asyncio tasks alive until they finish — without
# this, nothing holds a reference to a task created with
# asyncio.create_task() and it can be garbage-collected mid-execution.
_background_tasks: set[asyncio.Task] = set()


class AlgoSliceRecord(SQLModel, table=True):
    """One child order of a TWAP/VWAP parent — the actual audit trail of what was submitted, when, and why not if it wasn't."""

    __tablename__ = "algo_slices"

    id: Optional[int] = Field(default=None, primary_key=True)
    parent_decision_id: str = Field(index=True)
    slice_index: int
    quantity: int
    status: str = "PENDING"  # PENDING, SUBMITTED, FAILED, SKIPPED_KILL_SWITCH
    broker_order_id: Optional[str] = None
    error: Optional[str] = None
    submitted_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "slice_index": self.slice_index,
            "quantity": self.quantity,
            "status": self.status,
            "broker_order_id": self.broker_order_id,
            "error": self.error,
            "submitted_at": self.submitted_at,
        }


def build_twap_plan(total_quantity: int, slices: int) -> list[int]:
    """Equal-sized slices; any remainder goes to the earliest slices so the plan always sums to total_quantity exactly."""
    slices = max(1, slices)
    base, remainder = divmod(total_quantity, slices)
    return [base + (1 if i < remainder else 0) for i in range(slices)]


def build_vwap_plan(total_quantity: int, slices: int, volume_curve: list[float]) -> list[int]:
    """
    Slices weighted by a historical intraday volume curve (today's 5-min
    bar volumes — see get_price_history) instead of split evenly, so more
    size executes when the market has historically been more liquid.
    Falls back to an even TWAP split if no usable volume curve is
    available, rather than failing the whole order over missing data.
    """
    slices = max(1, slices)
    if not volume_curve or sum(volume_curve) <= 0:
        return build_twap_plan(total_quantity, slices)

    n = len(volume_curve)
    bucket_size = max(n // slices, 1)
    buckets = [sum(volume_curve[i : i + bucket_size]) for i in range(0, n, bucket_size)][:slices]
    while len(buckets) < slices:
        buckets.append(sum(volume_curve) / slices)

    total_weight = sum(buckets) or 1.0
    plan = [round(total_quantity * w / total_weight) for w in buckets]
    plan[0] += total_quantity - sum(plan)  # fix rounding drift so the plan sums exactly
    return plan


async def execute_algo_slices(
    decision_id: str,
    account: str,
    agent_id: str,
    symbol: str,
    asset_type: str,
    instruction: str,
    quantities: list[int],
    interval_seconds: float,
    session_factory: Optional[Callable[[], Session]] = None,
) -> None:
    """
    The background execution loop — see module docstring for the
    kill-switch and session-lifetime reasoning. session_factory defaults
    to the real SessionLocal (production use); tests inject the isolated
    test-DB session factory instead, the same pattern
    src/execution/strategy_scanner.py already uses.
    """
    from src.execution.executor import OrderRecord  # deferred: avoid circular import

    session_factory = session_factory or SessionLocal

    total_submitted = 0
    halted = False

    for i, quantity in enumerate(quantities):
        if i > 0:
            await asyncio.sleep(interval_seconds)
        if quantity <= 0:
            continue

        session = session_factory()
        try:
            if KillSwitchService(session).is_halted(agent_id):
                session.add(
                    AlgoSliceRecord(
                        parent_decision_id=decision_id, slice_index=i, quantity=quantity, status="SKIPPED_KILL_SWITCH"
                    )
                )
                session.commit()
                halted = True
                logger.critical("algo_slice_skipped_kill_switch", decision_id=decision_id, slice_index=i)
                continue

            slice_record = AlgoSliceRecord(parent_decision_id=decision_id, slice_index=i, quantity=quantity)
            try:
                settings = get_settings()
                broker = build_broker_adapter(settings)
                profile = settings.get_account_profile(account)
                order_spec = {
                    "orderId": f"{decision_id}-slice-{i}",
                    "accountId": account,
                    "symbol": symbol,
                    "assetType": asset_type,
                    "quantity": quantity,
                    "instruction": instruction,
                    "orderType": "MARKET",
                }
                response = await broker.submit_order(profile, order_spec)
                slice_record.status = "SUBMITTED"
                slice_record.broker_order_id = response.get("orderId")
                slice_record.submitted_at = datetime.utcnow()
                total_submitted += quantity
            except Exception as exc:
                slice_record.status = "FAILED"
                slice_record.error = str(exc)
                logger.error("algo_slice_failed", decision_id=decision_id, slice_index=i, error=str(exc))
            session.add(slice_record)
            session.commit()

            order = session.exec(select(OrderRecord).where(OrderRecord.decision_id == decision_id)).first()
            if order is not None and slice_record.status == "SUBMITTED":
                order.filled_quantity = (order.filled_quantity or 0) + quantity
                order.updated_at = datetime.utcnow()
                session.add(order)
                session.commit()
        finally:
            session.close()

    session = session_factory()
    try:
        order = session.exec(select(OrderRecord).where(OrderRecord.decision_id == decision_id)).first()
        if order is not None:
            total_planned = sum(quantities)
            if halted and total_submitted == 0:
                order.status = OrderStatus.CANCELED.value
            elif total_submitted >= total_planned and total_planned > 0:
                order.status = OrderStatus.FILLED.value
            elif total_submitted > 0:
                order.status = OrderStatus.PARTIAL_FILL.value
            else:
                order.status = OrderStatus.FAILED.value
            order.updated_at = datetime.utcnow()
            session.add(order)
            session.commit()
        logger.info(
            "algo_execution_complete",
            decision_id=decision_id,
            total_submitted=total_submitted,
            total_planned=sum(quantities),
            halted=halted,
        )
    finally:
        session.close()


def schedule_algo_execution(**kwargs: Any) -> asyncio.Task:
    """Fires the slicing loop as a background task that outlives the current request."""
    task = asyncio.create_task(execute_algo_slices(**kwargs))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


__all__ = [
    "AlgoSliceRecord",
    "build_twap_plan",
    "build_vwap_plan",
    "execute_algo_slices",
    "schedule_algo_execution",
]
