"""Request shapes for the price-alert endpoints (see src/api/server.py, src/execution/price_alerts.py)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class CreatePriceAlertRequest(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=10)
    condition: str = Field(..., description="ABOVE or BELOW")
    target_price: float = Field(..., gt=0)
    created_by: str = Field(..., min_length=1)

    model_config = {"extra": "forbid"}


__all__ = ["CreatePriceAlertRequest"]
