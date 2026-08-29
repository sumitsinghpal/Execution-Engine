"""
Bracket / trailing-stop orders — Dhan's "Super Order" pattern: a human
places one entry, then attaches an exit plan (a fixed take-profit and/or
stop-loss, or a trailing stop that ratchets up as the price rises) so the
position closes itself, no further click needed.

Deliberately built as its OWN table plus its OWN background monitoring
loop, layered ON TOP of (never inside) the existing Executor/TradeProposal/
OrderRecord pipeline that every order — manual or autonomous — already
depends on:

1. The entry itself is a completely ordinary order. It still goes through
   the unmodified preview -> execute gate (POST /v1/orders/preview then
   POST /v1/orders/execute) exactly like any other manual order — this
   module changes nothing about how an entry is placed.
2. Only AFTER an entry has been executed does a human "attach" a bracket
   to it (POST /v1/orders/bracket/attach, entry_decision_id + exit levels).
   That call creates a BracketOrderRecord — a separate row in a separate
   table — and does not touch the OrderRecord it references at all.
3. A background loop (manage_bracket_orders, same shape as
   src/execution/autonomous_trader.py's manage_open_positions()) watches
   only OPEN BracketOrderRecords, and when a level is hit, submits the
   exit through Executor.preview_order()/execute_order() like any other
   order — still risk-checked, still kill-switch-gated.

A bug in this feature can therefore never corrupt or bypass order
execution for anything else in the system — worst case, a bracket simply
fails to attach or fails to auto-exit, leaving the underlying position
exactly where a manually-placed order without a bracket already sits
today (open, needing a human to sell it).

Long-only, matching the rest of this system's order support (see
autonomous_trader.py) — the managed exit is always a SELL closing an
existing long, never a short entry.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Any, Callable, Optional

from sqlmodel import Field, Session, SQLModel, select

from src.brokers.base import BrokerAdapter
from src.brokers.factory import build_broker_adapter
from src.config import Settings
from src.execution.executor import Executor
from src.logging_config import get_logger
from src.models.orders import AssetType, Instruction, OrderType, TradeProposal
from src.notifications.webhook import notify

logger = get_logger(__name__)

DEFAULT_MONITOR_INTERVAL_SEC = 30


class BracketOrderStatus:
    OPEN = "OPEN"
    CLOSED_TARGET = "CLOSED_TARGET"
    CLOSED_STOP = "CLOSED_STOP"
    CLOSED_TRAILING_STOP = "CLOSED_TRAILING_STOP"
    CLOSED_ERROR = "CLOSED_ERROR"
    CANCELED = "CANCELED"


class BracketOrderRecord(SQLModel, table=True):
    __tablename__ = "bracket_orders"

    id: Optional[int] = Field(default=None, primary_key=True)
    entry_decision_id: str = Field(index=True, unique=True)
    account: str
    symbol: str = Field(index=True)
    quantity: int
    entry_price: str
    stop_loss_price: Optional[str] = None
    take_profit_price: Optional[str] = None
    # Percent below the highest price seen since entry, e.g. "0.05" = 5%.
    # Ratchets up only, never down — see manage_bracket_orders below.
    trailing_stop_pct: Optional[str] = None
    highest_price_seen: Optional[str] = None
    status: str = Field(default=BracketOrderStatus.OPEN, index=True)
    created_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    closed_at: Optional[datetime] = None
    exit_decision_id: Optional[str] = None
    exit_price: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "entry_decision_id": self.entry_decision_id,
            "account": self.account,
            "symbol": self.symbol,
            "quantity": self.quantity,
            "entry_price": self.entry_price,
            "stop_loss_price": self.stop_loss_price,
            "take_profit_price": self.take_profit_price,
            "trailing_stop_pct": self.trailing_stop_pct,
            "highest_price_seen": self.highest_price_seen,
            "status": self.status,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "closed_at": self.closed_at,
            "exit_decision_id": self.exit_decision_id,
            "exit_price": self.exit_price,
        }


class BracketOrderService:
    def __init__(self, session: Session):
        self.session = session

    def list_open(self) -> list[BracketOrderRecord]:
        stmt = select(BracketOrderRecord).where(BracketOrderRecord.status == BracketOrderStatus.OPEN)
        return list(self.session.exec(stmt).all())

    def list_all(self) -> list[BracketOrderRecord]:
        stmt = select(BracketOrderRecord).order_by(BracketOrderRecord.created_at.desc())
        return list(self.session.exec(stmt).all())

    def attach(
        self,
        entry_decision_id: str,
        account: str,
        symbol: str,
        quantity: int,
        entry_price: float,
        stop_loss_price: Optional[float],
        take_profit_price: Optional[float],
        trailing_stop_pct: Optional[float],
        created_by: str,
    ) -> BracketOrderRecord:
        existing = self.session.exec(
            select(BracketOrderRecord).where(BracketOrderRecord.entry_decision_id == entry_decision_id)
        ).first()
        if existing is not None:
            raise ValueError(f"A bracket is already attached to order {entry_decision_id}")

        record = BracketOrderRecord(
            entry_decision_id=entry_decision_id,
            account=account,
            symbol=symbol.upper().strip(),
            quantity=quantity,
            entry_price=str(entry_price),
            stop_loss_price=str(stop_loss_price) if stop_loss_price is not None else None,
            take_profit_price=str(take_profit_price) if take_profit_price is not None else None,
            trailing_stop_pct=str(trailing_stop_pct) if trailing_stop_pct is not None else None,
            highest_price_seen=str(entry_price) if trailing_stop_pct is not None else None,
            created_by=created_by,
        )
        self.session.add(record)
        self.session.commit()
        self.session.refresh(record)
        logger.info(
            "bracket_order_attached",
            entry_decision_id=entry_decision_id,
            symbol=record.symbol,
            stop_loss_price=record.stop_loss_price,
            take_profit_price=record.take_profit_price,
            trailing_stop_pct=record.trailing_stop_pct,
        )
        return record

    def cancel(self, bracket_id: int) -> Optional[BracketOrderRecord]:
        """Stops monitoring only — does NOT sell the position; it stays open exactly as an unbracketed order would."""
        record = self.session.get(BracketOrderRecord, bracket_id)
        if record is None or record.status != BracketOrderStatus.OPEN:
            return None
        record.status = BracketOrderStatus.CANCELED
        record.closed_at = datetime.utcnow()
        self.session.add(record)
        self.session.commit()
        self.session.refresh(record)
        logger.info("bracket_order_canceled", bracket_id=bracket_id, symbol=record.symbol)
        return record

    def _close(self, record: BracketOrderRecord, status: str, exit_decision_id: Optional[str], exit_price: Optional[float]) -> None:
        record.status = status
        record.closed_at = datetime.utcnow()
        record.exit_decision_id = exit_decision_id
        record.exit_price = str(exit_price) if exit_price is not None else None
        self.session.add(record)
        self.session.commit()


async def manage_bracket_orders(session: Session, settings: Settings, broker: BrokerAdapter) -> int:
    """
    Checks every OPEN bracket's live quote against its stop/target (and
    ratchets any trailing stop up first); submits a MARKET SELL for any
    that were hit. Returns how many were closed. Same failure handling as
    autonomous_trader.manage_open_positions(): a quote failure just skips
    this cycle and retries next time, a risk-check rejection leaves the
    bracket OPEN to retry, and a broker-call failure after risk-approval
    closes it defensively (CLOSED_ERROR) rather than retrying forever
    against a broker that may keep rejecting it.
    """
    service = BracketOrderService(session)
    executor = Executor(session=session, broker=broker)
    closed = 0

    for record in service.list_open():
        try:
            quote = await broker.get_quote(record.symbol)
            last = float(quote["last"])
        except Exception as exc:
            logger.warning("bracket_order_quote_failed", symbol=record.symbol, error=str(exc))
            continue

        stop_price = float(record.stop_loss_price) if record.stop_loss_price else None
        target_price = float(record.take_profit_price) if record.take_profit_price else None
        hit_trailing = False

        if record.trailing_stop_pct:
            pct = float(record.trailing_stop_pct)
            highest = max(float(record.highest_price_seen or record.entry_price), last)
            if highest > float(record.highest_price_seen or 0):
                record.highest_price_seen = str(highest)
                session.add(record)
                session.commit()
            trailing_stop_price = highest * (1 - pct)
            # A fixed stop-loss, if also set, still applies as a floor —
            # the trailing stop only ever tightens the exit, never loosens
            # protection below the level the human explicitly set.
            if stop_price is None or trailing_stop_price > stop_price:
                stop_price = trailing_stop_price
                hit_trailing = last <= trailing_stop_price

        hit_target = target_price is not None and last >= target_price
        hit_stop = stop_price is not None and last <= stop_price
        if not (hit_target or hit_stop):
            continue

        if hit_target:
            exit_reason, status = "take-profit", BracketOrderStatus.CLOSED_TARGET
        elif hit_trailing:
            exit_reason, status = "trailing-stop", BracketOrderStatus.CLOSED_TRAILING_STOP
        else:
            exit_reason, status = "stop-loss", BracketOrderStatus.CLOSED_STOP

        decision_id = f"bracket-exit-{uuid.uuid4()}"
        try:
            proposal = TradeProposal(
                decision_id=decision_id,
                agent_id="default",
                account=record.account,
                symbol=record.symbol,
                asset_type=AssetType.EQUITY,
                instruction=Instruction.SELL,
                quantity=record.quantity,
                order_type=OrderType.MARKET,
                strategy_id=f"bracket:{record.id}:exit",
            )
            preview = await executor.preview_order(proposal)
            if preview.risk_verdict != "APPROVED":
                logger.error("bracket_exit_rejected_by_risk_checks", symbol=record.symbol, details=preview.risk_details)
                continue

            await executor.execute_order(
                decision_id=decision_id,
                preview_id=preview.preview_id,
                approved_by=record.created_by,
                approved_at=datetime.utcnow(),
                attestation=f"Bracket order {exit_reason} exit — auto-triggered, no further human review.",
                idempotency_key=f"{decision_id}:bracket-exit",
            )
        except Exception as exc:
            logger.error("bracket_exit_order_failed", symbol=record.symbol, error=str(exc))
            service._close(record, BracketOrderStatus.CLOSED_ERROR, decision_id, None)
            await notify(settings, f":warning: Bracket exit FAILED for {record.symbol}: {exc}")
            closed += 1
            continue

        pnl = (last - float(record.entry_price)) * record.quantity
        service._close(record, status, decision_id, last)
        pnl_emoji = ":chart_with_upwards_trend:" if pnl >= 0 else ":chart_with_downwards_trend:"
        await notify(
            settings,
            f"{pnl_emoji} Bracket closed {record.symbol} on {exit_reason}: "
            f"entry {float(record.entry_price):.2f} → exit {last:.2f}, P/L {pnl:+.2f}",
        )
        closed += 1

    return closed


async def run_bracket_order_loop(
    session_factory: Callable[[], Session],
    get_settings_fn: Callable[[], Settings],
    stop_event: asyncio.Event,
    interval_sec: int = DEFAULT_MONITOR_INTERVAL_SEC,
) -> None:
    """Runs manage_bracket_orders on a timer until stop_event is set — same shape as every other background loop in this codebase."""
    logger.info("bracket_order_loop_started")
    while not stop_event.is_set():
        settings = get_settings_fn()
        session = session_factory()
        try:
            broker = build_broker_adapter(settings)
            closed = await manage_bracket_orders(session, settings, broker)
            if closed:
                logger.info("bracket_orders_closed", count=closed)
        except Exception as exc:
            logger.error("bracket_order_loop_iteration_failed", error=str(exc))
        finally:
            session.close()

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=max(interval_sec, 5))
        except asyncio.TimeoutError:
            pass
    logger.info("bracket_order_loop_stopped")


__all__ = [
    "BracketOrderRecord",
    "BracketOrderService",
    "BracketOrderStatus",
    "DEFAULT_MONITOR_INTERVAL_SEC",
    "manage_bracket_orders",
    "run_bracket_order_loop",
]
