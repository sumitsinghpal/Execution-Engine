"""
Unit tests for the scoped, redundant kill switch: a fleet-wide switch plus
one independent switch per agent, where is_halted() is the OR of both.

Note on account/scope choice: like other files in this shared-DB test
suite, kill switch state is a global singleton per scope, so each test
either uses a distinct agent_id or cleans up the scopes it touched.
"""

import pytest

from src.execution.kill_switch_state import GLOBAL_SCOPE, KillSwitchService


def test_all_scopes_start_disabled_by_default(test_db_engine_and_session):
    _, session = test_db_engine_and_session
    service = KillSwitchService(session)

    assert service.is_enabled("kst-fresh-agent-a") is False
    assert service.is_halted("kst-fresh-agent-a") is False


def test_halting_one_agent_does_not_affect_another(test_db_engine_and_session):
    """The whole point of per-agent scoping: one agent's halt is isolated."""
    _, session = test_db_engine_and_session
    service = KillSwitchService(session)

    try:
        service.set_state(enabled=True, set_by="test", scope="kst-agent-x")

        assert service.is_halted("kst-agent-x") is True
        assert service.is_halted("kst-agent-y") is False
        assert service.is_enabled(GLOBAL_SCOPE) is False, "an agent-scoped halt must not touch the global switch"
    finally:
        service.set_state(enabled=False, set_by="test_cleanup", scope="kst-agent-x")


def test_global_switch_halts_every_agent_regardless_of_own_state(test_db_engine_and_session):
    """The fleet-wide switch is a backstop that overrides every agent's own state."""
    _, session = test_db_engine_and_session
    service = KillSwitchService(session)

    try:
        service.set_state(enabled=True, set_by="test")  # defaults to GLOBAL_SCOPE

        assert service.is_halted("kst-agent-never-touched") is True
        assert service.is_halted("kst-another-agent") is True
    finally:
        service.set_state(enabled=False, set_by="test_cleanup")  # clear global again


def test_clearing_an_agents_own_switch_does_not_clear_the_global_switch(test_db_engine_and_session):
    """Clearing one agent's own halt while the global switch is still on must leave it halted."""
    _, session = test_db_engine_and_session
    service = KillSwitchService(session)

    try:
        service.set_state(enabled=True, set_by="test")  # global on
        service.set_state(enabled=True, set_by="test", scope="kst-agent-z")
        service.set_state(enabled=False, set_by="test", scope="kst-agent-z")  # clear only the agent's own switch

        assert service.is_enabled("kst-agent-z") is False
        assert service.is_halted("kst-agent-z") is True, "the still-enabled global switch must still halt it"
    finally:
        service.set_state(enabled=False, set_by="test_cleanup")
        service.set_state(enabled=False, set_by="test_cleanup", scope="kst-agent-z")


def test_known_agent_scopes_excludes_global_and_includes_touched_agents(test_db_engine_and_session):
    _, session = test_db_engine_and_session
    service = KillSwitchService(session)

    service.set_state(enabled=True, set_by="test", scope="kst-registry-probe")
    service.set_state(enabled=False, set_by="test", scope="kst-registry-probe")  # still creates/keeps the row

    scopes = service.known_agent_scopes()
    assert "kst-registry-probe" in scopes
    assert GLOBAL_SCOPE not in scopes
