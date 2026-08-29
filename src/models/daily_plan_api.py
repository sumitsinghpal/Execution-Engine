"""Request shapes for the daily-plan endpoints (see src/api/server.py, src/execution/strategy_ranking.py, src/execution/daily_plan.py)."""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field

from src.execution.daily_plan import DEFAULT_PLAN_TTL_HOURS
from src.execution.strategy_ranking import DEFAULT_LOOKBACK_DAYS, DEFAULT_TOP_N


class RankStrategiesRequest(BaseModel):
    symbols: Optional[list[str]] = Field(default=None, description="Defaults to settings.autonomous_watchlist when omitted")
    lookback_days: int = Field(default=DEFAULT_LOOKBACK_DAYS, ge=30, le=365)
    top_n: int = Field(default=DEFAULT_TOP_N, ge=1, le=10)
    notional_per_trade_usd: Decimal = Decimal("1000")
    risk_pct: Decimal = Decimal("0.01")
    reward_risk_ratio: Decimal = Decimal("2")

    model_config = {"extra": "forbid"}


class ArmPlanRequest(BaseModel):
    strategy_ids: list[str] = Field(..., min_length=1, max_length=10)
    notional_per_trade_usd: Decimal = Field(..., gt=0)
    armed_by: str = Field(..., min_length=1)
    ttl_hours: float = Field(default=DEFAULT_PLAN_TTL_HOURS, gt=0, le=168)

    model_config = {"extra": "forbid"}


class DisarmPlanRequest(BaseModel):
    disarmed_by: str = Field(..., min_length=1)

    model_config = {"extra": "forbid"}


__all__ = ["ArmPlanRequest", "DisarmPlanRequest", "RankStrategiesRequest"]
