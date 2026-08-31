"""
FastAPI application and route handlers.
External contract for EDGE-TF integration.
"""

import asyncio
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Optional
import uuid

from fastapi import FastAPI, Depends, HTTPException, Header, Path, Query, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, text
from sqlalchemy.orm import Session
from sqlmodel import select

from src.brokers.base import BrokerAPIOutageError, BrokerAuthenticationError
from src.brokers.factory import build_broker_adapter
from src.config import get_settings, Settings
from src.database import SessionLocal, init_db
from src.execution.agent_exposure_guard import AgentExposureGuard
from src.execution.algo_slices import AlgoSliceRecord
from src.execution.drawdown_guard import DrawdownGuard
from src.backtest.api_models import BacktestRequest
from src.backtest.runner import run_backtest_suite, summarize_suite
from src.execution.autonomous_positions import AutonomousPositionService
from src.execution.autonomous_trader import autonomous_cycle_once, run_autonomous_loop
from src.execution.daily_plan import DailyPlanService
from src.execution.strategy_ranking import rank_strategies_by_recent_performance
from src.execution.strategy_rotation import rotate_once, run_strategy_rotation_loop
from src.execution.price_alerts import PriceAlertService, check_alerts_once, run_price_alert_loop
from src.execution.bracket_orders import BracketOrderService, manage_bracket_orders, run_bracket_order_loop
from src.execution.watchlists import WatchlistService
from src.execution.edge_tf_connector import SOURCE as EDGE_TF_SOURCE, claim_upstream, poll_once as edge_tf_poll_once, report_upstream, run_connector_loop
from src.execution.ep_edge_earnings_adapter import (
    ExternalSignalIngestRequest as EarningsIngestRequest,
    SOURCE as EpEdgeSource,
    record_batch as ep_edge_record_batch,
)
from src.execution.hedge_engine_adapter import SOURCE as HedgeSource, record_batch as hedge_record_batch
from src.execution.executor import Executor, OrderRecord
from src.execution.external_signals import ExternalSignalStatus, ExternalSignalService
from src.execution.kill_switch_state import GLOBAL_SCOPE, KillSwitchService
from src.execution.multi_leg import LegRef, execute_multi_leg_order, preview_multi_leg_order
from src.execution.position_reconciliation import PositionReconciliationService
from src.execution.reconciliation import ReconciliationService
from src.execution.strategy_scanner import run_scanner_loop, scan_once
from src.execution.strategy_signals import SignalStatus, StrategySignalService
from src.execution.symbol_coordination import SymbolCoordinationGuard
from src.integrations.edge_tf_client import EdgeTFGatewayError
from src.logging_config import configure_logging, get_logger
from src.models.daily_plan_api import ArmPlanRequest, DisarmPlanRequest, RankStrategiesRequest, StartAgentRequest
from src.models.bracket_order_api import AttachBracketOrderRequest
from src.models.price_alert_api import CreatePriceAlertRequest
from src.models.watchlist_api import AddWatchlistSymbolRequest
from src.models.multi_leg_orders import MultiLegExecuteRequest, MultiLegPreviewRequest
from src.models.orders import (
    TradeProposal,
    OrderPreview,
    ExecutionRequest,
    ExecutionReceipt,
    OrderStatus_Model,
    KillSwitchStatus,
    HealthStatus,
)
from src.risk.pretrade import MarketHoursValidator
from src.strategy import engine as strategy_engine
from src.strategy.catalog import STRATEGIES

logger = get_logger(__name__)


# Initialize app
app = FastAPI(
    title="EDGE-Execution",
    description="Deterministic broker-neutral order execution microservice for EDGE-TF",
    version="0.1.0",
)

