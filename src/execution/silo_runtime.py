"""Persistent HedgeHog Silo mandate for autonomous PAPER and LIVE execution.

The Silo replaces DailyPlan as the run-authorization layer. Trading limits are
not hard-coded here; they are supplied by the armed mandate. Whether the
autonomous loop's orders are actually real is decided by
autonomous_trader.py's _build_broker(settings, mandate), never by this
module alone -- a mandate with mode="AUTONOMOUS_LIVE" only ever describes an
authorization; it grants nothing by existing.
"""
from __future__ import annotations

import hashlib
import hmac as hmac_module
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Literal, Optional
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, model_validator

DEFAULT_STATE_FILE = ".hedgehog_silo.json"


class ProbabilityBands(BaseModel):
    execute_above: float = Field(ge=0, le=1)
    resize_above: float = Field(ge=0, le=1)


class SiloMandate(BaseModel):
    silo_id: str
    armed_by: str
    mode: Literal["AUTONOMOUS_PAPER", "AUTONOMOUS_LIVE"] = "AUTONOMOUS_PAPER"
    timezone: str = "America/New_York"
    runtime_start: Optional[datetime] = None
    runtime_end: str = "16:00"
    trades_per_minute: int = Field(gt=0)
    max_per_trade_usd: Decimal = Field(gt=0)
    max_concentration_pct: Decimal = Field(gt=0, le=100)
    max_drawdown_pct: Optional[Decimal] = Field(default=None, gt=0, le=100)
    authorized_symbols: list[str] = Field(default_factory=list)
    authorized_strategy_ids: list[str] = Field(default_factory=list)
    approved_trade_cards: list[str] = Field(default_factory=list)
    approved_algos: list[str] = Field(default_factory=list)
    probability_weights: dict[str, float] = Field(default_factory=dict)
    decision_bands: ProbabilityBands
    armed_at: datetime = Field(default_factory=datetime.utcnow)
    active: bool = True

    # AUTONOMOUS_LIVE-only fields. A mandate authorizes real order routing
    # for autonomous_trader.py's _build_broker(settings, mandate) ONLY when
    # mode == "AUTONOMOUS_LIVE" AND every field below is set AND (separately,
    # at the deployment level, outside this file) both
    # settings.autonomous_live_robinhood_enabled and
    # settings.robinhood_live_trading_enabled are true AND the resolved
    # account profile is itself live_enabled. This mandate alone can never
    # grant live trading -- see _build_broker()'s own docstring.
    broker: Optional[Literal["robinhood"]] = None
    account_alias: Optional[str] = None
    live_execution_authorized: bool = False
    # HMAC over this mandate's own fields, keyed on the server's admin key
    # (see compute_mandate_hmac below) -- stamped by arm(), verified by
    # active_mandate() for AUTONOMOUS_LIVE mandates. A plain checksum would
    # let anyone who can write the state file recompute a matching value
    # after editing e.g. authorized_symbols; an HMAC can't be recomputed
    # without the same server-side secret every other admin-gated write in
    # this codebase already relies on.
    mandate_hmac: Optional[str] = None

    @model_validator(mode="after")
    def _validate_live_contract(self) -> "SiloMandate":
        if self.mode != "AUTONOMOUS_LIVE":
            return self
        missing = []
        if self.broker != "robinhood":
            missing.append("broker='robinhood'")
        if not self.account_alias:
            missing.append("account_alias")
        if not self.live_execution_authorized:
            missing.append("live_execution_authorized=true")
        if not self.authorized_symbols:
            missing.append("authorized_symbols")
        if not self.authorized_strategy_ids:
            missing.append("authorized_strategy_ids")
        if not self.approved_trade_cards and not self.approved_algos:
            missing.append("approved_trade_cards or approved_algos")
        if missing:
            raise ValueError(f"AUTONOMOUS_LIVE mandate missing required: {', '.join(missing)}")
        return self


class CandidateDecision(BaseModel):
    allowed: bool
    action: str
    probability_score: Optional[float] = None
    reason: str


class SiloState(BaseModel):
    mandate: Optional[SiloMandate] = None
    trade_timestamps: list[datetime] = Field(default_factory=list)


def compute_mandate_hmac(mandate: SiloMandate, secret: str) -> str:
    """
    HMAC-SHA256 over the mandate's own fields (excluding mandate_hmac
    itself), keyed on secret. Unlike a plain checksum, this can't be
    recomputed to match a tampered mandate without knowing secret --
    intended to be settings.api_key_admin, the same server-side secret
    every other admin-gated write in this codebase already relies on, so
    arming/verifying an AUTONOMOUS_LIVE mandate needs no new secret.
    """
    payload = mandate.model_dump_json(exclude={"mandate_hmac"})
    return hmac_module.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


