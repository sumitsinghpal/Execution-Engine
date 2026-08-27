"""
FastAPI application and route handlers.
External contract for EDGE-TF integration.
"""

import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Optional
import uuid

from fastapi import FastAPI, Depends, HTTPException, Header, Path, Query, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.orm import Session
from sqlmodel import select

from src.brokers.base import BrokerAPIOutageError, BrokerAuthenticationError
from src.brokers.factory import build_broker_adapter
from src.config import get_settings, Settings
from src.database import SessionLocal, init_db
from src.execution.agent_exposure_guard import AgentExposureGuard
from src.execution.algo_slices import AlgoSliceRecord
from src.execution.drawdown_guard import DrawdownGuard
from src.execution.executor import Executor, OrderRecord
from src.execution.kill_switch_state import GLOBAL_SCOPE, KillSwitchService
from src.execution.position_reconciliation import PositionReconciliationService
from src.execution.reconciliation import ReconciliationService
from src.execution.strategy_scanner import run_scanner_loop, scan_once
from src.execution.strategy_signals import SignalStatus, StrategySignalService
from src.execution.symbol_coordination import SymbolCoordinationGuard
from src.logging_config import configure_logging, get_logger
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


@app.on_event("startup")
async def startup_event():
    """Initialize database on startup."""
    configure_logging(get_settings().log_level, get_settings().log_format)
    init_db()

    global _scanner_stop_event, _scanner_task
    _scanner_stop_event = asyncio.Event()
    _scanner_task = asyncio.create_task(run_scanner_loop(SessionLocal, get_settings, _scanner_stop_event))

    logger.info("startup_complete")


@app.on_event("shutdown")
async def shutdown_event():
    """Stop the background strategy scanner cleanly."""
    if _scanner_stop_event is not None:
        _scanner_stop_event.set()
    if _scanner_task is not None:
        await asyncio.wait_for(_scanner_task, timeout=10)


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


@app.post("/v1/orders/preview", response_model=OrderPreview)
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


@app.post("/v1/orders/execute", response_model=ExecutionReceipt)
async def execute_order(
    request: ExecutionRequest,
    db: Session = Depends(get_db),
) -> ExecutionReceipt:
    """
    Execute a previously-approved order.
    
    Requires:
    - decision_id from original proposal
    - preview_id from preview response
    - approval artifact (approved_by, approved_at, attestation)
    - idempotency_key for safety
    
    Approval gates, kill switch, and risk checks are enforced server-side.
    """
    
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
        
        return receipt
        
    except ValueError as e:
        logger.error("execute_validation_error", decision_id=request.decision_id, error=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except BrokerAuthenticationError as e:
        logger.critical("execute_broker_auth_failed", decision_id=request.decision_id, error=str(e))
        raise HTTPException(
            status_code=503,
            detail=f"Broker authentication failed; trading halted pending re-authentication: {e}",
        )
    except BrokerAPIOutageError as e:
        logger.critical("execute_broker_api_outage", decision_id=request.decision_id, error=str(e))
        raise HTTPException(status_code=503, detail=f"Broker API unreachable, try again shortly: {e}")
    except Exception as e:
        logger.error("execute_error", decision_id=request.decision_id, error=str(e))
        raise HTTPException(status_code=500, detail="Execution failed")


@app.get("/v1/orders")
async def list_orders(
    account: Optional[str] = None,
    agent_id: Optional[str] = None,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    """
    Recent orders, most recent first — read-only, no side effects (unlike
    /v1/reconciliation/positions, which can trip the kill switch). Backs an
    activity feed / dashboard rather than a single decision_id lookup.
    """
    limit = max(1, min(limit, 200))
    stmt = select(OrderRecord)
    if account:
        stmt = stmt.where(OrderRecord.account == account)
    if agent_id:
        stmt = stmt.where(OrderRecord.agent_id == agent_id)
    stmt = stmt.order_by(OrderRecord.created_at.desc()).limit(limit)

    orders = db.exec(stmt).all()
    return {
        "orders": [
            {
                "decision_id": o.decision_id,
                "agent_id": o.agent_id,
                "account": o.account,
                "symbol": o.symbol,
                "instruction": o.instruction,
                "quantity": o.quantity,
                "order_type": o.order_type,
                "limit_price": o.limit_price,
                "stop_price": o.stop_price,
                "status": o.status,
                "risk_approved": o.risk_approved,
                "estimated_notional_usd": o.estimated_notional_usd,
                "filled_quantity": o.filled_quantity,
                "broker_status": o.broker_status,
                "strategy_id": o.strategy_id,
                "strategy_stop_loss_price": o.strategy_stop_loss_price,
                "strategy_take_profit_price": o.strategy_take_profit_price,
                "created_at": o.created_at,
                "updated_at": o.updated_at,
            }
            for o in orders
        ]
    }


@app.get("/v1/account/{account}/balances")
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


@app.get("/v1/account/{account}/positions")
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


@app.get("/v1/orders/{decision_id}", response_model=OrderStatus_Model)
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


@app.get("/v1/orders/{decision_id}/slices")
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


@app.post("/v1/reconciliation/positions")
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


@app.post("/v1/risk/drawdown-check")
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


@app.post("/v1/risk/agent-exposure-check")
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


@app.get("/v1/risk/symbol-exposure")
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


@app.post("/v1/strategies/scan-all")
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


@app.post("/v1/strategies/{strategy_id}/scan")
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


@app.get("/v1/strategies/signals")
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


@app.post("/v1/strategies/signals/{signal_id}/dismiss")
async def dismiss_strategy_signal(signal_id: int, db: Session = Depends(get_db)):
    """Marks a signal reviewed-and-declined. No order-related side effect — purely bookkeeping."""
    try:
        record = StrategySignalService(db).dismiss(signal_id)
        return record.to_dict()
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    
    settings = get_settings()
    uvicorn.run(
        "src.api.server:app",
        host=settings.host,
        port=settings.port,
        reload=(settings.env == "development"),
    )