# Local-dev CORS: permissive so the operator widget (served from a different
# port) can call this API directly from the browser. Tighten this to a named
# allowlist of origins before this is ever deployed anywhere reachable
# outside localhost.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_db() -> Session:
    """Dependency for database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_settings_dep() -> Settings:
    """Dependency for settings."""
    return get_settings()


def verify_admin_key(x_admin_key: Optional[str] = Header(None), settings: Settings = Depends(get_settings_dep)) -> bool:
    """Verify admin API key for protected endpoints."""
    if not x_admin_key or x_admin_key != settings.api_key_admin:
        raise HTTPException(status_code=403, detail="Invalid or missing admin key")
    return True


_scanner_stop_event: Optional[asyncio.Event] = None
_scanner_task: Optional[asyncio.Task] = None
_edge_tf_connector_stop_event: Optional[asyncio.Event] = None
_edge_tf_connector_task: Optional[asyncio.Task] = None
_autonomous_trader_stop_event: Optional[asyncio.Event] = None
_autonomous_trader_task: Optional[asyncio.Task] = None
_strategy_rotation_stop_event: Optional[asyncio.Event] = None
_strategy_rotation_task: Optional[asyncio.Task] = None
_price_alert_stop_event: Optional[asyncio.Event] = None
_price_alert_task: Optional[asyncio.Task] = None
_bracket_order_stop_event: Optional[asyncio.Event] = None
_bracket_order_task: Optional[asyncio.Task] = None


@app.on_event("startup")
async def startup_event():
    """Initialize database on startup."""
    configure_logging(get_settings().log_level, get_settings().log_format)
    init_db()

    global _scanner_stop_event, _scanner_task, _edge_tf_connector_stop_event, _edge_tf_connector_task
    global _autonomous_trader_stop_event, _autonomous_trader_task
    global _strategy_rotation_stop_event, _strategy_rotation_task, _price_alert_stop_event, _price_alert_task
    global _bracket_order_stop_event, _bracket_order_task
    _scanner_stop_event = asyncio.Event()
    _scanner_task = asyncio.create_task(run_scanner_loop(SessionLocal, get_settings, _scanner_stop_event))

    _edge_tf_connector_stop_event = asyncio.Event()
    _edge_tf_connector_task = asyncio.create_task(
        run_connector_loop(SessionLocal, get_settings, _edge_tf_connector_stop_event)
    )

    _autonomous_trader_stop_event = asyncio.Event()
    _autonomous_trader_task = asyncio.create_task(
        run_autonomous_loop(SessionLocal, get_settings, _autonomous_trader_stop_event)
    )

    _strategy_rotation_stop_event = asyncio.Event()
    _strategy_rotation_task = asyncio.create_task(
        run_strategy_rotation_loop(SessionLocal, get_settings, _strategy_rotation_stop_event)
    )

    _price_alert_stop_event = asyncio.Event()
    _price_alert_task = asyncio.create_task(
        run_price_alert_loop(SessionLocal, get_settings, _price_alert_stop_event)
    )

    _bracket_order_stop_event = asyncio.Event()
    _bracket_order_task = asyncio.create_task(
        run_bracket_order_loop(SessionLocal, get_settings, _bracket_order_stop_event)
    )

    logger.info("startup_complete")


@app.on_event("shutdown")
async def shutdown_event():
    """Stop the background strategy scanner, EDGE-TF connector, autonomous trader, and strategy rotation loop cleanly."""
    if _scanner_stop_event is not None:
        _scanner_stop_event.set()
    if _scanner_task is not None:
        await asyncio.wait_for(_scanner_task, timeout=10)

    if _edge_tf_connector_stop_event is not None:
        _edge_tf_connector_stop_event.set()
    if _edge_tf_connector_task is not None:
        await asyncio.wait_for(_edge_tf_connector_task, timeout=10)

    if _autonomous_trader_stop_event is not None:
        _autonomous_trader_stop_event.set()
    if _autonomous_trader_task is not None:
        await asyncio.wait_for(_autonomous_trader_task, timeout=10)

    if _strategy_rotation_stop_event is not None:
        _strategy_rotation_stop_event.set()
    if _strategy_rotation_task is not None:
        await asyncio.wait_for(_strategy_rotation_task, timeout=10)

    if _price_alert_stop_event is not None:
        _price_alert_stop_event.set()
    if _price_alert_task is not None:
        await asyncio.wait_for(_price_alert_task, timeout=10)

    if _bracket_order_stop_event is not None:
        _bracket_order_stop_event.set()
    if _bracket_order_task is not None:
        await asyncio.wait_for(_bracket_order_task, timeout=10)


@app.get("/v1/health")
async def health_check(
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings_dep)
) -> HealthStatus:
    """
    Health check endpoint.
    Returns service and dependency status.

    broker_connectivity is a real check, not a label: it builds whichever
    broker adapter the current settings actually select (PaperBrokerAdapter
    or a real SchwabBrokerAdapter — see src/brokers/factory.py) and calls
    list_accounts() on it. For Schwab this is the fastest genuine proof
    that the configured OAuth credentials work end-to-end — token refresh
    succeeds and the account is visible — the natural smoke test for
    "I just added real API keys, did it work". PaperBrokerAdapter's
    list_accounts() is a static value and can't fail.
    """

    # Check database
    db_status = "ok"
    try:
        db.execute(text("SELECT 1"))
    except Exception as e:
        db_status = "error"
        logger.error("database_health_check_failed", error=str(e))

    # Check broker connectivity
    broker_status = "ok"
    try:
        broker = build_broker_adapter(settings)
        await broker.list_accounts()
    except Exception as e:
        broker_status = "error"
        logger.error("broker_health_check_failed", error=str(e))

    return HealthStatus(
        status="healthy" if db_status == "ok" and broker_status == "ok" else "degraded",
        timestamp=datetime.utcnow(),
        database=db_status,
        broker_connectivity=broker_status,
    )


@app.post("/v1/orders/preview", response_model=OrderPreview, dependencies=[Depends(verify_admin_key)])
async def preview_order(
    proposal: TradeProposal,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> OrderPreview:
    """
    Preview an order before approval.
    
    Validates proposal, runs risk checks, and returns the configured broker preview.
    Does NOT submit the order; requires explicit /execute call.
    """
    
    try:
        executor = Executor(session=db)
        
        # Execute preview flow
        preview = await executor.preview_order(proposal)
        
        logger.info(
            "preview_success",
            decision_id=proposal.decision_id,
            preview_id=preview.preview_id,
        )
        
        return preview
        
    except ValueError as e:
        logger.error("preview_validation_error", decision_id=proposal.decision_id, error=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except BrokerAuthenticationError as e:
        # The kill switch has already been auto-engaged by Executor at this
        # point (see _shutdown_on_auth_failure) — this response just needs
        # to make the *cause* legible, distinctly from a generic 500, so an
        # operator sees "re-authenticate with the broker" rather than
        # guessing from a bare "Preview failed".
        logger.critical("preview_broker_auth_failed", decision_id=proposal.decision_id, error=str(e))
        raise HTTPException(
            status_code=503,
            detail=f"Broker authentication failed; trading halted pending re-authentication: {e}",
        )
    except BrokerAPIOutageError as e:
        # Transient (connectivity/5xx) — already retried internally by the
        # adapter and still failed. Deliberately does NOT trip the kill
        # switch the way an auth failure does: an outage is often
        # self-resolving within seconds, and forcing a dual-authorization
        # reset for something that clears itself would be its own hazard.
        logger.critical("preview_broker_api_outage", decision_id=proposal.decision_id, error=str(e))
        raise HTTPException(status_code=503, detail=f"Broker API unreachable, try again shortly: {e}")
    except Exception as e:
        logger.error("preview_error", decision_id=proposal.decision_id, error=str(e))
        raise HTTPException(status_code=500, detail="Preview failed")


@app.post("/v1/orders/execute", response_model=ExecutionReceipt, dependencies=[Depends(verify_admin_key)])
async def execute_order(
    request: ExecutionRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> ExecutionReceipt:
    """
    Execute a previously-approved order.

    Requires:
    - decision_id from original proposal
    - preview_id from preview response
    - approval artifact (approved_by, approved_at, attestation)
    - idempotency_key for safety

    Approval gates, kill switch, and risk checks are enforced server-side.

    If decision_id matches a signal this system received from an external
    decision engine (src/execution/external_signals.py), local status
    bookkeeping tracks it either way. Only an "edge-tf" sourced signal has
    an upstream gateway to talk to: the trade is atomically claimed there
    first — refused if something else already claimed or it expired, in
    which case this aborts before touching the broker — and the outcome is
    reported back afterward, success or failure, so EDGE-TF's own audit
    ledger stays accurate. A source like "ep-edge-earnings" has no such
    gateway (see src/execution/ep_edge_earnings_adapter.py) and just gets
    marked CLAIMED/EXECUTED/FAILED locally with no round trip.
    """

    external_signal = ExternalSignalService(db).get_by_trade_id(request.decision_id)
    signal_has_gateway = external_signal is not None and external_signal.source == EDGE_TF_SOURCE
    if external_signal is not None and external_signal.status == ExternalSignalStatus.PENDING:
        if signal_has_gateway:
            try:
                await claim_upstream(external_signal, settings, executor_id=settings.edge_tf_executor_id)
            except EdgeTFGatewayError as e:
                logger.error("edge_tf_claim_failed", decision_id=request.decision_id, error=str(e))
                raise HTTPException(
                    status_code=409,
                    detail=f"EDGE-TF refused to hand off this trade for execution: {e}",
                )
        ExternalSignalService(db).mark_status(request.decision_id, ExternalSignalStatus.CLAIMED)

    try:
        executor = Executor(session=db)

        # Execute order
        receipt = await executor.execute_order(
            decision_id=request.decision_id,
            preview_id=request.preview_id,
            approved_by=request.approval.approved_by,
            approved_at=request.approval.approved_at,
            attestation=request.approval.attestation,
            idempotency_key=request.approval.idempotency_key,
        )

        logger.info(
            "execute_success",
            decision_id=request.decision_id,
            execution_id=receipt.execution_id,
        )

        if external_signal is not None:
            if signal_has_gateway:
                order_status = await executor.get_order_status(request.decision_id)
                await report_upstream(external_signal, settings, order_status)
            ExternalSignalService(db).mark_status(request.decision_id, ExternalSignalStatus.EXECUTED)

        return receipt

    except ValueError as e:
        logger.error("execute_validation_error", decision_id=request.decision_id, error=str(e))
        if external_signal is not None:
            ExternalSignalService(db).mark_status(request.decision_id, ExternalSignalStatus.FAILED)
        raise HTTPException(status_code=400, detail=str(e))
    except BrokerAuthenticationError as e:
        logger.critical("execute_broker_auth_failed", decision_id=request.decision_id, error=str(e))
        if external_signal is not None:
            ExternalSignalService(db).mark_status(request.decision_id, ExternalSignalStatus.FAILED)
        raise HTTPException(
            status_code=503,
            detail=f"Broker authentication failed; trading halted pending re-authentication: {e}",
        )
    except BrokerAPIOutageError as e:
        logger.critical("execute_broker_api_outage", decision_id=request.decision_id, error=str(e))
        if external_signal is not None:
            ExternalSignalService(db).mark_status(request.decision_id, ExternalSignalStatus.FAILED)
        raise HTTPException(status_code=503, detail=f"Broker API unreachable, try again shortly: {e}")
    except Exception as e:
        logger.error("execute_error", decision_id=request.decision_id, error=str(e))
        if external_signal is not None:
            ExternalSignalService(db).mark_status(request.decision_id, ExternalSignalStatus.FAILED)
        raise HTTPException(status_code=500, detail="Execution failed")


@app.post("/v1/orders/multi-leg/preview", dependencies=[Depends(verify_admin_key)])
async def preview_multi_leg(
    request: MultiLegPreviewRequest,
    db: Session = Depends(get_db),
) -> dict:
    """
    Preview a 2-leg options combo (vertical spread, straddle, strangle, or
    an unvalidated "custom" pair) as ONE logical trade. Each leg still
    runs through the exact same Executor.preview_order risk checks
    (allowlists, notional caps, kill switch, stale-quote protection) as
    any other order — this just validates the combo's shape (see
    src/execution/multi_leg.py's validate_combo_structure) and combines
    the per-leg results into one net debit/credit and, for a vertical
    spread specifically, a standard max-loss/max-profit figure.

    Does not execute anything — see POST /v1/orders/multi-leg/execute,
    which takes each leg's own (decision_id, preview_id) from this
    response the same way a normal single-leg order requires from
    POST /v1/orders/preview.
    """
    try:
        legs = [leg.to_trade_proposal() for leg in request.legs]
        executor = Executor(session=db)
        combo_preview = await preview_multi_leg_order(executor, legs, combo_type=request.combo_type)
        return combo_preview.to_dict()
    except ValueError as e:
        logger.error("multi_leg_preview_validation_error", error=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except BrokerAuthenticationError as e:
        logger.critical("multi_leg_preview_broker_auth_failed", error=str(e))
        raise HTTPException(status_code=503, detail=f"Broker authentication failed; trading halted pending re-authentication: {e}")
    except BrokerAPIOutageError as e:
        logger.critical("multi_leg_preview_broker_api_outage", error=str(e))
        raise HTTPException(status_code=503, detail=f"Broker API unreachable, try again shortly: {e}")


@app.post("/v1/orders/multi-leg/execute", dependencies=[Depends(verify_admin_key)])
async def execute_multi_leg(
    request: MultiLegExecuteRequest,
    db: Session = Depends(get_db),
) -> dict:
    """
    Execute a previously-previewed combo — each leg by its own
    (decision_id, preview_id), same as a normal single-leg
    POST /v1/orders/execute. Legs execute sequentially; if a later leg
    fails after an earlier one already went through, this agent's kill
    switch is tripped immediately (a naked, unhedged leg is now open) —
    see src/execution/multi_leg.py's module docstring for why. The
    response's fully_executed/failed_leg_index tell you which case
    happened; a combo returning fully_executed=false has already been
    partially executed and needs manual review, not a blind retry.
    """
    order_by_decision_id: dict[str, OrderRecord] = {}
    for leg_ref in request.legs:
        order = db.exec(select(OrderRecord).where(OrderRecord.decision_id == leg_ref.decision_id)).first()
        if order is None:
            raise HTTPException(status_code=404, detail=f"No previewed order found for decision_id {leg_ref.decision_id!r}")
        order_by_decision_id[leg_ref.decision_id] = order

    legs = [
        LegRef(
            decision_id=leg_ref.decision_id, preview_id=leg_ref.preview_id,
            agent_id=order_by_decision_id[leg_ref.decision_id].agent_id,
            symbol=order_by_decision_id[leg_ref.decision_id].symbol,
        )
        for leg_ref in request.legs
    ]

    try:
        executor = Executor(session=db)
        result = await execute_multi_leg_order(
            executor, combo_id=request.combo_id, legs=legs,
            approved_by=request.approved_by, attestation=request.attestation,
        )
        logger.info(
            "multi_leg_execute_complete", combo_id=request.combo_id,
            fully_executed=result.fully_executed, legs_executed=len(result.executed_legs),
        )
        return result.to_dict()
    except BrokerAuthenticationError as e:
        logger.critical("multi_leg_execute_broker_auth_failed", combo_id=request.combo_id, error=str(e))
        raise HTTPException(status_code=503, detail=f"Broker authentication failed; trading halted pending re-authentication: {e}")


@app.get("/v1/orders", dependencies=[Depends(verify_admin_key)])
async def list_orders(
    account: Optional[str] = None,
    agent_id: Optional[str] = None,
    status: Optional[str] = None,
    symbol: Optional[str] = None,
    instruction: Optional[str] = None,
    strategy_id: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    """
    Every order this system has ever previewed/executed, most recent
    first, filterable enough to actually serve as a real transaction
    history rather than just the last few for a live activity feed —
    read-only, no side effects (unlike /v1/reconciliation/positions,
    which can trip the kill switch).

    Deliberately one unified list rather than splitting "order book" vs
    "trade book" the way some broker UIs do: this system doesn't model
    partial fills as separate trade rows distinct from their parent
    order (filled_quantity/average_fill_price live on the order itself),
    so there's nothing a second endpoint would show that this one
    doesn't already have. status alone (PREVIEWED/SUBMITTED/FILLED/
    REJECTED/...) tells you whether an order actually executed.

    start_date/end_date bound created_at (inclusive, UTC calendar days).
    "total" in the response is the full filtered count, independent of
    limit/offset, so a caller can page through everything rather than
    just knowing whether this one page filled up.
    """
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    stmt = select(OrderRecord)
    count_stmt = select(func.count()).select_from(OrderRecord)

    def _apply_filters(s):
        if account:
            s = s.where(OrderRecord.account == account)
        if agent_id:
            s = s.where(OrderRecord.agent_id == agent_id)
        if status:
            s = s.where(OrderRecord.status == status.upper())
        if symbol:
            s = s.where(OrderRecord.symbol == symbol.upper())
        if instruction:
            s = s.where(OrderRecord.instruction == instruction.upper())
        if strategy_id:
            s = s.where(OrderRecord.strategy_id == strategy_id)
        if start_date:
            s = s.where(OrderRecord.created_at >= datetime.combine(start_date, datetime.min.time()))
        if end_date:
            s = s.where(OrderRecord.created_at < datetime.combine(end_date, datetime.min.time()) + timedelta(days=1))
        return s

    stmt = _apply_filters(stmt).order_by(OrderRecord.created_at.desc()).limit(limit).offset(offset)
    count_stmt = _apply_filters(count_stmt)

    orders = db.exec(stmt).all()
    total = db.exec(count_stmt).one()

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "orders": [
            {
                "decision_id": o.decision_id,
                "agent_id": o.agent_id,
                "account": o.account,
                "symbol": o.symbol,
                "asset_type": o.asset_type,
                "instruction": o.instruction,
                "quantity": o.quantity,
                "order_type": o.order_type,
                "limit_price": o.limit_price,
                "stop_price": o.stop_price,
                "status": o.status,
                "risk_approved": o.risk_approved,
                "estimated_notional_usd": o.estimated_notional_usd,
                "filled_quantity": o.filled_quantity,
                "average_fill_price": o.average_fill_price,
                "execution_id": o.execution_id,
                "broker_status": o.broker_status,
                "broker_message": o.broker_message,
                "strategy_id": o.strategy_id,
                "strategy_stop_loss_price": o.strategy_stop_loss_price,
                "strategy_take_profit_price": o.strategy_take_profit_price,
                "algo_duration_minutes": o.algo_duration_minutes,
                "algo_slices": o.algo_slices,
                "created_at": o.created_at,
                "updated_at": o.updated_at,
            }
            for o in orders
        ]
    }


@app.get("/v1/account/{account}/balances", dependencies=[Depends(verify_admin_key)])
async def get_account_balances(
    account: str,
    settings: Settings = Depends(get_settings_dep),
):
    """
    Read-only balances for one account alias — no side effects (unlike
    DrawdownGuard, which captures/compares a baseline). Uses whichever
    broker the current settings select (paper or real Schwab; see
    src/brokers/factory.py), so this reflects real numbers once Schwab is
    configured.
    """
    try:
        profile = settings.get_account_profile(account)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    try:
        broker = build_broker_adapter(settings)
        balances = await broker.get_balances(profile)
        return {"account": account, "balances": balances}
    except Exception as e:
        logger.error("balances_query_error", account=account, error=str(e))
        raise HTTPException(status_code=502, detail=f"Could not fetch balances: {e}")


@app.get("/v1/account/{account}/positions", dependencies=[Depends(verify_admin_key)])
async def get_account_positions(
    account: str,
    settings: Settings = Depends(get_settings_dep),
):
    """Read-only broker-reported positions for one account alias — no side effects."""
    try:
        profile = settings.get_account_profile(account)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    try:
        broker = build_broker_adapter(settings)
        positions = await broker.get_positions(profile)
        return {"account": account, "positions": positions}
    except Exception as e:
        logger.error("positions_query_error", account=account, error=str(e))
        raise HTTPException(status_code=502, detail=f"Could not fetch positions: {e}")


@app.get("/v1/quotes", dependencies=[Depends(verify_admin_key)])
async def get_quotes(
    symbols: str = Query(..., description="Comma-separated tickers, e.g. QQQ,SPY,IWM"),
    settings: Settings = Depends(get_settings_dep),
):
    """
    Batch live quotes — one broker call per symbol (whichever broker
    current settings select; see src/brokers/factory.py), gathered
    concurrently. Backs watchlists and price alerts, both of which need
    "current price for N symbols" on a timer. One symbol failing (bad
    ticker, a transient broker hiccup) doesn't fail the others — it comes
    back with an "error" field instead of a quote, same "one failure
    doesn't abort the rest" pattern used throughout this codebase's
    batch operations.
    """
    tickers = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    if not tickers:
        raise HTTPException(status_code=400, detail="symbols must not be empty")
    if len(tickers) > 50:
        raise HTTPException(status_code=400, detail="At most 50 symbols per request")

    broker = build_broker_adapter(settings)

    async def _one(symbol: str) -> dict:
        try:
            return {"symbol": symbol, **await broker.get_quote(symbol)}
        except Exception as e:
            return {"symbol": symbol, "error": str(e)}

    results = await asyncio.gather(*[_one(s) for s in tickers])
    return {"quotes": results}


@app.get("/v1/watchlists", dependencies=[Depends(verify_admin_key)])
async def get_watchlists(db: Session = Depends(get_db)):
    """Every watchlist and its symbols in one call — {list_name: [symbol, ...]}."""
    return {"watchlists": WatchlistService(db).all_lists()}


@app.post("/v1/watchlists/{list_name}/items", dependencies=[Depends(verify_admin_key)])
async def add_watchlist_symbol(
    list_name: str,
    request: AddWatchlistSymbolRequest,
    db: Session = Depends(get_db),
):
    """Adds a symbol to a list — the list is created implicitly the first time a symbol is added to a new name. Adding an already-present symbol is a harmless no-op."""
    try:
        item = WatchlistService(db).add_symbol(list_name, request.symbol)
        return item.to_dict()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/v1/watchlists/{list_name}/items/{symbol}", dependencies=[Depends(verify_admin_key)])
async def remove_watchlist_symbol(list_name: str, symbol: str, db: Session = Depends(get_db)):
    """Removes one symbol from one list. The list itself disappears once its last symbol is gone — see WatchlistService's own docstring."""
    removed = WatchlistService(db).remove_symbol(list_name, symbol)
    if not removed:
        raise HTTPException(status_code=404, detail=f"{symbol} is not on the '{list_name}' list")
    return {"removed": True}


