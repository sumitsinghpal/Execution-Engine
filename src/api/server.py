"""
FastAPI application and route handlers.
External contract for EDGE-TF integration.
"""

from datetime import datetime
from typing import Optional
import uuid

from fastapi import FastAPI, Depends, HTTPException, Header, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.brokers.base import BrokerAuthenticationError
from src.config import get_settings, Settings
from src.database import SessionLocal, init_db
from src.execution.executor import Executor
from src.execution.kill_switch_state import KillSwitchService
from src.execution.position_reconciliation import PositionReconciliationService
from src.execution.reconciliation import ReconciliationService
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


@app.on_event("startup")
async def startup_event():
    """Initialize database on startup."""
    configure_logging(get_settings().log_level, get_settings().log_format)
    init_db()
    logger.info("startup_complete")


@app.get("/v1/health")
async def health_check(db: Session = Depends(get_db)) -> HealthStatus:
    """
    Health check endpoint.
    Returns service and dependency status.
    """
    
    # Check database
    db_status = "ok"
    try:
        db.execute(text("SELECT 1"))
    except Exception as e:
        db_status = "error"
        logger.error("database_health_check_failed", error=str(e))
    
    # Check broker connectivity (would make real call in production)
    broker_status = "untested"  # In mock mode
    
    return HealthStatus(
        status="healthy" if db_status == "ok" else "degraded",
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
    except Exception as e:
        logger.error("execute_error", decision_id=request.decision_id, error=str(e))
        raise HTTPException(status_code=500, detail="Execution failed")


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


@app.get("/v1/market-status")
async def get_market_status():
    """Query current US market hours status."""
    validator = MarketHoursValidator()
    return validator.get_market_status()


@app.post("/v1/reconciliation/positions")
async def reconcile_positions(
    account: str = "primary",
    db: Session = Depends(get_db),
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
        service = PositionReconciliationService(session=db)
        report = await service.reconcile_or_halt(account, halted_by="reconciliation_check")
        return report.to_dict()
    except Exception as e:
        logger.error("position_reconciliation_error", account=account, error=str(e))
        raise HTTPException(status_code=500, detail=f"Position reconciliation failed: {e}")


if __name__ == "__main__":
    import uvicorn
    
    settings = get_settings()
    uvicorn.run(
        "src.api.server:app",
        host=settings.host,
        port=settings.port,
        reload=(settings.env == "development"),
    )