class SiloRuntime:
    def __init__(self, state_file: str = DEFAULT_STATE_FILE):
        self.path = Path(state_file)

    def _load(self) -> SiloState:
        try:
            return SiloState.model_validate_json(self.path.read_text())
        except (OSError, ValueError):
            return SiloState()

    def _save(self, state: SiloState) -> None:
        self.path.write_text(state.model_dump_json(indent=2))

    def arm(self, mandate: SiloMandate, admin_key: Optional[str] = None) -> SiloMandate:
        mandate.active = True
        mandate.armed_at = datetime.utcnow()
        if mandate.runtime_start is None:
            # Must be tz-aware UTC, not naive datetime.utcnow() -- see
            # active_mandate()'s handling of a naive runtime_start below,
            # which assumes exactly this.
            mandate.runtime_start = datetime.now(timezone.utc)
        if mandate.mode == "AUTONOMOUS_LIVE":
            if not admin_key:
                raise ValueError("Arming an AUTONOMOUS_LIVE mandate requires the server's admin key")
            mandate.mandate_hmac = compute_mandate_hmac(mandate, admin_key)
        self._save(SiloState(mandate=mandate, trade_timestamps=[]))
        return mandate

    def disarm(self) -> Optional[SiloMandate]:
        state = self._load()
        if state.mandate:
            state.mandate.active = False
            self._save(state)
        return state.mandate

    def active_mandate(self, now: Optional[datetime] = None, admin_key: Optional[str] = None) -> Optional[SiloMandate]:
        state = self._load()
        m = state.mandate
        if m is None or not m.active:
            return None
        if m.mode == "AUTONOMOUS_LIVE":
            # A missing or mismatched HMAC is treated exactly like an
            # inactive mandate -- fails closed, not an exception, so a
            # tampered or un-verifiable LIVE mandate simply stops
            # authorizing anything rather than raising mid-scan.
            if not admin_key or not m.mandate_hmac or not hmac_module.compare_digest(
                m.mandate_hmac, compute_mandate_hmac(m, admin_key)
            ):
                return None
        tz = ZoneInfo(m.timezone)
        current = now.astimezone(tz) if now and now.tzinfo else datetime.now(tz)
        hh, mm = (int(x) for x in m.runtime_end.split(":", 1))
        end = current.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if current >= end:
            m.active = False
            state.mandate = m
            self._save(state)
            return None
        if m.runtime_start:
            start = m.runtime_start
            # A naive runtime_start is always a UTC clock reading (see
            # arm() above and SiloMandate's own construction) -- it must
            # be interpreted as UTC before converting to the mandate's
            # timezone, never assumed to already BE that timezone. Doing
            # the latter mislabels e.g. a 22:28 UTC reading as 22:28
            # America/New_York (really 18:28 NY), which is hours ahead of
            # the real current NY time right after arming and made every
            # arm() silently report "not active yet" for hours.
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            start = start.astimezone(tz)
            if current < start:
                return None
        return m

    def record_trade(self, when: Optional[datetime] = None) -> None:
        state = self._load()
        now = when or datetime.utcnow()
        cutoff = now - timedelta(minutes=1)
        state.trade_timestamps = [x for x in state.trade_timestamps if x >= cutoff]
        state.trade_timestamps.append(now)
        self._save(state)

    def evaluate_candidate(
        self,
        *,
        symbol: str,
        strategy_id: str,
        estimated_notional_usd: Decimal,
        concentration_pct: Optional[Decimal] = None,
        drawdown_pct: Optional[Decimal] = None,
        evidence: Optional[dict[str, float]] = None,
        admin_key: Optional[str] = None,
    ) -> CandidateDecision:
        state = self._load()
        m = self.active_mandate(admin_key=admin_key)
        if m is None:
            return CandidateDecision(allowed=False, action="DEFER", reason="No active Silo mandate")
        now = datetime.utcnow()
        cutoff = now - timedelta(minutes=1)
        recent = [x for x in state.trade_timestamps if x >= cutoff]
        if len(recent) >= m.trades_per_minute:
            return CandidateDecision(allowed=False, action="DEFER", reason="Silo trade-rate budget exhausted")
        if m.authorized_symbols and symbol not in m.authorized_symbols:
            return CandidateDecision(allowed=False, action="DEFER", reason="Symbol outside armed Silo")
        if m.authorized_strategy_ids and strategy_id not in m.authorized_strategy_ids:
            return CandidateDecision(allowed=False, action="DEFER", reason="Strategy outside armed Silo")
        if estimated_notional_usd > m.max_per_trade_usd:
            return CandidateDecision(allowed=False, action="RESIZE", reason="Candidate exceeds Silo max-per-trade")
        if concentration_pct is not None and concentration_pct > m.max_concentration_pct:
            return CandidateDecision(allowed=False, action="RESIZE", reason="Candidate exceeds Silo concentration mandate")
        if m.max_drawdown_pct is not None and drawdown_pct is not None and drawdown_pct > m.max_drawdown_pct:
            return CandidateDecision(allowed=False, action="DEFER", reason="Silo drawdown mandate reached")

        weights = m.probability_weights
        if not weights:
            return CandidateDecision(allowed=True, action="EXECUTE", reason="No probabilistic weights configured")
        evidence = evidence or {}
        used = [(k, float(w), float(evidence[k])) for k, w in weights.items() if k in evidence]
        if not used:
            return CandidateDecision(allowed=False, action="DEFER", reason="No evidence for Silo probability model")
        weight_sum = sum(abs(w) for _, w, _ in used)
        score = sum(w * v for _, w, v in used) / weight_sum if weight_sum else 0.0
        score = max(0.0, min(1.0, score))
        if score >= m.decision_bands.execute_above:
            return CandidateDecision(allowed=True, action="EXECUTE", probability_score=score, reason="Probabilistic Silo gate: execute")
        if score >= m.decision_bands.resize_above:
            return CandidateDecision(allowed=False, action="RESIZE", probability_score=score, reason="Probabilistic Silo gate: resize")
        return CandidateDecision(allowed=False, action="DEFER", probability_score=score, reason="Probabilistic Silo gate: defer")


__all__ = ["CandidateDecision", "ProbabilityBands", "SiloMandate", "SiloRuntime", "SiloState", "compute_mandate_hmac"]