@app.delete("/v1/watchlists/{list_name}", dependencies=[Depends(verify_admin_key)])
async def delete_watchlist(list_name: str, db: Session = Depends(get_db)):
    """Deletes an entire list (every symbol on it)."""
    removed_count = WatchlistService(db).delete_list(list_name)
    return {"removed_symbols": removed_count}


@app.post("/v1/alerts", dependencies=[Depends(verify_admin_key)])
async def create_price_alert(request: CreatePriceAlertRequest, db: Session = Depends(get_db)):
    """
    Creates a one-shot price alert: fires an outbound webhook
    notification (src/notifications/webhook.py) the next time the
    background check (src/execution/price_alerts.py, ~60s interval)
    sees the symbol cross target_price, then deactivates itself.
    """
    try:
        alert = PriceAlertService(db).create(request.symbol, request.condition, request.target_price, request.created_by)
        return alert.to_dict()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/v1/alerts", dependencies=[Depends(verify_admin_key)])
async def list_price_alerts(active_only: bool = False, db: Session = Depends(get_db)):
    """Every alert, most recent first — pass active_only=true for just the ones still watching."""
    alerts = PriceAlertService(db).list_all(active_only=active_only)
    return {"alerts": [a.to_dict() for a in alerts]}


@app.delete("/v1/alerts/{alert_id}", dependencies=[Depends(verify_admin_key)])
async def cancel_price_alert(alert_id: int, db: Session = Depends(get_db)):
    """Cancels an alert before it fires. Already-fired or already-canceled alerts return 404 — nothing left to cancel."""
    alert = PriceAlertService(db).cancel(alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail=f"Alert {alert_id} not found or no longer active")
    return alert.to_dict()


