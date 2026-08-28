"""
Multi-leg options combos — vertical spreads, straddles, strangles — as ONE
logical trade rather than N independent single-leg orders that don't know
about each other. Single-leg OPTION orders already flow through the
normal TradeProposal/Executor pipeline unchanged (asset_type=OPTION with
a full 21-char OCC symbol — see src/models/orders.py and
src/models/occ_symbol.py); what was missing there is any notion that two
legs make up one position with one combined risk figure, previewed and
executed together.

Scoped to exactly 2 legs (vertical spread, straddle, strangle, or an
unvalidated "custom" pair) rather than a fully generic N-leg builder —
real combos beyond 2 legs (iron condors, butterflies, etc.) are rare
enough outside a dedicated options desk that a correct, tested 2-leg
implementation is worth more than a half-tested generic one.

Execution is sequential (leg 1 then leg 2) through the SAME Executor
every other order in this system goes through — not a true broker-side
combo/multi-leg order, which src/broker/order_builder.py doesn't build.
That means a real, if narrow, execution risk this module has to own
honestly rather than paper over: leg 1 can fill while leg 2 fails,
leaving a naked single-leg position open and risked as if it were still
hedged. Rather than retry silently, a leg failure after at least one
other leg already executed immediately trips THIS agent's kill switch
(scope = the leg's own agent_id) — an unhedged leg is exactly the kind
of runaway risk the kill switch exists to stop, and because every trip
goes through KillSwitchService.set_state, this also fires an outbound
webhook notification for free (see src/notifications/webhook.py).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Optional

from src.execution.executor import Executor
from src.execution.kill_switch_state import KillSwitchService
from src.logging_config import get_logger
from src.models.occ_symbol import parse_occ_symbol
from src.models.orders import AssetType, Instruction, TradeProposal

logger = get_logger(__name__)

ComboType = Literal["vertical_spread", "straddle", "strangle", "custom"]
_COMBO_TYPES = ("vertical_spread", "straddle", "strangle", "custom")


@dataclass
class LegRef:
    """
    Enough to execute one leg without needing the original TradeProposal
    object back — decision_id/preview_id are what Executor.execute_order
    actually needs (it rebuilds the rest from the persisted OrderRecord);
    agent_id/symbol ride along only for logging and for scoping the kill
    switch trip if this leg's execution fails after an earlier one already
    went through. Deliberately decoupled from LegPreview/MultiLegPreview:
    a combo's preview and execute calls can be two separate HTTP requests
    (see /v1/orders/multi-leg/execute), so execute must be able to build
    this from persisted state, not just from an in-memory preview result.
    """

    decision_id: str
    preview_id: str
    agent_id: str
    symbol: str


@dataclass
class LegPreview:
    leg: TradeProposal
    preview_id: str
    risk_verdict: str
    risk_details: dict
    estimated_cost: float

    def to_leg_ref(self) -> LegRef:
        return LegRef(
            decision_id=self.leg.decision_id, preview_id=self.preview_id,
            agent_id=self.leg.agent_id, symbol=self.leg.symbol,
        )


@dataclass
class MultiLegPreview:
    combo_id: str
    combo_type: ComboType
    legs: list[LegPreview]
    net_debit_or_credit_usd: float  # positive = net debit (you pay); negative = net credit (you receive)
    max_loss_usd: Optional[float]
    max_profit_usd: Optional[float]
    risk_verdict: str  # APPROVED only if every leg was individually APPROVED

    def to_dict(self) -> dict:
        return {
            "combo_id": self.combo_id,
            "combo_type": self.combo_type,
            "risk_verdict": self.risk_verdict,
            "net_debit_or_credit_usd": self.net_debit_or_credit_usd,
            "max_loss_usd": self.max_loss_usd,
            "max_profit_usd": self.max_profit_usd,
            "legs": [
                {
                    "decision_id": lp.leg.decision_id,
                    "agent_id": lp.leg.agent_id,
                    "symbol": lp.leg.symbol,
                    "instruction": lp.leg.instruction.value,
                    "quantity": lp.leg.quantity,
                    "preview_id": lp.preview_id,
                    "risk_verdict": lp.risk_verdict,
                    "risk_details": lp.risk_details,
                    "estimated_cost_usd": lp.estimated_cost,
                }
                for lp in self.legs
            ],
        }


@dataclass
class MultiLegExecutionResult:
    combo_id: str
    executed_legs: list[dict] = field(default_factory=list)
    failed_leg_index: Optional[int] = None
    error: Optional[str] = None

    @property
    def fully_executed(self) -> bool:
        return self.failed_leg_index is None and self.error is None

    def to_dict(self) -> dict:
        return {
            "combo_id": self.combo_id,
            "fully_executed": self.fully_executed,
            "executed_legs": self.executed_legs,
            "failed_leg_index": self.failed_leg_index,
            "error": self.error,
        }


def validate_combo_structure(combo_type: ComboType, legs: list[TradeProposal]) -> None:
    """
    Raises ValueError on a structurally invalid combo — e.g. a "straddle"
    with mismatched strikes, or a "vertical_spread" mixing calls and puts.
    "custom" skips the shape checks below and accepts any 2 same-underlying,
    same-quantity option legs, for combos that don't fit one of the three
    named shapes.
    """
    if combo_type not in _COMBO_TYPES:
        raise ValueError(f"Unknown combo_type {combo_type!r}; must be one of {_COMBO_TYPES}")
    if len(legs) != 2:
        raise ValueError(f"Multi-leg combos currently support exactly 2 legs, got {len(legs)}")
    for leg in legs:
        if leg.asset_type != AssetType.OPTION:
            raise ValueError(f"Every leg must be asset_type=OPTION, got {leg.asset_type.value} for {leg.symbol}")

    a, b = (parse_occ_symbol(leg.symbol) for leg in legs)
    if a.underlying != b.underlying:
        raise ValueError(f"Both legs must share the same underlying, got {a.underlying!r} and {b.underlying!r}")
    if legs[0].quantity != legs[1].quantity:
        raise ValueError("Both legs must trade the same quantity — this system only supports 1:1 ratio combos")
    if legs[0].symbol == legs[1].symbol:
        raise ValueError("Both legs resolve to the identical option contract — not a combo")

    if combo_type == "custom":
        return

    if combo_type == "vertical_spread":
        if a.right != b.right:
            raise ValueError("A vertical spread requires both legs to be the same right (both calls or both puts)")
        if a.expiration != b.expiration:
            raise ValueError("A vertical spread requires both legs to share the same expiration")
        if a.strike == b.strike:
            raise ValueError("A vertical spread requires two different strikes")
        if legs[0].instruction == legs[1].instruction:
            raise ValueError("A vertical spread requires one leg BUY and the other SELL")
    elif combo_type == "straddle":
        if a.right == b.right:
            raise ValueError("A straddle requires one call and one put")
        if a.strike != b.strike:
            raise ValueError("A straddle requires both legs to share the same strike")
        if a.expiration != b.expiration:
            raise ValueError("A straddle requires both legs to share the same expiration")
        if legs[0].instruction != legs[1].instruction:
            raise ValueError("A straddle requires both legs to use the same instruction — both BUY for a long straddle, both SELL for a short straddle")
    elif combo_type == "strangle":
        if a.right == b.right:
            raise ValueError("A strangle requires one call and one put")
        if a.strike == b.strike:
            raise ValueError("A strangle requires two different strikes — equal strikes make it a straddle, not a strangle")
        if a.expiration != b.expiration:
            raise ValueError("A strangle requires both legs to share the same expiration")
        if legs[0].instruction != legs[1].instruction:
            raise ValueError("A strangle requires both legs to use the same instruction — both BUY for a long strangle, both SELL for a short strangle")


def _leg_signed_cost_usd(leg: TradeProposal, estimated_cost: float) -> float:
    """Signed dollar cost of one leg from the broker's own preview estimate — positive for a BUY (a debit), negative for a SELL (a credit). Premiums come from the broker's preview response, not a separate options-pricing model this system doesn't have."""
    magnitude = abs(estimated_cost)
    return magnitude if leg.instruction == Instruction.BUY else -magnitude


