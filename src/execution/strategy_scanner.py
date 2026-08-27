"""
Background task that continuously evaluates the strategy catalog against
the configured watchlist — the actual "agentic" / autonomous piece of the
strategy feature. Runs as an asyncio task started at FastAPI startup (see
src/api/server.py) and stopped at shutdown.

What it does NOT do: place an order, call /v1/orders/preview, or touch the
kill switch / risk checks in any way. It only ever writes rows to
StrategySignalRecord (src/execution/strategy_signals.py) for a human to
review in the dashboard. Turning a signal into an actual order still goes
through the same preview -> approve -> execute flow as any manually
entered proposal — this loop cannot execute a trade by itself.
"""

import asyncio
from typing import Callable, Optional

from sqlmodel import Session

from src.brokers.factory import build_broker_adapter
from src.config import Settings
from src.execution.strategy_signals import StrategySignalService
from src.logging_config import get_logger
from src.strategy import engine as strategy_engine
from src.strategy.catalog import STRATEGIES

logger = get_logger(__name__)


async def scan_once(session: Session, settings: Settings) -> int:
    """One full pass over every (watchlist symbol × strategy) pair. Returns how many new signals were recorded."""
    broker = build_broker_adapter(settings)
    service = StrategySignalService(session)
    new_count = 0

    for symbol in settings.strategy_watchlist:
        for strategy_id in STRATEGIES:
            try:
                detail = await strategy_engine.scan(broker, symbol, strategy_id)
            except Exception as exc:
                logger.warning("strategy_scan_failed", strategy_id=strategy_id, symbol=symbol, error=str(exc))
                continue
            if detail is None:
                continue
            record = service.record_if_new(strategy_id, symbol, detail)
            if record is not None:
                new_count += 1

    return new_count


async def run_scanner_loop(
    session_factory: Callable[[], Session],
    get_settings_fn: Callable[[], Settings],
    stop_event: asyncio.Event,
) -> None:
    """Runs scan_once on a timer until stop_event is set. Each iteration gets its own DB session."""
    logger.info("strategy_scanner_started")
    while not stop_event.is_set():
        settings = get_settings_fn()
        interval = max(settings.strategy_scan_interval_sec, 5)

        if settings.strategy_scan_enabled and settings.strategy_watchlist:
            session = session_factory()
            try:
                new_count = await scan_once(session, settings)
                if new_count:
                    logger.info("strategy_scan_pass_complete", new_signals=new_count)
            except Exception as exc:
                logger.error("strategy_scanner_iteration_failed", error=str(exc))
            finally:
                session.close()

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    logger.info("strategy_scanner_stopped")


__all__ = ["scan_once", "run_scanner_loop"]
