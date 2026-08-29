"""
"Notify me when QQQ crosses $700" — a price alert checked on a timer
against real quotes, firing through the same outbound webhook already
built for kill-switch trips and autonomous trade activity (see
src/notifications/webhook.py) rather than inventing a second
notification channel.

An alert fires (once) and then deactivates itself — this is a one-shot
trigger ("tell me when it crosses"), not a standing condition someone
has to remember to turn off, matching how a price alert works in every
real brokerage app. Re-arm by creating a new one.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Callable, Optional

from sqlmodel import Field, Session, SQLModel, select

from src.brokers.base import BrokerAdapter
from src.brokers.factory import build_broker_adapter
from src.config import Settings
from src.logging_config import get_logger
from src.notifications.webhook import notify_sync

logger = get_logger(__name__)

DEFAULT_CHECK_INTERVAL_SEC = 60


class AlertCondition:
    ABOVE = "ABOVE"
    BELOW = "BELOW"


class PriceAlertRecord(SQLModel, table=True):
    __tablename__ = "price_alerts"

    id: Optional[int] = Field(default=None, primary_key=True)
    symbol: str = Field(index=True)
    condition: str  # AlertCondition.ABOVE | BELOW
    target_price: float
    created_by: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    active: bool = Field(default=True, index=True)
    triggered_at: Optional[datetime] = None
    triggered_price: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "symbol": self.symbol,
            "condition": self.condition,
            "target_price": self.target_price,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "active": self.active,
            "triggered_at": self.triggered_at,
            "triggered_price": self.triggered_price,
        }


class PriceAlertService:
    def __init__(self, session: Session):
        self.session = session

    def create(self, symbol: str, condition: str, target_price: float, created_by: str) -> PriceAlertRecord:
        condition = condition.upper()
        if condition not in (AlertCondition.ABOVE, AlertCondition.BELOW):
            raise ValueError(f"condition must be ABOVE or BELOW, got {condition!r}")
        if target_price <= 0:
            raise ValueError("target_price must be positive")

        alert = PriceAlertRecord(symbol=symbol.upper().strip(), condition=condition, target_price=target_price, created_by=created_by)
        self.session.add(alert)
        self.session.commit()
        self.session.refresh(alert)
        logger.info("price_alert_created", symbol=alert.symbol, condition=condition, target_price=target_price)
        return alert

    def list_all(self, active_only: bool = False) -> list[PriceAlertRecord]:
        stmt = select(PriceAlertRecord)
        if active_only:
            stmt = stmt.where(PriceAlertRecord.active == True)  # noqa: E712
        stmt = stmt.order_by(PriceAlertRecord.created_at.desc())
        return list(self.session.exec(stmt).all())

    def cancel(self, alert_id: int) -> Optional[PriceAlertRecord]:
        alert = self.session.get(PriceAlertRecord, alert_id)
        if alert is None or not alert.active:
            return None
        alert.active = False
        self.session.add(alert)
        self.session.commit()
        self.session.refresh(alert)
        logger.info("price_alert_canceled", alert_id=alert_id, symbol=alert.symbol)
        return alert

    def _mark_triggered(self, alert: PriceAlertRecord, price: float) -> None:
        alert.active = False
        alert.triggered_at = datetime.utcnow()
        alert.triggered_price = price
        self.session.add(alert)
        self.session.commit()


async def check_alerts_once(session: Session, settings, broker: BrokerAdapter) -> int:
    """
    Checks every active alert against a real quote, fetched once per
    distinct symbol (not once per alert — several alerts can watch the
    same symbol). Fires notify() and deactivates each alert that's now
    true. Returns how many fired. A quote failure for one symbol is
    logged and skipped, same "one failure doesn't block the rest"
    pattern as every other batch operation in this codebase.
    """
    service = PriceAlertService(session)
    active = service.list_all(active_only=True)
    if not active:
        return 0

    symbols = sorted({a.symbol for a in active})
    prices: dict[str, float] = {}
    for symbol in symbols:
        try:
            quote = await broker.get_quote(symbol)
            prices[symbol] = float(quote["last"])
        except Exception as exc:
            logger.warning("price_alert_quote_failed", symbol=symbol, error=str(exc))

    fired = 0
    for alert in active:
        price = prices.get(alert.symbol)
        if price is None:
            continue
        hit = (alert.condition == AlertCondition.ABOVE and price >= alert.target_price) or (
            alert.condition == AlertCondition.BELOW and price <= alert.target_price
        )
        if not hit:
            continue
        service._mark_triggered(alert, price)
        direction = "risen above" if alert.condition == AlertCondition.ABOVE else "fallen below"
        notify_sync(settings, f":bell: {alert.symbol} has {direction} {alert.target_price:.2f} — now {price:.2f}")
        fired += 1

    return fired


async def run_price_alert_loop(
    session_factory: Callable[[], Session],
    get_settings_fn: Callable[[], Settings],
    stop_event: asyncio.Event,
    interval_sec: int = DEFAULT_CHECK_INTERVAL_SEC,
) -> None:
    """
    Runs check_alerts_once on a timer until stop_event is set — same
    shape as every other background loop in this codebase (autonomous
    trader, strategy scanner, strategy rotation). A short 60s default
    interval, unlike the once-a-day strategy rotation: a price alert is
    time-sensitive in a way "which strategy performed best" isn't.
    """
    logger.info("price_alert_loop_started")
    while not stop_event.is_set():
        settings = get_settings_fn()
        session = session_factory()
        try:
            broker = build_broker_adapter(settings)
            fired = await check_alerts_once(session, settings, broker)
            if fired:
                logger.info("price_alerts_fired", count=fired)
        except Exception as exc:
            logger.error("price_alert_loop_iteration_failed", error=str(exc))
        finally:
            session.close()

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=max(interval_sec, 5))
        except asyncio.TimeoutError:
            pass
    logger.info("price_alert_loop_stopped")


__all__ = ["AlertCondition", "DEFAULT_CHECK_INTERVAL_SEC", "PriceAlertRecord", "PriceAlertService", "check_alerts_once", "run_price_alert_loop"]
