from __future__ import annotations

from dataclasses import asdict, dataclass, field

from sqlmodel import Session, select

from src.models.orders import KillSwitchState, TradeProposal
from src.risk import limits


@dataclass
class RiskVerdict:
    passed: bool
    reasons: list[str] = field(default_factory=list)
    metrics: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def run_pretrade_checks(session: Session, proposal: TradeProposal) -> RiskVerdict:
    verdict = RiskVerdict(passed=True)

    kill_switch = session.exec(select(KillSwitchState).where(KillSwitchState.id == 1)).first()
    if kill_switch and kill_switch.is_on:
        verdict.passed = False
        verdict.reasons.append("kill switch is enabled")

    checks = (
        limits.enforce_account_allowlist,
        limits.enforce_symbol_policy,
    )
    for check in checks:
        try:
            check(proposal)
        except limits.RiskViolation as exc:
            verdict.passed = False
            verdict.reasons.append(str(exc))

    notional = None
    try:
        notional = limits.enforce_max_order_notional(proposal)
    except limits.RiskViolation as exc:
        verdict.passed = False
        verdict.reasons.append(str(exc))

    if notional is not None:
        verdict.metrics["estimated_notional"] = f"{notional:.2f}"
        try:
            limits.enforce_max_concentration(notional)
        except limits.RiskViolation as exc:
            verdict.passed = False
            verdict.reasons.append(str(exc))

    try:
        limits.enforce_market_hours()
    except limits.RiskViolation as exc:
        verdict.passed = False
        verdict.reasons.append(str(exc))

    return verdict
