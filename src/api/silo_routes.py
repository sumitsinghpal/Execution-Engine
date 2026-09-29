"""
HedgeHog Silo control API. Arming a mode="AUTONOMOUS_LIVE" mandate here
authorizes real order routing (see src/execution/autonomous_trader.py's
_build_broker docstring for the full independent-switch chain) -- it does
not by itself enable anything; every deployment-level switch still
defaults off. /status now requires the admin key, same as /arm and
/disarm, since it exposes account_alias and other authorization details
once a live-capable mandate is armed.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Header, HTTPException

from src.config import get_settings
from src.execution.silo_runtime import SiloMandate, SiloRuntime

router = APIRouter(prefix="/v1/silo", tags=["silo"])


def _verify(x_admin_key: Optional[str]) -> None:
    if not x_admin_key or x_admin_key != get_settings().api_key_admin:
        raise HTTPException(status_code=403, detail="Invalid or missing admin key")


@router.get("/status")
def silo_status(x_admin_key: Optional[str] = Header(None)):
    _verify(x_admin_key)
    runtime = SiloRuntime()
    active = runtime.active_mandate(admin_key=x_admin_key)
    return {"armed": active is not None, "mandate": active.model_dump(mode="json") if active else None}


@router.post("/arm")
def silo_arm(mandate: SiloMandate, x_admin_key: Optional[str] = Header(None)):
    _verify(x_admin_key)
    armed = SiloRuntime().arm(mandate, admin_key=x_admin_key)
    return {"armed": True, "mandate": armed.model_dump(mode="json")}


@router.post("/disarm")
def silo_disarm(x_admin_key: Optional[str] = Header(None)):
    _verify(x_admin_key)
    prior = SiloRuntime().disarm()
    return {"armed": False, "prior_silo_id": prior.silo_id if prior else None}