def _compute_vertical_spread_risk(legs: list[TradeProposal], net_usd: float) -> tuple[float, float]:
    """
    Standard vertical spread max-loss/max-profit. strike_width_usd is the
    dollar width between strikes (both legs already validated to trade
    the same quantity): a net DEBIT caps the loss at what was paid and the
    profit at the remaining width; a net CREDIT caps the profit at what
    was received and the loss at the remaining width.
    """
    a, b = (parse_occ_symbol(leg.symbol) for leg in legs)
    strike_width_usd = float(abs(a.strike - b.strike)) * 100 * legs[0].quantity
    if net_usd >= 0:  # net debit paid
        return net_usd, max(0.0, strike_width_usd - net_usd)
    credit = -net_usd  # net credit received
    return max(0.0, strike_width_usd - credit), credit


async def preview_multi_leg_order(
    executor: Executor, legs: list[TradeProposal], combo_type: ComboType = "custom"
) -> MultiLegPreview:
    """Previews every leg through the normal Executor.preview_order pipeline — same risk checks, allowlists, and notional caps any other order gets — and combines the results into one combo-level view."""
    validate_combo_structure(combo_type, legs)

    leg_previews: list[LegPreview] = []
    for leg in legs:
        preview = await executor.preview_order(leg)
        leg_previews.append(LegPreview(
            leg=leg, preview_id=preview.preview_id, risk_verdict=preview.risk_verdict,
            risk_details=preview.risk_details, estimated_cost=float(preview.estimated_cost),
        ))

    net_usd = sum(_leg_signed_cost_usd(lp.leg, lp.estimated_cost) for lp in leg_previews)
    max_loss_usd: Optional[float] = None
    max_profit_usd: Optional[float] = None
    if combo_type == "vertical_spread":
        max_loss_usd, max_profit_usd = _compute_vertical_spread_risk(legs, net_usd)

    overall_verdict = "APPROVED" if all(lp.risk_verdict == "APPROVED" for lp in leg_previews) else "REJECTED"

    return MultiLegPreview(
        combo_id=f"combo-{uuid.uuid4()}", combo_type=combo_type, legs=leg_previews,
        net_debit_or_credit_usd=net_usd, max_loss_usd=max_loss_usd, max_profit_usd=max_profit_usd,
        risk_verdict=overall_verdict,
    )


