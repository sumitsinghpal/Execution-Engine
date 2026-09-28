"""
Bounded LLM evidence scoring for HedgeHog Silo's probabilistic gate (see
SiloRuntime.evaluate_candidate in src/execution/silo_runtime.py).

Scope, deliberately narrow: Groq and Gemini are asked to rate exactly two
things about a candidate that has ALREADY been selected by a fixed,
deterministic strategy rule (src/strategy/catalog.py), already sized by
src/execution/risk_reward.py's standardized stop/target, and already
cleared the Silo's own authorized_symbols/authorized_strategy_ids, rate,
concentration and drawdown checks — "eig" (does this specific setup carry
real informational edge) and "regime" (is the current market regime
favorable for it), each 0.0-1.0. Neither model ever sees, and neither
model's output can ever touch, whether to trade, how much to trade, or
when to exit — those stay fixed decisions made before this module is ever
called. This function returns a dict of floats; nothing in it constructs
a TradeProposal, calls Executor, or reaches a broker.

SiloRuntime.evaluate_candidate() already re-normalizes its weighted
average over only the evidence keys it's actually given (see
src/execution/silo_evidence.py's module docstring), so "eig"/"regime"
here simply slot into the same mandate.probability_weights/decision_bands
math the honest liquidity/freshness keys already use — an LLM score
becomes one more bounded input to an existing deterministic formula, not
a new decision-maker of its own.

Fails closed at every layer: disabled by default (settings.
llm_evidence_enabled), a timeout or malformed/out-of-schema response from
either provider drops that provider's score entirely rather than
fabricating one, and a factor a provider didn't validly return is simply
absent from the result -- exactly the same "absent, not fabricated"
contract src/execution/silo_evidence.py uses for liquidity/freshness.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from src.config import Settings
from src.logging_config import get_logger

logger = get_logger(__name__)

_SCORE_KEYS = ("eig", "regime")


@dataclass
class CandidateContext:
    """Everything an LLM scorer is given about a candidate — no more, no less."""

    symbol: str
    strategy_id: str
    entry_price: float
    stop_loss_price: float
    take_profit_price: float
    rationale: str


def _build_prompt(candidate: CandidateContext) -> str:
    return (
        "You are rating ONE specific trade candidate for a paper-trading research system. "
        "This candidate was already selected by a fixed, rule-based strategy and already sized "
        "and risk-approved by an independent system. You do not decide whether to trade, how much "
        "to trade, or when to exit -- those are already fixed. Your only job is to rate two things "
        "about this exact candidate, each as a number from 0.0 (very poor) to 1.0 (very strong):\n\n"
        '- "eig": how much genuine informational edge this specific setup appears to carry, beyond random chance.\n'
        '- "regime": how favorable the current market regime looks for this kind of trade.\n\n'
        f"Candidate: symbol={candidate.symbol}, strategy={candidate.strategy_id}, "
        f"entry_price={candidate.entry_price}, stop_loss_price={candidate.stop_loss_price}, "
        f"take_profit_price={candidate.take_profit_price}, strategy_rationale={candidate.rationale!r}.\n\n"
        'Respond with ONLY a JSON object of the exact shape {"eig": <number>, "regime": <number>}. '
        "No other text."
    )


def _extract_json_object(text: str) -> Optional[dict]:
    """Pulls the first {...} object out of a response, tolerating markdown code fences around it."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _validate_scores(parsed: dict) -> dict[str, float]:
    """
    Keeps only the score keys that are present and a finite real number,
    clamped to [0, 1]. A key that's missing, non-numeric, NaN, or infinite
    is simply left out -- never fabricated as 0.0 or 1.0.
    """
    scores: dict[str, float] = {}
    for key in _SCORE_KEYS:
        value = parsed.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if not math.isfinite(value):
            continue
        scores[key] = max(0.0, min(1.0, float(value)))
    return scores


async def _call_groq(candidate: CandidateContext, settings: Settings) -> dict[str, float]:
    if not settings.groq_api_key:
        return {}
    try:
        async with httpx.AsyncClient(timeout=settings.llm_evidence_timeout_seconds) as client:
            response = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {settings.groq_api_key}"},
                json={
                    "model": settings.groq_model,
                    "messages": [{"role": "user", "content": _build_prompt(candidate)}],
                    "response_format": {"type": "json_object"},
                    "temperature": 0,
                },
            )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
    except Exception as exc:
        logger.warning("llm_evidence_groq_call_failed", symbol=candidate.symbol, error=str(exc))
        return {}
    parsed = _extract_json_object(content)
    if parsed is None:
        logger.warning("llm_evidence_groq_unparseable_response", symbol=candidate.symbol)
        return {}
    return _validate_scores(parsed)


async def _call_gemini(candidate: CandidateContext, settings: Settings) -> dict[str, float]:
    if not settings.gemini_api_key:
        return {}
    try:
        async with httpx.AsyncClient(timeout=settings.llm_evidence_timeout_seconds) as client:
            response = await client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{settings.gemini_model}:generateContent",
                params={"key": settings.gemini_api_key},
                json={
                    "contents": [{"parts": [{"text": _build_prompt(candidate)}]}],
                    "generationConfig": {"responseMimeType": "application/json", "temperature": 0},
                },
            )
        response.raise_for_status()
        content = response.json()["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as exc:
        logger.warning("llm_evidence_gemini_call_failed", symbol=candidate.symbol, error=str(exc))
        return {}
    parsed = _extract_json_object(content)
    if parsed is None:
        logger.warning("llm_evidence_gemini_unparseable_response", symbol=candidate.symbol)
        return {}
    return _validate_scores(parsed)


async def compute_llm_evidence(candidate: CandidateContext, settings: Settings) -> dict[str, float]:
    """
    Queries both providers concurrently and averages whichever ones
    actually returned a valid score for a given key. Never raises: a
    disabled feature, missing keys, timeouts, or malformed responses all
    resolve to an empty (or partial) dict, not an exception -- the caller
    never needs its own try/except around this.
    """
    if not settings.llm_evidence_enabled:
        return {}
    groq_scores, gemini_scores = await asyncio.gather(
        _call_groq(candidate, settings), _call_gemini(candidate, settings), return_exceptions=False
    )
    merged: dict[str, float] = {}
    for key in _SCORE_KEYS:
        values = [s[key] for s in (groq_scores, gemini_scores) if key in s]
        if values:
            merged[key] = sum(values) / len(values)
    return merged


__all__ = ["CandidateContext", "compute_llm_evidence"]
