"""
Tracks open/closed positions opened by the autonomous trader
(src/execution/autonomous_trader.py) — separate from OrderRecord because a
position spans two orders (an entry and, later, an exit) plus the
standardized stop/target it's being managed against, none of which
OrderRecord has a place for.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlmodel import Field, Session, SQLModel, select

from src.logging_config import get_logger

logger = get_logger(__name__)


class AutonomousPositionStatus:
    OPEN = "OPEN"
    CLOSED_TARGET = "CLOSED_TARGET"    # take-profit hit
    CLOSED_STOP = "CLOSED_STOP"        # stop-loss hit
    CLOSED_ERROR = "CLOSED_ERROR"      # exit order failed to submit; closed defensively to stop retrying every cycle


class AutonomousPositionRecord(SQLModel, table=True):
    __tablename__ = "autonomous_positions"

    id: Optional[int] = Field(default=None, primary_key=True)
    symbol: str = Field(index=True)
    strategy_id: str = Field(index=True)
    account: str
    agent_id: str

    entry_decision_id: str = Field(unique=True, index=True)
    quantity: int
    entry_price: float
    stop_loss_price: float
    take_profit_price: float
    entry_rationale: str  # LLM-narrated (or template-fallback) explanation

    status: str = Field(default=AutonomousPositionStatus.OPEN, index=True)
    exit_decision_id: Optional[str] = None
    exit_price: Optional[float] = None
    exit_rationale: Optional[str] = None
    pnl_usd: Optional[float] = None

    opened_at: datetime = Field(default_factory=datetime.utcnow)
    closed_at: Optional[datetime] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "symbol": self.symbol,
            "strategy_id": self.strategy_id,
            "account": self.account,
            "entry_decision_id": self.entry_decision_id,
            "quantity": self.quantity,
            "entry_price": self.entry_price,
            "stop_loss_price": self.stop_loss_price,
            "take_profit_price": self.take_profit_price,
            "entry_rationale": self.entry_rationale,
            "status": self.status,
            "exit_decision_id": self.exit_decision_id,
            "exit_price": self.exit_price,
            "exit_rationale": self.exit_rationale,
            "pnl_usd": self.pnl_usd,
            "opened_at": self.opened_at,
            "closed_at": self.closed_at,
        }


class AutonomousPositionService:
    def __init__(self, session: Session):
        self.session = session

    def open_position(
        self,
        *,
        symbol: str,
        strategy_id: str,
        account: str,
        agent_id: str,
        entry_decision_id: str,
        quantity: int,
        entry_price: float,
        stop_loss_price: float,
        take_profit_price: float,
        entry_rationale: str,
    ) -> AutonomousPositionRecord:
        record = AutonomousPositionRecord(
            symbol=symbol,
            strategy_id=strategy_id,
            account=account,
            agent_id=agent_id,
            entry_decision_id=entry_decision_id,
            quantity=quantity,
            entry_price=entry_price,
            stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
            entry_rationale=entry_rationale,
        )
        self.session.add(record)
        self.session.commit()
        self.session.refresh(record)
        logger.info(
            "autonomous_position_opened",
            symbol=symbol,
            strategy_id=strategy_id,
            quantity=quantity,
            entry_price=entry_price,
            stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
        )
        return record

    def close_position(
        self,
        position: AutonomousPositionRecord,
        *,
        status: str,
        exit_decision_id: Optional[str],
        exit_price: Optional[float],
        exit_rationale: Optional[str],
    ) -> AutonomousPositionRecord:
        position.status = status
        position.exit_decision_id = exit_decision_id
        position.exit_price = exit_price
        position.exit_rationale = exit_rationale
        position.closed_at = datetime.utcnow()
        if exit_price is not None:
            position.pnl_usd = (exit_price - position.entry_price) * position.quantity
        self.session.add(position)
        self.session.commit()
        self.session.refresh(position)
        logger.info(
            "autonomous_position_closed",
            symbol=position.symbol,
            status=status,
            exit_price=exit_price,
            pnl_usd=position.pnl_usd,
        )
        return position

    def list_open(self) -> list[AutonomousPositionRecord]:
        return list(
            self.session.exec(
                select(AutonomousPositionRecord).where(AutonomousPositionRecord.status == AutonomousPositionStatus.OPEN)
            ).all()
        )

    def has_open_position(self, symbol: str, strategy_id: str) -> bool:
        existing = self.session.exec(
            select(AutonomousPositionRecord).where(
                AutonomousPositionRecord.symbol == symbol,
                AutonomousPositionRecord.strategy_id == strategy_id,
                AutonomousPositionRecord.status == AutonomousPositionStatus.OPEN,
            )
        ).first()
        return existing is not None

    def list_all(self, limit: int = 100) -> list[AutonomousPositionRecord]:
        stmt = select(AutonomousPositionRecord).order_by(AutonomousPositionRecord.opened_at.desc()).limit(max(1, min(limit, 500)))
        return list(self.session.exec(stmt).all())


__all__ = ["AutonomousPositionRecord", "AutonomousPositionService", "AutonomousPositionStatus"]
