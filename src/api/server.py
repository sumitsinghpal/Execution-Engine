from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlmodel import Session, select

from src.config import get_settings
from src.db import get_session, init_db
from src.execution.executor import ExecutionServiceError, execute_trade, order_status, preview_trade
from src.models.orders import ExecuteOrderRequest, KillSwitchState, TradeProposal

settings = get_settings()
app = FastAPI(title="Schwab Execution Engine", version="1.0.0")


@app.on_event("startup")
def on_startup() -> None:
    init_db()


@app.exception_handler(ExecutionServiceError)
async def handle_execution_error(_: Request, exc: ExecutionServiceError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
    )


def admin_auth(x_api_key: str = Header(default="")) -> None:
    if x_api_key != settings.admin_api_key:
        raise HTTPException(status_code=403, detail="forbidden")


@app.get(f"{settings.api_prefix}/health")
def health() -> dict:
    return {
        "status": "ok",
        "time": datetime.now(UTC).isoformat(),
        "mock_broker_mode": settings.schwab_mock_mode,
    }


@app.post(f"{settings.api_prefix}/orders/preview")
async def preview_endpoint(proposal: TradeProposal, session: Session = Depends(get_session)) -> dict:
    correlation_id = str(uuid4())
    response = await preview_trade(session, proposal, correlation_id)
    session.commit()
    return response


@app.post(f"{settings.api_prefix}/orders/execute")
async def execute_endpoint(request: ExecuteOrderRequest, session: Session = Depends(get_session)) -> dict:
    correlation_id = str(uuid4())
    response = await execute_trade(session, request, correlation_id)
    session.commit()
    return response


@app.get(f"{settings.api_prefix}/orders/{{decision_id}}")
def get_order(decision_id: str, session: Session = Depends(get_session)) -> dict:
    return order_status(session, decision_id)


@app.post(f"{settings.api_prefix}/kill-switch/on", dependencies=[Depends(admin_auth)])
def kill_switch_on(session: Session = Depends(get_session)) -> dict:
    state = session.exec(select(KillSwitchState).where(KillSwitchState.id == 1)).first()
    if state is None:
        state = KillSwitchState(id=1, is_on=True)
    state.is_on = True
    state.updated_at = datetime.now(UTC)
    session.add(state)
    session.commit()
    return {"kill_switch": "ON"}


@app.post(f"{settings.api_prefix}/kill-switch/off", dependencies=[Depends(admin_auth)])
def kill_switch_off(session: Session = Depends(get_session)) -> dict:
    state = session.exec(select(KillSwitchState).where(KillSwitchState.id == 1)).first()
    if state is None:
        state = KillSwitchState(id=1, is_on=False)
    state.is_on = False
    state.updated_at = datetime.now(UTC)
    session.add(state)
    session.commit()
    return {"kill_switch": "OFF"}
