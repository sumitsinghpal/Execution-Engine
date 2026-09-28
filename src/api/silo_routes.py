"""HedgeHog Silo control API. This does not enable live autonomous routing."""
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
def silo_status():
    runtime = SiloRuntime()
    active = runtime.active_mandate()
    return {"armed": active is not None, "mandate": active.model_dump(mode="json") if active else None}


@router.post("/arm")
def silo_arm(mandate: SiloMandate, x_admin_key: Optional[str] = Header(None)):
    _verify(x_admin_key)
    armed = SiloRuntime().arm(mandate)
    return {"armed": True, "mandate": armed.model_dump(mode="json")}


@router.post("/disarm")
def silo_disarm(x_admin_key: Optional[str] = Header(None)):
    _verify(x_admin_key)
    prior = SiloRuntime().disarm()
    return {"armed": False, "prior_silo_id": prior.silo_id if prior else None}