@app.post("/v1/alerts/check-now", dependencies=[Depends(verify_admin_key)])
async def check_price_alerts_now(db: Session = Depends(get_db), settings: Settings = Depends(get_settings_dep)):
    """Triggers one immediate alert check instead of waiting for the next ~60s background tick — for testing/demo."""
    broker = build_broker_adapter(settings)
    fired = await check_alerts_once(db, settings, broker)
    return {"fired": fired}


@app.post("/v1/orders/bracket/attach", dependencies=[Depends(verify_admin_key)])
async def attach_bracket_order(
    request: AttachBracketOrderRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
):
    """
    Attaches a stop-loss / take-profit / trailing-stop exit plan to an
    already-executed BUY order (Dhan's "Super Order" pattern) — a human
    still places the entry through the normal preview -> execute flow
    first; this call only starts the background monitoring that closes it
    automatically once a level is hit (src/execution/bracket_orders.py).
    entry_price is the live quote at the moment of attaching, matching how
    every other paper-mode entry in this system treats "current price" as
    the fill price for a MARKET order.
    """
    stmt = select(OrderRecord).where(OrderRecord.decision_id == request.entry_decision_id)
    order = db.exec(stmt).first()
    if order is None:
        raise HTTPException(status_code=404, detail=f"Order {request.entry_decision_id} not found")
    # SUBMITTED (an async real broker, still awaiting reconciliation),
    # PARTIAL_FILL, or FILLED (PaperBrokerAdapter's instant-fill response —
    # see its submit_order() docstring) all mean the order has genuinely
    # reached the broker; anything else (PREVIEWED, REJECTED, ...) hasn't.
    if order.status not in ("SUBMITTED", "PARTIAL_FILL", "FILLED"):
        raise HTTPException(status_code=400, detail=f"Order {request.entry_decision_id} has not been executed (status: {order.status})")
    if order.instruction != "BUY":
        raise HTTPException(status_code=400, detail="A bracket can only be attached to a BUY entry — it manages a long position's exit")

    try:
        broker = build_broker_adapter(settings)
        quote = await broker.get_quote(order.symbol)
        entry_price = float(quote["last"])
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not fetch a live quote for {order.symbol}: {e}")

    try:
        record = BracketOrderService(db).attach(
            entry_decision_id=order.decision_id,
            account=order.account,
            symbol=order.symbol,
            quantity=order.quantity,
            entry_price=entry_price,
            stop_loss_price=request.stop_loss_price,
            take_profit_price=request.take_profit_price,
            trailing_stop_pct=request.trailing_stop_pct,
            created_by=request.created_by,
        )
        return record.to_dict()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/v1/orders/bracket", dependencies=[Depends(verify_admin_key)])
async def list_bracket_orders(db: Session = Depends(get_db)):
    """Every bracket order, most recent first — open ones are actively monitored, closed ones show how/where they exited."""
    records = BracketOrderService(db).list_all()
    return {"brackets": [r.to_dict() for r in records]}


