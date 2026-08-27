"""
Persisted, human-reviewable strategy signals — the autonomous half of the
strategy feature: a background scanner (src/execution/strategy_scanner.py)
continuously evaluates the strategy catalog against live price history and
records what it finds here, with no order ever placed or even previewed
automatically. A signal sitting in this table is inert data until a human
looks at it in the dashboard and chooses to load it into the order ticket
— which then runs through the exact same preview -> risk checks -> human
approval -> execute gate as any manually-entered order. This table has no
side effects of its own.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from sqlmodel import Field, Session, SQLModel, select

from src.logging_config import get_logger
from src.strategy.catalog import STRATEGIES, SignalDetail

logger = get_logger(__name__)


class SignalStatus:
    PENDING = "PENDING"
    DISMISSED = "DISMISSED"


class StrategySignalRecord(SQLModel, table=True):
    """One fired strategy signal, awaiting human review (or already dismissed)."""

    __tablename__ = "strategy_signals"

    id: Optional[int] = Field(default=None, primary_key=True)
    strategy_id: str = Field(index=True)
    symbol: str = Field(index=True)
    side: str = "BUY"
    entry_price: float
    stop_loss_price: float
    take_profit_price: float
    rationale: str
    status: str = Field(default=SignalStatus.PENDING, index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    trading_date: str = Field(default_factory=lambda: datetime.utcnow().date().isoformat(), index=True)

    def to_dict(self) -> dict[str, Any]:
        strategy = STRATEGIES.get(self.strategy_id)
        return {
            "id": self.id,
            "strategy_id": self.strategy_id,
            "strategy_name": strategy.name if strategy else self.strategy_id,
            "category": strategy.category.value if strategy else None,
            "symbol": self.symbol,
            "side": self.side,
            "entry_price": self.entry_price,
            "stop_loss_price": self.stop_loss_price,
            "take_profit_price": self.take_profit_price,
            "rationale": self.rationale,
            "status": self.status,
            "created_at": self.created_at,
        }


class StrategySignalService:
    def __init__(self, session: Session):
        self.session = session

    def record_if_new(self, strategy_id: str, symbol: str, detail: SignalDetail) -> Optional[StrategySignalRecord]:
        """
        Persists a fired signal unless an identical (strategy_id, symbol)
        signal is already PENDING today — the scanner runs every
        strategy_scan_interval_sec, and without this a single day's setup
        would otherwise spawn one duplicate row per scan interval.
        """
        today = date.today().isoformat()
        stmt = select(StrategySignalRecord).where(
            StrategySignalRecord.strategy_id == strategy_id,
            StrategySignalRecord.symbol == symbol,
            StrategySignalRecord.status == SignalStatus.PENDING,
            StrategySignalRecord.trading_date == today,
        )
        if self.session.exec(stmt).first() is not None:
            return None

        record = StrategySignalRecord(
            strategy_id=strategy_id,
            symbol=symbol,
            entry_price=detail.entry_price,
            stop_loss_price=detail.stop_loss_price,
            take_profit_price=detail.take_profit_price,
            rationale=detail.rationale,
        )
        self.session.add(record)
        self.session.commit()
        self.session.refresh(record)
        logger.info(
            "strategy_signal_fired",
            strategy_id=strategy_id,
            symbol=symbol,
            entry_price=detail.entry_price,
            stop_loss_price=detail.stop_loss_price,
            take_profit_price=detail.take_profit_price,
        )
        return record

    def list_signals(self, status: Optional[str] = None, limit: int = 50) -> list[StrategySignalRecord]:
        stmt = select(StrategySignalRecord)
        if status:
            stmt = stmt.where(StrategySignalRecord.status == status)
        stmt = stmt.order_by(StrategySignalRecord.created_at.desc()).limit(max(1, min(limit, 200)))
        return list(self.session.exec(stmt).all())

    def dismiss(self, signal_id: int) -> StrategySignalRecord:
        record = self.session.get(StrategySignalRecord, signal_id)
        if record is None:
            raise ValueError(f"Signal {signal_id} not found")
        record.status = SignalStatus.DISMISSED
        self.session.add(record)
        self.session.commit()
        self.session.refresh(record)
        return record


__all__ = ["StrategySignalRecord", "StrategySignalService", "SignalStatus"]
