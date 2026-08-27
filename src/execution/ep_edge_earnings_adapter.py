"""
Adapter for EP-Edge-Earnings-Engine's TradeCandidate output — the
push-based counterpart to src/execution/edge_tf_connector.py.

Unlike EDGE-TF, EP-Edge-Earnings-Engine (packages/trading/strategy.py's
build_trade_candidate, called from workflows/earnings_event.py) has no HTTP
service, no claim/report lifecycle, and no order-shaped output: it's a
library that returns a directional thesis (ticker, direction, expected
value, confidence) with a docstring that says outright "this module cannot
place orders." There is nothing to poll, so this side is fed by a push:
whoever runs EP-Edge-Earnings-Engine's workflow POSTs the resulting
candidates to POST /v1/external-signals/ingest (see server.py), which calls
record_batch below.

A TradeCandidate also carries no quantity, no order type, no price — it's
explicitly unsized (see EP-Edge-Earnings-Engine's own README: "trade
candidate generation without execution"). That's preserved rather than
papered over: ExternalSignalRecord.quantity is left None for every
ep-edge-earnings signal, and ExternalSignalRecord.to_trade_proposal_dict()
requires a human-supplied quantity at load time for exactly this reason.
"""

from __future__ import annotations

import hashlib
from typing import Any, Optional

from pydantic import BaseModel, Field
from sqlmodel import Session

from src.execution.external_signals import ExternalSignalRecord, ExternalSignalService
from src.logging_config import get_logger

logger = get_logger(__name__)

SOURCE = "ep-edge-earnings"

_DIRECTION_TO_SIDE = {"bullish": "BUY", "bearish": "SELL"}  # "neutral" intentionally absent — nothing to trade


class TradeCandidateInput(BaseModel):
    """
    Wire shape matching EP-Edge-Earnings-Engine's own
    packages.domain.models.TradeCandidate field-for-field, so a caller can
    serialize that dataclass with dataclasses.asdict() (direction becomes
    its .value string) and POST it here unchanged. Kept as a standalone
    model rather than importing that package directly — these are two
    separate repos/deployments, same boundary EDGE-TF's own gateway
    docstring insists on ("separate repo, separate runtime").
    """

    ticker: str
    thesis: str
    direction: str  # "bullish" | "bearish" | "neutral"
    instrument: str = "equity"
    expected_move: float
    implied_move: float
    probability_positive: float
    expected_value: float
    confidence: float
    market_awareness: float
    invalidation_conditions: tuple[str, ...] = ()

    model_config = {"extra": "forbid"}


def _stable_trade_id(candidate: TradeCandidateInput) -> str:
    """
    EP-Edge-Earnings-Engine issues no trade id of its own — this system
    needs a stable one to dedupe re-ingestion (e.g. the same batch POSTed
    twice) without a schema-level identity from the source. Deterministic
    over the fields that define "the same candidate": re-running the
    workflow on the same evidence should not spawn a duplicate row, but a
    genuinely revised thesis (different expected_value/confidence/etc. for
    the same ticker) is a new one.
    """
    basis = "|".join(
        [
            candidate.ticker,
            candidate.direction,
            f"{candidate.expected_value:.6f}",
            f"{candidate.confidence:.6f}",
            f"{candidate.market_awareness:.6f}",
        ]
    )
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]
    return f"ep-edge:{candidate.ticker}:{digest}"


def record_candidate(session: Session, candidate: dict[str, Any]) -> Optional[ExternalSignalRecord]:
    """
    Records one TradeCandidate as an external signal. Returns None (and
    records nothing) for a "neutral" direction — build_trade_candidate on
    the EP-Edge-Earnings-Engine side is only ever supposed to return one
    once it has already decided BULLISH or BEARISH, but this stays
    defensive against a caller handing over a raw ResearchObject's full
    candidate list unfiltered.
    """
    parsed = TradeCandidateInput.model_validate(candidate)
    side = _DIRECTION_TO_SIDE.get(parsed.direction)
    if side is None:
        logger.info("ep_edge_candidate_skipped_neutral", ticker=parsed.ticker)
        return None

    instruction = {
        "trade_id": _stable_trade_id(parsed),
        "symbol": parsed.ticker,
        "side": side,
        "order_type": "MARKET",  # no price/level in a TradeCandidate — see module docstring
        "thesis_id": parsed.ticker,
        "strategy_module": "workflows.earnings_event",
        "rationale": (
            f"{parsed.thesis} (expected_value={parsed.expected_value:.4f}, "
            f"confidence={parsed.confidence:.2f}, market_awareness={parsed.market_awareness:.2f})"
        ),
    }
    return ExternalSignalService(session).record_if_new(SOURCE, instruction)


def record_batch(session: Session, candidates: list[dict[str, Any]]) -> int:
    """Records every candidate in one ingest call; returns how many were genuinely new."""
    new_count = 0
    for candidate in candidates:
        record = record_candidate(session, candidate)
        if record is not None:
            new_count += 1
    return new_count


class ExternalSignalIngestRequest(BaseModel):
    """POST /v1/external-signals/ingest body — source-tagged so the same endpoint can take a second push source later."""

    source: str
    candidates: list[dict[str, Any]] = Field(default_factory=list)

    model_config = {"extra": "forbid"}


__all__ = [
    "SOURCE",
    "ExternalSignalIngestRequest",
    "TradeCandidateInput",
    "record_batch",
    "record_candidate",
]