@app.post("/v1/orders/bracket/{bracket_id}/cancel", dependencies=[Depends(verify_admin_key)])
async def cancel_bracket_order(bracket_id: int, db: Session = Depends(get_db)):
    """Stops monitoring a bracket — the underlying position stays open, exactly like an order that was never bracketed."""
    record = BracketOrderService(db).cancel(bracket_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Bracket {bracket_id} not found or no longer open")
    return record.to_dict()


@app.post("/v1/orders/bracket/check-now", dependencies=[Depends(verify_admin_key)])
async def check_bracket_orders_now(db: Session = Depends(get_db), settings: Settings = Depends(get_settings_dep)):
    """Triggers one immediate bracket-order check instead of waiting for the next ~30s background tick — for testing/demo."""
    broker = build_broker_adapter(settings)
    closed = await manage_bracket_orders(db, settings, broker)
    return {"closed": closed}


@app.get("/v1/orders/{decision_id}", response_model=OrderStatus_Model, dependencies=[Depends(verify_admin_key)])
async def get_order_status(
    decision_id: str,
    db: Session = Depends(get_db),
) -> OrderStatus_Model:
    """Query the status of an order by decision_id."""
    
    try:
        executor = Executor(session=db)
        status = await executor.get_order_status(decision_id)
        
        logger.info("order_status_queried", decision_id=decision_id, status=status.status)
        
        return status
        
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error("status_query_error", decision_id=decision_id, error=str(e))
        raise HTTPException(status_code=500, detail="Status query failed")


@app.get("/v1/orders/{decision_id}/slices", dependencies=[Depends(verify_admin_key)])
async def get_order_slices(decision_id: str, db: Session = Depends(get_db)):
    """
    Child MARKET slices submitted so far for a TWAP/VWAP order (see
    src/execution/algo_slices.py) — empty for any non-algo order. The
    audit trail of what was actually submitted, and why not for any slice
    that wasn't (e.g. SKIPPED_KILL_SWITCH).
    """
    stmt = (
        select(AlgoSliceRecord)
        .where(AlgoSliceRecord.parent_decision_id == decision_id)
        .order_by(AlgoSliceRecord.slice_index)
    )
    records = db.exec(stmt).all()
    return {"decision_id": decision_id, "slices": [r.to_dict() for r in records]}


@app.post("/v1/kill-switch/on", response_model=KillSwitchStatus)
async def kill_switch_on(
    _: bool = Depends(verify_admin_key),
    db: Session = Depends(get_db),
) -> KillSwitchStatus:
    """
    Enable the global kill switch.
    Prevents all new order executions.
    Requires admin API key.
    """
    record = KillSwitchService(db).set_state(
        enabled=True, set_by="admin", reason="Manually activated by admin"
    )

    logger.warning("kill_switch_enabled", actor=record.set_by)

    return KillSwitchStatus(
        enabled=record.enabled,
        set_by=record.set_by,
        set_at=record.set_at,
        reason=record.reason,
    )


@app.post("/v1/kill-switch/off", response_model=KillSwitchStatus)
async def kill_switch_off(
    _: bool = Depends(verify_admin_key),
    db: Session = Depends(get_db),
) -> KillSwitchStatus:
    """
    Disable the global kill switch.
    Allows order executions to resume.
    Requires admin API key.
    """
    record = KillSwitchService(db).set_state(
        enabled=False, set_by="admin", reason="Manually deactivated by admin"
    )

    logger.info("kill_switch_disabled", actor=record.set_by)

    return KillSwitchStatus(
        enabled=record.enabled,
        set_by=record.set_by,
        set_at=record.set_at,
        reason=record.reason,
    )


@app.get("/v1/kill-switch/status", response_model=KillSwitchStatus)
async def get_kill_switch_status(db: Session = Depends(get_db)) -> KillSwitchStatus:
    """Query current kill switch state."""
    record = KillSwitchService(db).get_state()
    return KillSwitchStatus(
        enabled=record.enabled,
        set_by=record.set_by,
        set_at=record.set_at,
        reason=record.reason,
    )


_AGENT_ID_PATH = Path(..., pattern=r"^[A-Za-z0-9_-]{1,64}$")


def _reject_global_scope_as_agent_id(agent_id: str) -> None:
    if agent_id == GLOBAL_SCOPE:
        raise HTTPException(
            status_code=400,
            detail=f"'{GLOBAL_SCOPE}' is reserved for the fleet-wide kill switch; use /v1/kill-switch/on|off|status",
        )


@app.post("/v1/kill-switch/agents/{agent_id}/on", response_model=KillSwitchStatus)
async def agent_kill_switch_on(
    agent_id: str = _AGENT_ID_PATH,
    _: bool = Depends(verify_admin_key),
    db: Session = Depends(get_db),
) -> KillSwitchStatus:
    """
    Halt one agent, without affecting any other agent or the fleet-wide
    switch — the redundant, per-agent half of the kill switch (see
    KillSwitchService.is_halted). Requires admin API key.
    """
    _reject_global_scope_as_agent_id(agent_id)
    record = KillSwitchService(db).set_state(
        enabled=True, set_by="admin", reason=f"Manually activated by admin for agent '{agent_id}'", scope=agent_id
    )
    logger.warning("agent_kill_switch_enabled", agent_id=agent_id, actor=record.set_by)
    return KillSwitchStatus(enabled=record.enabled, set_by=record.set_by, set_at=record.set_at, reason=record.reason)


@app.post("/v1/kill-switch/agents/{agent_id}/off", response_model=KillSwitchStatus)
async def agent_kill_switch_off(
    agent_id: str = _AGENT_ID_PATH,
    _: bool = Depends(verify_admin_key),
    db: Session = Depends(get_db),
) -> KillSwitchStatus:
    """
    Clear one agent's own halt. Does not clear the fleet-wide switch — if
    that is also on, the agent stays blocked until it is cleared too.
    Requires admin API key.
    """
    _reject_global_scope_as_agent_id(agent_id)
    record = KillSwitchService(db).set_state(
        enabled=False, set_by="admin", reason=f"Manually deactivated by admin for agent '{agent_id}'", scope=agent_id
    )
    logger.info("agent_kill_switch_disabled", agent_id=agent_id, actor=record.set_by)
    return KillSwitchStatus(enabled=record.enabled, set_by=record.set_by, set_at=record.set_at, reason=record.reason)


@app.get("/v1/kill-switch/agents/{agent_id}/status", response_model=KillSwitchStatus)
async def get_agent_kill_switch_status(
    agent_id: str = _AGENT_ID_PATH,
    db: Session = Depends(get_db),
) -> KillSwitchStatus:
    """
    Query one agent's own switch state — not whether the agent is actually
    halted (that also depends on the fleet-wide switch; see
    GET /v1/agents/status for the combined view).
    """
    _reject_global_scope_as_agent_id(agent_id)
    record = KillSwitchService(db).get_state(scope=agent_id)
    return KillSwitchStatus(enabled=record.enabled, set_by=record.set_by, set_at=record.set_at, reason=record.reason)


@app.get("/v1/agents/status")
async def get_agents_status(db: Session = Depends(get_db)):
    """
    Coordination-layer view across every agent this system knows about:
    every agent_id seen in order history, merged with every agent_id that
    has ever had its own kill switch toggled (an agent halted before it
    has ever placed an order still shows up here). For each, reports
    whether it is currently halted and, if so, whether that's from its own
    switch, the fleet-wide switch, or both.
    """
    kill_switch_service = KillSwitchService(db)
    global_record = kill_switch_service.get_state(GLOBAL_SCOPE)

    seen_in_orders = set(db.exec(select(OrderRecord.agent_id).distinct()).all())
    seen_in_switches = set(kill_switch_service.known_agent_scopes())
    agent_ids = sorted(seen_in_orders | seen_in_switches)

    agents = []
    for agent_id in agent_ids:
        agent_record = kill_switch_service.get_state(agent_id)
        agents.append(
            {
                "agent_id": agent_id,
                "halted": global_record.enabled or agent_record.enabled,
                "halted_by_global_switch": global_record.enabled,
                "halted_by_own_switch": agent_record.enabled,
                "own_switch_set_by": agent_record.set_by,
                "own_switch_set_at": agent_record.set_at,
                "own_switch_reason": agent_record.reason,
            }
        )

    return {
        "global_kill_switch": {
            "enabled": global_record.enabled,
            "set_by": global_record.set_by,
            "set_at": global_record.set_at,
            "reason": global_record.reason,
        },
        "agents": agents,
    }


@app.get("/v1/market-status")
async def get_market_status():
    """Query current US market hours status."""
    validator = MarketHoursValidator()
    return validator.get_market_status()


@app.post("/v1/reconciliation/positions", dependencies=[Depends(verify_admin_key)])
async def reconcile_positions(
    account: str = "primary",
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
):
    """
    Reconcile this system's believed positions (summed from filled order
    history) against what the broker actually reports for the account.
    Intended to run before every trading session begins — see
    PositionReconciliationService for why per-order status polling alone
    doesn't answer this question.

    On any mismatch, this automatically trips the kill switch — no admin
    key required to call this endpoint, since its only possible side effect
    is halting trading, never resuming it (resuming still requires the
    admin-gated /v1/kill-switch/off).
    """
    try:
        service = PositionReconciliationService(session=db, broker=build_broker_adapter(settings))
        report = await service.reconcile_or_halt(account, halted_by="reconciliation_check")
        return report.to_dict()
    except Exception as e:
        logger.error("position_reconciliation_error", account=account, error=str(e))
        raise HTTPException(status_code=500, detail=f"Position reconciliation failed: {e}")


@app.post("/v1/risk/drawdown-check", dependencies=[Depends(verify_admin_key)])
async def check_drawdown(
    account: str = "primary",
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
):
    """
    Compare this account's current equity against its captured start-of-day
    baseline (capturing the baseline now if this is the first check today).

    On breaching settings.max_daily_drawdown_pct, this automatically trips
    the kill switch — no admin key required to call this endpoint, since
    its only possible side effect is halting trading, never resuming it
    (resuming still requires the admin-gated /v1/kill-switch/off). Intended
    to be polled periodically through the trading day, the same way
    /v1/reconciliation/positions is intended to run before it starts.
    """
    try:
        guard = DrawdownGuard(session=db, broker=build_broker_adapter(settings))
        report = await guard.check_and_halt(account, halted_by="drawdown_check_endpoint")
        return report.to_dict()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("drawdown_check_error", account=account, error=str(e))
        raise HTTPException(status_code=500, detail=f"Drawdown check failed: {e}")


@app.post("/v1/risk/agent-exposure-check", dependencies=[Depends(verify_admin_key)])
async def check_agent_exposure(
    agent_id: str = Query(..., pattern=r"^[A-Za-z0-9_-]{1,64}$"),
    db: Session = Depends(get_db),
):
    """
    Sum this agent's committed notional (orders actually submitted to the
    broker, not merely previewed) for today and compare it against
    AgentRiskProfile.max_daily_notional_usd for this agent, if one is
    configured. A redundant, agent-scoped backstop alongside the
    account-level /v1/risk/drawdown-check — see AgentExposureGuard for why
    the two are independent.

    On breaching the cap, this halts only this agent's own kill switch
    scope — no admin key required, since the only possible side effect is
    halting this one agent, never resuming it or affecting any other agent
    (resuming still requires the admin-gated
    /v1/kill-switch/agents/{agent_id}/off).
    """
    _reject_global_scope_as_agent_id(agent_id)
    try:
        guard = AgentExposureGuard(session=db)
        report = guard.check_and_halt(agent_id, halted_by="agent_exposure_check_endpoint")
        return report.to_dict()
    except Exception as e:
        logger.error("agent_exposure_check_error", agent_id=agent_id, error=str(e))
        raise HTTPException(status_code=500, detail=f"Agent exposure check failed: {e}")


@app.get("/v1/risk/symbol-exposure", dependencies=[Depends(verify_admin_key)])
async def get_symbol_exposure(
    account: str,
    symbol: str,
    db: Session = Depends(get_db),
):
    """
    Read-only view of today's combined committed notional for one
    (account, symbol) pair, summed across every agent — the same number
    Executor checks synchronously at preview time (see
    SymbolCoordinationGuard). Useful for a coordination dashboard, or for
    an agent to check headroom before proposing an order rather than
    finding out from a rejection.
    """
    try:
        guard = SymbolCoordinationGuard(session=db)
        report = guard.check(account, symbol, Decimal("0"))
        return report.to_dict()
    except Exception as e:
        logger.error("symbol_exposure_query_error", account=account, symbol=symbol, error=str(e))
        raise HTTPException(status_code=500, detail=f"Symbol exposure query failed: {e}")


@app.post("/v1/strategies/scan-all", dependencies=[Depends(verify_admin_key)])
async def scan_all_strategies_now(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
):
    """
    Triggers one full pass of the background scanner immediately (every
    strategy × every symbol in settings.strategy_watchlist) instead of
    waiting for the next strategy_scan_interval_sec tick. Same effect as
    an autonomous pass — records any fired signals, places no orders.
    """
    try:
        new_count = await scan_once(db, settings)
        return {"new_signals": new_count, "watchlist": settings.strategy_watchlist}
    except Exception as e:
        logger.error("strategy_scan_all_error", error=str(e))
        raise HTTPException(status_code=502, detail=f"Scan-all failed: {e}")


@app.get("/v1/strategies")
async def list_strategies():
    """
    The fixed catalog of famous technical strategies (src/strategy/catalog.py),
    grouped implicitly by `category` (INTRADAY / MULTI_DAY / OTHER) — each
    with the stop-loss/take-profit convention commonly associated with it.
    Read-only, no side effects.
    """
    return {"strategies": [s.to_dict() for s in STRATEGIES.values()]}


@app.post("/v1/strategies/{strategy_id}/scan", dependencies=[Depends(verify_admin_key)])
async def scan_strategy(
    strategy_id: str,
    symbol: str = Query(..., pattern=r"^[A-Z]{1,5}$"),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
):
    """
    On-demand version of what the background scanner does continuously:
    fetch price history and check this one strategy's entry rule for this
    one symbol right now. If it fires, the signal is recorded exactly like
    an autonomous hit (visible in GET /v1/strategies/signals) — this does
    NOT preview or place an order.
    """
    if strategy_id not in STRATEGIES:
        raise HTTPException(status_code=404, detail=f"Unknown strategy_id: {strategy_id}")
    try:
        broker = build_broker_adapter(settings)
        detail = await strategy_engine.scan(broker, symbol, strategy_id)
    except Exception as e:
        logger.error("strategy_scan_error", strategy_id=strategy_id, symbol=symbol, error=str(e))
        raise HTTPException(status_code=502, detail=f"Scan failed: {e}")

    if detail is None:
        return {"signal": None, "message": "No entry condition met right now."}

    record = StrategySignalService(db).record_if_new(strategy_id, symbol, detail)
    return {"signal": (record or _pending_duplicate_signal(db, strategy_id, symbol)).to_dict()}


def _pending_duplicate_signal(db: Session, strategy_id: str, symbol: str):
    """record_if_new() returns None when today's signal already exists — fetch it so the response is never empty."""
    existing = [
        r
        for r in StrategySignalService(db).list_signals(status=SignalStatus.PENDING, limit=200)
        if r.strategy_id == strategy_id and r.symbol == symbol
    ]
    return existing[0] if existing else None


@app.get("/v1/strategies/signals", dependencies=[Depends(verify_admin_key)])
async def list_strategy_signals(
    status: Optional[str] = Query(default=SignalStatus.PENDING),
    limit: int = 50,
    db: Session = Depends(get_db),
):
    """
    Signals the strategy scanner (autonomous, background) or an on-demand
    scan has fired — the human-review queue. Defaults to PENDING; pass
    status=DISMISSED or omit status entirely for the full history.
    """
    records = StrategySignalService(db).list_signals(status=status or None, limit=limit)
    return {"signals": [r.to_dict() for r in records]}


@app.post("/v1/strategies/signals/{signal_id}/dismiss", dependencies=[Depends(verify_admin_key)])
async def dismiss_strategy_signal(signal_id: int, db: Session = Depends(get_db)):
    """Marks a signal reviewed-and-declined. No order-related side effect — purely bookkeeping."""
    try:
        record = StrategySignalService(db).dismiss(signal_id)
        return record.to_dict()
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/v1/external-signals/poll", dependencies=[Depends(verify_admin_key)])
async def poll_external_signals_now(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
):
    """
    Triggers one immediate poll of EDGE-TF's execution gateway instead of
    waiting for the next edge_tf_poll_interval_sec tick. Same effect as an
    autonomous pass — records any newly-approved trades, places no orders.
    """
    if not settings.edge_tf_connector_enabled:
        raise HTTPException(status_code=400, detail="EDGE-TF connector is disabled (edge_tf_connector_enabled=false)")
    try:
        new_count = await edge_tf_poll_once(db, settings)
        return {"new_signals": new_count}
    except Exception as e:
        logger.error("edge_tf_poll_all_error", error=str(e))
        raise HTTPException(status_code=502, detail=f"EDGE-TF poll failed: {e}")


@app.get("/v1/external-signals", dependencies=[Depends(verify_admin_key)])
async def list_external_signals(
    status: Optional[str] = Query(default=ExternalSignalStatus.PENDING),
    limit: int = 50,
    db: Session = Depends(get_db),
):
    """
    Trades EDGE-TF (or, in future, another external decision engine) has
    already approved on its own side — the human-review queue for signals
    that didn't originate from this system's own strategy catalog. Defaults
    to PENDING; pass status=DISMISSED/CLAIMED/EXECUTED/FAILED or omit status
    entirely for the full history.
    """
    records = ExternalSignalService(db).list_signals(status=status or None, limit=limit)
    return {"signals": [r.to_dict() for r in records]}


@app.post("/v1/external-signals/{trade_id}/load", dependencies=[Depends(verify_admin_key)])
async def load_external_signal(
    trade_id: str,
    account: str = Query(..., description="Account alias to execute this trade against"),
    agent_id: str = Query(default="default"),
    quantity: Optional[int] = Query(
        default=None,
        description="Required for a signal its source didn't size itself (e.g. ep-edge-earnings); ignored otherwise.",
    ),
    db: Session = Depends(get_db),
):
    """
    Shapes one PENDING external signal as a TradeProposal payload
    (decision_id set to the upstream trade_id) ready to hand to
    POST /v1/orders/preview — this does not preview or execute anything
    itself, it only saves the operator from hand-transcribing the signal.
    """
    record = ExternalSignalService(db).get_by_trade_id(trade_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"External signal {trade_id} not found")
    if record.status != ExternalSignalStatus.PENDING:
        raise HTTPException(status_code=409, detail=f"External signal {trade_id} is {record.status}, not PENDING")
    try:
        settings = get_settings()
        settings.get_account_profile(account)  # fail closed on an unknown alias before handing this back
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    try:
        proposal = record.to_trade_proposal_dict(account=account, agent_id=agent_id, quantity=quantity)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"proposal": proposal}


