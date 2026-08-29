"""Request shape for the bracket-order attach endpoint (see src/api/server.py, src/execution/bracket_orders.py)."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, model_validator


class AttachBracketOrderRequest(BaseModel):
    entry_decision_id: str = Field(..., min_length=1)
    stop_loss_price: Optional[float] = Field(default=None, gt=0)
    take_profit_price: Optional[float] = Field(default=None, gt=0)
    # e.g. 0.05 for a 5% trailing stop. Bounded well short of 1 (100%) —
    # a trailing stop that wide is not protective, it's a typo.
    trailing_stop_pct: Optional[float] = Field(default=None, gt=0, lt=0.9)
    created_by: str = Field(..., min_length=1)

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _require_a_stop(self) -> "AttachBracketOrderRequest":
        if self.stop_loss_price is None and self.trailing_stop_pct is None:
            raise ValueError("A bracket needs at least one of stop_loss_price or trailing_stop_pct")
        return self


__all__ = ["AttachBracketOrderRequest"]