async def execute_multi_leg_order(
    executor: Executor, combo_id: str, legs: list[LegRef], approved_by: str, attestation: str
) -> MultiLegExecutionResult:
    """
    Executes every leg sequentially against its own already-approved
    preview. Deliberately does NOT take the MultiLegPreview object back —
    the preview and execute calls can be two separate HTTP requests (a
    human approving in between), so this only needs what
    Executor.execute_order itself needs per leg (decision_id/preview_id),
    which the caller can always reconstruct from persisted state alone.
    Executor.execute_order's own preview-must-be-approved gate still
    applies per leg — a leg whose preview was REJECTED simply fails here
    with that leg's own error, same as any other rejected single-leg
    order. See this module's docstring for what happens if a leg fails
    after an earlier one already executed.
    """
    result = MultiLegExecutionResult(combo_id=combo_id)

    for i, leg in enumerate(legs):
        try:
            receipt = await executor.execute_order(
                decision_id=leg.decision_id,
                preview_id=leg.preview_id,
                approved_by=approved_by,
                approved_at=datetime.utcnow(),
                attestation=f"{attestation} (combo {combo_id}, leg {i + 1}/{len(legs)})",
                idempotency_key=f"{leg.decision_id}:{combo_id}:leg{i}",
            )
            result.executed_legs.append(receipt.model_dump(mode="json"))
        except Exception as exc:
            logger.error(
                "multi_leg_execution_failed_mid_combo",
                combo_id=combo_id, failed_leg_index=i,
                legs_already_executed=len(result.executed_legs), symbol=leg.symbol, error=str(exc),
            )
            result.failed_leg_index = i
            result.error = str(exc)
            if result.executed_legs:
                # At least one leg is now open, unhedged — halt this agent
                # immediately rather than leave a naked position running
                # under a strategy that assumed it was hedged.
                KillSwitchService(executor.session).set_state(
                    enabled=True, set_by="multi_leg_guard", scope=leg.agent_id,
                    reason=(
                        f"Auto-halt: multi-leg combo {combo_id} failed on leg {i + 1}/"
                        f"{len(legs)} ({leg.symbol}) after {len(result.executed_legs)} leg(s) "
                        f"already executed — a naked, unhedged leg is open. Manual review required before clearing."
                    ),
                )
            return result

    return result


__all__ = [
    "ComboType",
    "LegPreview",
    "LegRef",
    "MultiLegExecutionResult",
    "MultiLegPreview",
    "execute_multi_leg_order",
    "preview_multi_leg_order",
    "validate_combo_structure",
]
