"""
Tests for the per-agent daily notional exposure cap: a redundant,
agent-scoped backstop alongside the account-level DrawdownGuard.

Note on decision_id/agent_id choice: like other files in this shared-DB
test suite, each test uses a unique agent_id so its committed-notional sum
isn't polluted by orders another test created for the same agent earlier
in the run.
"""

from datetime import datetime

from src.agents.profiles import AgentRiskProfile
from src.execution.agent_exposure_guard import AgentExposureGuard
from src.execution.executor import OrderRecord
from src.execution.kill_switch_state import GLOBAL_SCOPE, KillSwitchService
from src.models.orders import OrderStatus


def _make_order(session, agent_id, notional, status=OrderStatus.SUBMITTED.value, symbol="QQQ"):
    order = OrderRecord(
        decision_id=f"exposure-test-{agent_id}-{datetime.utcnow().timestamp()}",
        agent_id=agent_id,
        account="primary",
        symbol=symbol,
        quantity=1,
        instruction="BUY",
        status=status,
        payload_checksum="test-checksum",
        estimated_notional_usd=str(notional),
    )
    session.add(order)
    session.commit()
    return order


def test_uncapped_agent_is_never_breached(test_db_engine_and_session):
    """An agent with no configured cap is exempt, no matter how much it has committed."""
    _, session = test_db_engine_and_session
    _make_order(session, "aeg-uncapped-agent", 999_999)

    guard = AgentExposureGuard(session=session)
    report = guard.check_exposure("aeg-uncapped-agent")

    assert report.cap_usd is None
    assert report.breached is False


def test_committed_notional_sums_only_committed_statuses(test_db_engine_and_session):
    """A PREVIEWED-only order (never approved/submitted) must not count as committed capital."""
    _, session = test_db_engine_and_session
    _make_order(session, "aeg-status-filter-agent", 500, status=OrderStatus.PREVIEWED.value)
    _make_order(session, "aeg-status-filter-agent", 300, status=OrderStatus.SUBMITTED.value)
    _make_order(session, "aeg-status-filter-agent", 200, status=OrderStatus.REJECTED.value)

    guard = AgentExposureGuard(session=session)
    report = guard.check_exposure("aeg-status-filter-agent")

    assert report.committed_notional_usd == 300


def test_under_cap_does_not_trip_kill_switch(test_db_engine_and_session):
    _, session = test_db_engine_and_session
    _make_order(session, "aeg-under-cap-agent", 5_000)

    guard = AgentExposureGuard(session=session)
    guard.settings.agent_risk_profiles["aeg-under-cap-agent"] = AgentRiskProfile(max_daily_notional_usd=10_000)
    assert KillSwitchService(session).is_enabled("aeg-under-cap-agent") is False

    report = guard.check_and_halt("aeg-under-cap-agent")

    assert report.breached is False
    assert KillSwitchService(session).is_enabled("aeg-under-cap-agent") is False


def test_breach_halts_only_this_agents_own_scope(test_db_engine_and_session):
    """A breach must halt this agent's own switch — not the fleet-wide switch, not another agent."""
    _, session = test_db_engine_and_session
    _make_order(session, "aeg-breach-agent", 1_500)

    guard = AgentExposureGuard(session=session)
    guard.settings.agent_risk_profiles["aeg-breach-agent"] = AgentRiskProfile(max_daily_notional_usd=1_000)

    try:
        report = guard.check_and_halt("aeg-breach-agent")

        assert report.breached is True
        assert report.committed_notional_usd == 1_500
        assert KillSwitchService(session).is_enabled("aeg-breach-agent") is True
        assert KillSwitchService(session).is_enabled(GLOBAL_SCOPE) is False, (
            "an agent exposure breach must not trip the fleet-wide switch"
        )
        assert KillSwitchService(session).is_enabled("aeg-breach-agent-neighbor") is False
    finally:
        KillSwitchService(session).set_state(enabled=False, set_by="test_cleanup", scope="aeg-breach-agent")
