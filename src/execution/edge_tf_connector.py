"""
Background task that polls EDGE-TF's execution gateway for approved trades
and records them as external signals — the EDGE-TF-specific counterpart to
src/execution/strategy_scanner.py. Runs as an asyncio task started at
FastAPI startup (see src/api/server.py) and stopped at shutdown.

What it does NOT do: preview, approve, or execute an order. Polling only
ever writes rows to ExternalSignalRecord (external_signals.py) for a human
to review in the dashboard. Turning one into an actual order still goes
through the same preview -> approve -> execute flow as any manually entered
proposal — see claim_upstream/report_upstream below for the two upstream
calls that happen either side of that local execute, and
src/api/server.py's /v1/orders/execute handler for where they're wired in.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Callable, Optional

from sqlmodel import Session

from src.brokers.factory import build_broker_adapter
from src.config import Settings
from src.execution.external_signals import ExternalSignalRecord, ExternalSignalService
from src.integrations.edge_tf_client import EdgeTFClient, EdgeTFGatewayError
from src.logging_config import get_logger
from src.models.orders import OrderStatus

logger = get_logger(__name__)

SOURCE = "edge-tf"

# This system's OrderStatus -> EDGE-TF's ExecutionReportStatus. PREVIEWED/
# APPROVED never reach report_upstream (they're pre-execution states).
_REPORT_STATUS_MAP = {
    OrderStatus.SUBMITTED: "ACCEPTED",
    OrderStatus.ACKNOWLEDGED: "ACCEPTED",
    OrderStatus.PARTIAL_FILL: "PARTIALLY_FILLED",
    OrderStatus.FILLED: "FILLED",
    OrderStatus.CANCELED: "CANCELLED",
    OrderStatus.REJECTED: "REJECTED",
    OrderStatus.FAILED: "ERROR",
}


def _client(settings: Settings) -> Optional[EdgeTFClient]:
    if not settings.edge_tf_gateway_url or not settings.edge_tf_gateway_token:
        return None
    return EdgeTFClient(settings.edge_tf_gateway_url, settings.edge_tf_gateway_token)


async def poll_once(session: Session, settings: Settings) -> int:
    """One pass: fetch every currently-approved-and-unclaimed EDGE-TF trade, record any new ones. Returns how many were new."""
    client = _client(settings)
    if client is None:
        return 0

    try:
        instructions = await client.list_orders()
    except EdgeTFGatewayError as exc:
        logger.warning("edge_tf_poll_failed", error=str(exc))
        return 0

    service = ExternalSignalService(session)
    new_count = 0
    for instruction in instructions:
        record = service.record_if_new(SOURCE, instruction)
        if record is not None:
            new_count += 1
    return new_count


async def post_portfolio_snapshots(session: Session, settings: Settings) -> None:
    """
    Best-effort: pushes current broker balances/positions for every allowed
    account back to EDGE-TF, so its portfolio view reflects reality between
    trades too, not just at report_upstream time. Failure here is logged
    and swallowed — it's a reconciliation nicety, not on the execution path.
    """
    client = _client(settings)
    if client is None:
        return

    broker = build_broker_adapter(settings)
    for alias in settings.account_allowlist:
        try:
            profile = settings.get_account_profile(alias)
            balances = await broker.get_balances(profile)
            positions = await broker.get_positions(profile)
            snapshot = {
                "broker": profile.broker.value if hasattr(profile.broker, "value") else str(profile.broker),
                "account_id": alias,
                "cash": float(balances.get("cash", 0.0)) if isinstance(balances, dict) else 0.0,
                "positions": [
                    {
                        "symbol": p.get("symbol"),
                        "quantity": p.get("quantity", 0),
                        "market_value": p.get("market_value"),
                        "average_cost": p.get("average_cost"),
                    }
                    for p in (positions or [])
                    if p.get("symbol")
                ],
                "as_of": datetime.now(timezone.utc).isoformat(),
                "source": "EXTERNAL_EXECUTION_SERVICE",
            }
            await client.post_snapshot(snapshot)
        except Exception as exc:
            logger.warning("edge_tf_snapshot_post_failed", account=alias, error=str(exc))


async def run_connector_loop(
    session_factory: Callable[[], Session],
    get_settings_fn: Callable[[], Settings],
    stop_event: asyncio.Event,
) -> None:
    """Runs poll_once (and, less often, a portfolio snapshot push) on a timer until stop_event is set."""
    logger.info("edge_tf_connector_started")
    iterations = 0
    while not stop_event.is_set():
        settings = get_settings_fn()
        interval = max(settings.edge_tf_poll_interval_sec, 5)

        if settings.edge_tf_connector_enabled:
            session = session_factory()
            try:
                new_count = await poll_once(session, settings)
                if new_count:
                    logger.info("edge_tf_poll_pass_complete", new_signals=new_count)
                # Push a snapshot roughly every 5 polls rather than every
                # tick — it's an account-wide broker call, not a cheap one.
                if iterations % 5 == 0:
                    await post_portfolio_snapshots(session, settings)
            except Exception as exc:
                logger.error("edge_tf_connector_iteration_failed", error=str(exc))
            finally:
                session.close()
            iterations += 1

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    logger.info("edge_tf_connector_stopped")


async def claim_upstream(record: ExternalSignalRecord, settings: Settings, *, executor_id: str) -> None:
    """
    Atomically claims this trade on EDGE-TF's side right before local
    execution proceeds. Raises EdgeTFGatewayError if EDGE-TF refuses (already
    claimed, expired, or mutated since it was polled) — the caller MUST NOT
    execute locally when this raises, or the same upstream trade could be
    executed twice.
    """
    client = _client(settings)
    if client is None:
        raise EdgeTFGatewayError(503, "NOT_CONFIGURED", "EDGE-TF gateway is not configured")
    await client.claim(record.external_trade_id, executor_id=executor_id)


async def report_upstream(record: ExternalSignalRecord, settings: Settings, order_status) -> None:
    """
    Best-effort report of the local execution outcome back to EDGE-TF.
    Failure here is logged, never raised: the local trade has already
    executed (or definitively failed) by the time this is called, and that
    outcome must reach the caller regardless of whether EDGE-TF heard about it.
    """
    client = _client(settings)
    if client is None:
        return

    status = _REPORT_STATUS_MAP.get(order_status.status)
    if status is None:
        logger.warning(
            "edge_tf_report_skipped_unmapped_status",
            trade_id=record.external_trade_id,
            local_status=order_status.status,
        )
        return

    try:
        await client.report(
            record.external_trade_id,
            {
                "trade_id": record.external_trade_id,
                "instruction_id": record.instruction_id,
                "broker": "execution-engine",
                "broker_order_id": order_status.execution_id,
                "status": status,
                "filled_quantity": float(order_status.filled_quantity or 0),
                "average_price": float(order_status.average_fill_price) if order_status.average_fill_price is not None else None,
                "message": order_status.broker_message,
            },
        )
    except Exception as exc:
        logger.warning("edge_tf_report_failed", trade_id=record.external_trade_id, error=str(exc))


__all__ = [
    "SOURCE",
    "claim_upstream",
    "poll_once",
    "post_portfolio_snapshots",
    "report_upstream",
    "run_connector_loop",
]