@app.post("/v1/external-signals/ingest", dependencies=[Depends(verify_admin_key)])
async def ingest_external_signals(
    request: EarningsIngestRequest,
    db: Session = Depends(get_db),
):
    """
    Push-based counterpart to /v1/external-signals/poll: for a source with
    no HTTP gateway of its own to poll (today: ep-edge-earnings and
    hedge-engine, both libraries with no service and no claim/report
    lifecycle — see src/execution/ep_edge_earnings_adapter.py and
    src/execution/hedge_engine_adapter.py), whoever runs that workflow
    POSTs the resulting candidates/decisions here instead. Same effect
    either way: records signals for human review in GET
    /v1/external-signals, places no orders.
    """
    if request.source == EpEdgeSource:
        new_count = ep_edge_record_batch(db, request.candidates)
        return {"new_signals": new_count, "source": request.source}
    if request.source == HedgeSource:
        new_count = hedge_record_batch(db, request.candidates)
        return {"new_signals": new_count, "source": request.source}
    raise HTTPException(status_code=400, detail=f"Unknown or unsupported ingestion source: {request.source}")


@app.post("/v1/external-signals/{trade_id}/dismiss", dependencies=[Depends(verify_admin_key)])
async def dismiss_external_signal(trade_id: str, db: Session = Depends(get_db)):
    """Marks an external signal reviewed-and-declined. No upstream call, no order-related side effect."""
    record = ExternalSignalService(db).get_by_trade_id(trade_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"External signal {trade_id} not found")
    updated = ExternalSignalService(db).mark_status(trade_id, ExternalSignalStatus.DISMISSED)
    return updated.to_dict()


