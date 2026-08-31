"""
Adapter for hedge-engine-impl's "Decision Record" output — a third
push-based external signal source, same shape of integration as
src/execution/ep_edge_earnings_adapter.py (see that module's docstring
for why push rather than poll).

hedge-engine-impl (a macro/LETF-decay-hedging strategy library — see
src/ev_calc.py, src/decay_sim.py in that repo) has no HTTP service and no
claim/report lifecycle either. Its src/audit.py's make_decision_record()
produces a self-contained, tamper-evident JSON object:

    {
      "llm_output": {
        "p_success": 0.7, "p_confidence": 0.82, "horizon_days": 5,
        "expected_delta": {"fav": 0.08, "neutral": 0.0, "unfav": -0.05},
        "suggested_instrument": {"type": "LETF", "ticker": "SSO", "leverage": 3.0},
        "rationale": "...", "evidence": [...], "flags": {...}
      },
      "quant_checks": {
        "ev_gross": ..., "letf_decay": ..., "ev_net": ...,
        "viability_pass": true, "p_confidence": ..., "safety_margin": ..., "notes": "..."
      },
      "prompt_hash": "...", "model_version": "...", "inputs": {...},
      "decision_id": "<uuid4 hex>", "timestamp_utc": "...", "audit_hash": "..."
    }

Only `quant_checks.viability_pass == true` decisions are recorded — same
"only record a signal that already passed its own gate" rule
ep_edge_earnings_adapter.py follows for a non-neutral direction. A LETF
hedge is always a long position in the suggested instrument (hedge-engine
itself only ever builds a BUY execution plan for one — see its
build_execution_plan_from_decision), never a short.

hedge-engine's decision_id is a fresh UUID4 per call (not content-derived
like EP-Edge-Earnings' thesis, which has no id of its own) — re-recording
the exact same decision_id is naturally idempotent via
ExternalSignalService.record_if_new()'s existing external_trade_id dedup,
so no separate content-hash is needed here the way EP's adapter needs one.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field
from sqlmodel import Session

from src.execution.external_signals import ExternalSignalRecord, ExternalSignalService
from src.logging_config import get_logger

logger = get_logger(__name__)

SOURCE = "hedge-engine"

# hedge-engine-impl ships exactly one strategy today (LETF decay hedging).
# A dedicated field for this doesn't exist in its Decision Record, so it's
# named here rather than left blank — revisit if/when hedge-engine grows
# a second strategy that also produces Decision Records.
_STRATEGY_MODULE = "hedge-engine:letf-decay"


class SuggestedInstrument(BaseModel):
    type: str = "LETF"
    ticker: str
    leverage: Optional[float] = None

    model_config = {"extra": "allow"}  # forward-compatible with fields hedge-engine adds later


class QuantChecks(BaseModel):
    ev_gross: float
    letf_decay: float
    ev_net: float
    viability_pass: bool
    p_confidence: float
    safety_margin: float
    notes: str = ""

    model_config = {"extra": "allow"}


class LlmOutput(BaseModel):
    suggested_instrument: SuggestedInstrument
    rationale: str = ""
    p_success: Optional[float] = None
    horizon_days: Optional[int] = None

    model_config = {"extra": "allow"}


class HedgeDecisionInput(BaseModel):
    """
    Wire shape matching hedge-engine-impl's own Decision Record
    field-for-field (src/audit.py's make_decision_record output), so a
    caller can POST that dict here unchanged. Kept as a standalone model
    rather than importing that package directly — separate repos,
    separate runtimes, same boundary every other external-signal adapter
    in this file holds.
    """

    decision_id: str
    llm_output: LlmOutput
    quant_checks: QuantChecks
    timestamp_utc: Optional[str] = None
    audit_hash: Optional[str] = None
    model_version: Optional[str] = None

    model_config = {"extra": "allow"}


def record_decision(session: Session, decision: dict[str, Any]) -> Optional[ExternalSignalRecord]:
    """
    Records one hedge-engine Decision Record as an external signal.
    Returns None (records nothing) when quant_checks.viability_pass is
    False — hedge-engine's own EV/confidence gate already rejected it;
    there's nothing actionable to show a human.
    """
    parsed = HedgeDecisionInput.model_validate(decision)
    if not parsed.quant_checks.viability_pass:
        logger.info("hedge_decision_skipped_not_viable", decision_id=parsed.decision_id, notes=parsed.quant_checks.notes)
        return None

    instrument = parsed.llm_output.suggested_instrument
    rationale = (
        f"{parsed.llm_output.rationale} "
        f"(ev_net={parsed.quant_checks.ev_net:.4f}, p_confidence={parsed.quant_checks.p_confidence:.2f}"
        + (f", leverage={instrument.leverage}x" if instrument.leverage else "")
        + ")"
    ).strip()

    instruction = {
        "trade_id": f"hedge:{parsed.decision_id}",
        "symbol": instrument.ticker,
        "side": "BUY",  # a hedge-engine Decision Record only ever proposes going long its suggested LETF — see module docstring
        "order_type": "MARKET",  # no price/level in a Decision Record
        "thesis_id": parsed.decision_id,
        "strategy_module": _STRATEGY_MODULE,
        "rationale": rationale,
    }
    return ExternalSignalService(session).record_if_new(SOURCE, instruction)


def record_batch(session: Session, decisions: list[dict[str, Any]]) -> int:
    """Records every decision in one ingest call; returns how many were genuinely new (viable AND not a dedup)."""
    new_count = 0
    for decision in decisions:
        record = record_decision(session, decision)
        if record is not None:
            new_count += 1
    return new_count


__all__ = ["SOURCE", "HedgeDecisionInput", "record_batch", "record_decision"]
