"""
Evidence signals for HedgeHog Silo's probabilistic execute/resize/defer
gate (see SiloRuntime.evaluate_candidate in src/execution/silo_runtime.py).

Deliberately narrow: this computes REAL, honest 0..1 proxies only for the
factors this codebase actually has data for today —

  - "liquidity": from the live quote's bid/ask spread. A tighter spread
    relative to price is more liquid; a spread at or beyond
    LIQUIDITY_FLOOR_SPREAD_PCT scores 0.0.
  - "freshness": from the live quote's own quote_time vs now, using the
    SAME staleness window RiskChecker already enforces
    (settings.max_quote_age_seconds), so "fresh" means the same thing
    here as it does everywhere else in this system.

It deliberately does NOT fabricate "eig" (expected information gain),
"regime", or "execution" scores: nothing in this codebase computes a real
signal for any of those today, and a constant or made-up number dressed
as evidence would be worse than no evidence at all (a Silo mandate that
weights those factors would then be "satisfied" by noise instead of
correctly excluding them). SiloRuntime.evaluate_candidate() already
handles partial evidence correctly — it re-normalizes the weighted
average over only the factors a caller actually supplies evidence for —
so a mandate's eig/regime/execution weights simply drop out of the score
rather than being silently satisfied by a fake value. When a real source
for any of those factors exists, add it here the same way.

A factor is left OUT of the returned dict entirely when it can't be
honestly computed (e.g. a quote missing bid/ask), never included as a
fabricated 0.0 or 1.0 — "unknown" and "worst possible" are not the same
thing, and conflating them would make the probability gate systematically
more conservative than it should be for reasons that have nothing to do
with the candidate itself.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

LIQUIDITY_FLOOR_SPREAD_PCT = 0.02  # a 2%+ bid/ask spread scores liquidity 0.0


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def liquidity_score(quote: dict[str, Any]) -> Optional[float]:
    """None when the quote doesn't have a usable bid/ask/last to compare — never fabricated as 0 or 1."""
    bid, ask, last = quote.get("bid"), quote.get("ask"), quote.get("last")
    if bid is None or ask is None or not last:
        return None
    try:
        spread_pct = abs(float(ask) - float(bid)) / float(last)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return _clamp(1.0 - spread_pct / LIQUIDITY_FLOOR_SPREAD_PCT)


def freshness_score(quote: dict[str, Any], max_age_seconds: float) -> Optional[float]:
    """None when the quote has no parseable quote_time to measure staleness from."""
    quote_time_raw = quote.get("quote_time")
    if not quote_time_raw:
        return None
    try:
        observed = datetime.fromisoformat(str(quote_time_raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    if max_age_seconds <= 0:
        return None
    age_seconds = max((datetime.now(timezone.utc) - observed).total_seconds(), 0.0)
    return _clamp(1.0 - age_seconds / max_age_seconds)


def compute_evidence(quote: dict[str, Any], max_quote_age_seconds: float) -> dict[str, float]:
    """Real evidence only — see module docstring for why eig/regime/execution are never included here."""
    evidence: dict[str, float] = {}
    liquidity = liquidity_score(quote)
    if liquidity is not None:
        evidence["liquidity"] = liquidity
    freshness = freshness_score(quote, max_quote_age_seconds)
    if freshness is not None:
        evidence["freshness"] = freshness
    return evidence


def concentration_pct_of_equity(candidate_notional_usd: Decimal, account_equity: Optional[float]) -> Optional[Decimal]:
    """
    A candidate's notional as a percentage of total account equity — what
    SiloMandate.max_concentration_pct is actually meant to bound. None
    when equity is missing/zero so the caller can fail closed (defer)
    instead of silently skipping the concentration check, which is what
    happened before this was wired up at all.
    """
    if not account_equity:
        return None
    return (candidate_notional_usd / Decimal(str(account_equity))) * Decimal("100")


__all__ = ["compute_evidence", "concentration_pct_of_equity", "freshness_score", "liquidity_score"]