@app.post("/v1/backtest/run", dependencies=[Depends(verify_admin_key)])
async def run_backtest_endpoint(request: BacktestRequest):
    """
    Backtests the exact live rules src/execution/autonomous_trader.py runs
    (same strategy.evaluate() functions, same standardized
    compute_standardized_exit()/size_position()) against real historical
    daily bars (yfinance — see src/backtest/data_source.py), not the
    synthetic PaperBrokerAdapter series used for live paper trading.

    Read-only and has nothing to do with the order/risk/kill-switch
    pipeline — it never previews, approves, or executes anything, it only
    fetches market data and reports what the rules would have done with
    it. Fetches real network data per (symbol, strategy_id) pair, so
    symbols/date-range are capped (see src/backtest/api_models.py).
    """
    unknown_strategies = [s for s in request.strategy_ids if s not in STRATEGIES]
    if unknown_strategies:
        raise HTTPException(status_code=400, detail=f"Unknown strategy_id(s): {unknown_strategies}")

    try:
        results, errors = await run_backtest_suite(
            request.symbols,
            request.strategy_ids,
            request.start_date,
            request.end_date,
            risk_pct=request.risk_pct,
            reward_risk_ratio=request.reward_risk_ratio,
            notional_per_trade_usd=request.notional_per_trade_usd,
            starting_capital=request.starting_capital,
            slippage_bps=request.slippage_bps,
            commission_per_order_usd=request.commission_per_order_usd,
        )
    except Exception as e:
        logger.error("backtest_run_error", error=str(e))
        raise HTTPException(status_code=502, detail=f"Backtest failed: {e}")

    return {
        "results": [r.to_dict() for r in results],
        "errors": errors,
        "summary": summarize_suite(results, combined_starting_capital=request.starting_capital),
    }


