"""
Per-agent risk overrides for deployments running multiple coordinating
agents (see src/execution/kill_switch_state.py for the per-agent kill
switch this composes with).

Every field is optional and means "fall back to the global default" when
unset — an unconfigured agent (including the implicit "default" agent used
by single-agent callers) behaves exactly as it did before agent-scoped
overrides existed. Configuring a profile is how an operator gives one
agent a *tighter* leash than the fleet default; nothing here can loosen a
global restriction (see RiskChecker's symbol-denylist handling, which
unions rather than overrides).
"""

from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict


class AgentRiskProfile(BaseModel):
    """Optional per-agent overrides layered on top of the global risk settings."""

    max_order_notional_usd: Optional[Decimal] = None
    symbol_allowlist: Optional[list[str]] = None
    symbol_denylist: Optional[list[str]] = None

    # Daily committed-notional exposure cap for AgentExposureGuard (see
    # src/execution/agent_exposure_guard.py). None disables the check for
    # this agent — opt-in, since an unconfigured agent shouldn't suddenly
    # start getting auto-halted with no threshold anyone chose.
    max_daily_notional_usd: Optional[Decimal] = None

    model_config = ConfigDict(extra="forbid")


__all__ = ["AgentRiskProfile"]
