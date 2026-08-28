"""Request/response shapes for the multi-leg options endpoints (see src/api/server.py and src/execution/multi_leg.py)."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from src.models.orders import AssetType, Instruction, OrderType, TradeProposal

MAX_LEGS = 2  # see src/execution/multi_leg.py's module docstring for why this is scoped to 2-leg combos


class MultiLegLegRequest(BaseModel):
    """One leg of a combo — the same shape as a normal single-leg options order, minus asset_type/order_type defaults that are fixed for every leg."""

    decision_id: str = Field(..., description="Unique decision_id for THIS leg — each leg is still its own independently tracked order")
    agent_id: str = Field(default="default")
    account: str
    symbol: str = Field(..., description="Full 21-character OCC option symbol for this leg")
    instruction: Instruction
    quantity: int = Field(..., gt=0)
    order_type: OrderType = OrderType.MARKET
    limit_price: Optional[float] = None
    strategy_id: Optional[str] = None

    model_config = {"extra": "forbid"}

    def to_trade_proposal(self) -> TradeProposal:
        return TradeProposal(
            decision_id=self.decision_id,
            agent_id=self.agent_id,
            account=self.account,
            symbol=self.symbol,
            asset_type=AssetType.OPTION,
            instruction=self.instruction,
            quantity=self.quantity,
            order_type=self.order_type,
            limit_price=self.limit_price,
            strategy_id=self.strategy_id,
        )


class MultiLegPreviewRequest(BaseModel):
    combo_type: Literal["vertical_spread", "straddle", "strangle", "custom"] = "custom"
    legs: list[MultiLegLegRequest] = Field(..., min_length=MAX_LEGS, max_length=MAX_LEGS)

    model_config = {"extra": "forbid"}


class MultiLegExecuteLegRef(BaseModel):
    """References an already-previewed leg by (decision_id, preview_id) — same two fields a normal single-leg /v1/orders/execute call needs, since Executor.execute_order rebuilds everything else from the persisted preview."""

    decision_id: str
    preview_id: str

    model_config = {"extra": "forbid"}


class MultiLegExecuteRequest(BaseModel):
    combo_id: str = Field(..., description="combo_id returned by the preview call")
    legs: list[MultiLegExecuteLegRef] = Field(..., min_length=MAX_LEGS, max_length=MAX_LEGS)
    approved_by: str
    attestation: str

    model_config = {"extra": "forbid"}


__all__ = [
    "MAX_LEGS",
    "MultiLegExecuteLegRef",
    "MultiLegExecuteRequest",
    "MultiLegLegRequest",
    "MultiLegPreviewRequest",
]