@app.get("/v1/autonomous/status", dependencies=[Depends(verify_admin_key)])
async def autonomous_status(settings: Settings = Depends(get_settings_dep), db: Session = Depends(get_db)):
    """
    Read-only configuration snapshot for the autonomous trader
    (src/execution/autonomous_trader.py) — whether it's enabled, which
    strategies/symbols it's watching, and the standardized risk:reward it's
    using. To actually stop it: either autonomous_trading_enabled=false, or
    POST /v1/kill-switch/agents/{autonomous_agent_id}/on for an immediate
    halt without a redeploy.

    "strategy_ids"/"notional_per_trade_usd" here are the static settings
    fallback only — active_plan (see GET /v1/autonomous/plan) is what
    scan_for_entries() actually uses when present, and takes priority.
    """
    active_plan = DailyPlanService(db).get_active_plan()
    return {
        "enabled": settings.autonomous_trading_enabled,
        "agent_id": settings.autonomous_agent_id,
        "account": settings.autonomous_account,
        "strategy_ids": settings.autonomous_strategy_ids,
        "watchlist": settings.autonomous_watchlist,
        "risk_pct": str(settings.autonomous_risk_pct),
        "reward_risk_ratio": str(settings.autonomous_reward_risk_ratio),
        "notional_per_trade_usd": str(settings.autonomous_notional_per_trade_usd),
        "scan_interval_sec": settings.autonomous_scan_interval_sec,
        "broker": "PAPER (hard-coded, cannot be changed via settings)",
        "llm_narration_enabled": settings.llm_narration_enabled,
        "active_plan": active_plan.to_dict() if active_plan else None,
    }


@app.post("/v1/autonomous/run-once", dependencies=[Depends(verify_admin_key)])
async def autonomous_run_once(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
):
    """
    Triggers one immediate autonomous cycle (manage open positions, then
    scan for new entries) instead of waiting for the next
    autonomous_scan_interval_sec tick — same effect as an autonomous pass,
    for testing/demo without waiting. Still requires
    autonomous_trading_enabled=true; still submits real (paper) orders.
    """
    if not settings.autonomous_trading_enabled:
        raise HTTPException(status_code=400, detail="Autonomous trading is disabled (autonomous_trading_enabled=false)")
    try:
        return await autonomous_cycle_once(db, settings)
    except Exception as e:
        logger.error("autonomous_run_once_error", error=str(e))
        raise HTTPException(status_code=500, detail=f"Autonomous cycle failed: {e}")


@app.get("/v1/autonomous/positions", dependencies=[Depends(verify_admin_key)])
async def list_autonomous_positions(
    status: Optional[str] = Query(default=None, description="Filter by OPEN/CLOSED_TARGET/CLOSED_STOP/CLOSED_ERROR; omit for all."),
    limit: int = 100,
    db: Session = Depends(get_db),
):
    """Every position the autonomous trader has opened, most recent first, with its entry/exit rationale."""
    records = AutonomousPositionService(db).list_all(limit=limit)
    if status:
        records = [r for r in records if r.status == status]
    return {"positions": [r.to_dict() for r in records]}


def _validate_daily_bar_strategy_ids(strategy_ids: list[str]) -> None:
    """Shared by /arm and /start — every strategy_id must be a real, daily-bar strategy in the catalog. An intraday one (ORB, VWAP Reversion) can't be armed since this system never backtests or ranks those against daily data."""
    unknown = [s for s in strategy_ids if s not in STRATEGIES]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown strategy_id(s): {unknown}")
    non_daily = [s for s in strategy_ids if STRATEGIES[s].bar_interval != "daily"]
    if non_daily:
        raise HTTPException(status_code=400, detail=f"Only daily-bar strategies can be armed here: {non_daily}")


@app.post("/v1/autonomous/rank-strategies", dependencies=[Depends(verify_admin_key)])
async def rank_strategies(
    request: RankStrategiesRequest,
    settings: Settings = Depends(get_settings_dep),
):
    """
    Ranks every daily-bar strategy by real performance over a recent
    rolling window (see src/execution/strategy_ranking.py for exactly how
    — real network data per symbol, so this can take up to a minute).
    Read-only: computes and returns a ranking plus a suggested top_n
    picks, does not arm or execute anything. Most callers want
    POST /v1/autonomous/start instead, which does this and arms the
    result in one step; this exists separately for inspecting the
    ranking on its own.
    """
    symbols = request.symbols or settings.autonomous_watchlist
    try:
        ranking = await rank_strategies_by_recent_performance(
            symbols,
            lookback_days=request.lookback_days,
            top_n=request.top_n,
            notional_per_trade_usd=request.notional_per_trade_usd,
            risk_pct=request.risk_pct,
            reward_risk_ratio=request.reward_risk_ratio,
        )
        return ranking.to_dict()
    except Exception as e:
        logger.error("rank_strategies_error", error=str(e))
        raise HTTPException(status_code=502, detail=f"Ranking failed: {e}")


@app.post("/v1/autonomous/start", dependencies=[Depends(verify_admin_key)])
async def start_agent(
    request: StartAgentRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
):
    """
    The one-time "take this money and trade it until I say stop" action:
    ranks recent performance, arms the top picks at
    request.notional_per_trade_usd, and from then on requires no further
    confirmation — src/execution/strategy_rotation.py's background loop
    keeps the strategy selection current on its own. Replaces any
    previously active plan. To actually stop: POST /v1/autonomous/disarm
    (positions already open keep being managed either way).
    """
    symbols = request.symbols or settings.autonomous_watchlist
    try:
        ranking = await rank_strategies_by_recent_performance(
            symbols,
            notional_per_trade_usd=request.notional_per_trade_usd,
        )
    except Exception as e:
        logger.error("start_agent_ranking_error", error=str(e))
        raise HTTPException(status_code=502, detail=f"Could not rank strategies: {e}")

    if not ranking.top_picks:
        raise HTTPException(
            status_code=422,
            detail="No strategy fired on your watchlist in the last 90 days — nothing to start with. Try again another day, or arm a specific strategy directly via /v1/autonomous/arm.",
        )

    plan = DailyPlanService(db).arm(ranking.top_picks, request.notional_per_trade_usd, request.started_by)
    return {"plan": plan.to_dict(), "ranking": ranking.to_dict()}


@app.post("/v1/autonomous/arm", dependencies=[Depends(verify_admin_key)])
async def arm_daily_plan(
    request: ArmPlanRequest,
    db: Session = Depends(get_db),
):
    """
    Lower-level than /start: arms an explicit strategy_ids list directly
    (skips ranking). Runs until disarmed (POST /v1/autonomous/disarm) —
    no expiry. Replaces any previously active plan; does not touch
    positions already open.
    """
    _validate_daily_bar_strategy_ids(request.strategy_ids)
    try:
        plan = DailyPlanService(db).arm(request.strategy_ids, request.notional_per_trade_usd, request.armed_by)
        return plan.to_dict()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/v1/autonomous/disarm", dependencies=[Depends(verify_admin_key)])
async def disarm_daily_plan(
    request: DisarmPlanRequest,
    db: Session = Depends(get_db),
):
    """Stops new autonomous entries immediately — this is the "stop the trade" action. Positions already open are untouched and keep being managed normally."""
    plan = DailyPlanService(db).disarm(request.disarmed_by)
    return {"was_active": plan is not None, "plan": plan.to_dict() if plan else None}


@app.get("/v1/autonomous/plan", dependencies=[Depends(verify_admin_key)])
async def get_daily_plan(db: Session = Depends(get_db)):
    """The currently active plan, or null if not armed."""
    plan = DailyPlanService(db).get_active_plan()
    return {"active_plan": plan.to_dict() if plan else None}


@app.post("/v1/autonomous/rotate-now", dependencies=[Depends(verify_admin_key)])
async def rotate_strategies_now(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
):
    """
    Triggers one immediate strategy rotation instead of waiting for the
    next daily tick (src/execution/strategy_rotation.py) — for
    testing/demo. A no-op (rotated=false) when nothing is currently
    armed; there's nothing to rotate.
    """
    try:
        rotated = await rotate_once(db, settings)
    except Exception as e:
        logger.error("rotate_now_error", error=str(e))
        raise HTTPException(status_code=502, detail=f"Rotation failed: {e}")
    plan = DailyPlanService(db).get_active_plan()
    return {"rotated": rotated, "plan": plan.to_dict() if plan else None}


if __name__ == "__main__":
    import uvicorn
    
    settings = get_settings()
    uvicorn.run(
        "src.api.server:app",
        host=settings.host,
        port=settings.port,
        reload=(settings.env == "development"),
    )
