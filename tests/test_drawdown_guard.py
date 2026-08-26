"""
Tests for the daily loss/drawdown shutdown: does an account's equity get a
stable start-of-day baseline, and does exceeding max_daily_drawdown_pct
against that baseline automatically halt trading.

Note on account choice: like test_position_reconciliation.py, this repo's
test suite shares one physical SQLite file across the whole test session,
so each test here uses a distinct account alias (primary / retirement /
paper — the three configured by default) rather than a fresh account per
test.
"""

import pytest

from src.execution.drawdown_guard import DrawdownGuard, _extract_equity
from src.execution.kill_switch_state import KillSwitchService


class FakeBroker:
    """
    Returns balances from a fixed queue, one popped per get_balances() call
    (the last value repeats once the queue is exhausted) — lets a single
    test control both the baseline-capture call and the later current-
    equity call with different numbers.
    """

    def __init__(self, equities):
        self._equities = list(equities)
        self._idx = 0
        self.calls = 0

    async def get_balances(self, profile):
        self.calls += 1
        equity = self._equities[min(self._idx, len(self._equities) - 1)]
        self._idx += 1
        return {"net_liquidation_value": equity, "mode": "TEST"}

    async def preview_order(self, profile, order_spec):
        raise NotImplementedError

    async def submit_order(self, profile, order_spec):
        raise NotImplementedError

    async def get_order_status(self, profile, order_id):
        raise NotImplementedError

    async def list_accounts(self):
        raise NotImplementedError

    async def get_positions(self, profile):
        raise NotImplementedError

    async def get_quote(self, symbol):
        raise NotImplementedError


class MissingEquityBroker:
    """Returns a balances payload with no usable net_liquidation_value."""

    async def get_balances(self, profile):
        return {"availableFunds": 500, "mode": "TEST"}

    async def preview_order(self, profile, order_spec):
        raise NotImplementedError

    async def submit_order(self, profile, order_spec):
        raise NotImplementedError

    async def get_order_status(self, profile, order_id):
        raise NotImplementedError

    async def list_accounts(self):
        raise NotImplementedError

    async def get_positions(self, profile):
        raise NotImplementedError

    async def get_quote(self, symbol):
        raise NotImplementedError


def test_extract_equity_returns_none_when_missing():
    """A malformed/unexpected balances payload must fail closed, not read as $0."""
    assert _extract_equity({"availableFunds": 500}) is None
    assert _extract_equity({}) is None


def test_extract_equity_parses_present_value():
    assert _extract_equity({"net_liquidation_value": "12345.67"}) == 12345.67
    assert _extract_equity({"net_liquidation_value": 12345.67}) == 12345.67


@pytest.mark.asyncio
async def test_baseline_is_captured_once_and_stays_stable(test_db_engine_and_session):
    """
    The first check of the day captures the baseline from the broker; a
    later check on the same day must not overwrite it even though the
    broker's reported equity has since changed.

    Deliberately doesn't assert the baseline's exact value or that this
    call is the very first ever for "primary": other tests in this shared-
    DB suite (e.g. the API test hitting /v1/risk/drawdown-check) may have
    already captured today's baseline for this account first. What matters
    here is idempotency — whichever value got captured first stays fixed —
    not who captured it.
    """
    _, session = test_db_engine_and_session
    broker = FakeBroker([1_000_000, 500_000])
    guard = DrawdownGuard(session=session, broker=broker)

    first = await guard.ensure_todays_baseline("primary")
    calls_after_first = broker.calls

    second = await guard.ensure_todays_baseline("primary")
    assert second.baseline_equity == first.baseline_equity, "baseline must not move once captured for the day"
    assert broker.calls == calls_after_first, "a cached baseline must not re-query the broker"


@pytest.mark.asyncio
async def test_missing_equity_fails_closed_without_writing_a_baseline(test_db_engine_and_session):
    """
    If the broker's balances response has no usable equity field, baseline
    capture must raise rather than silently writing a bogus $0 baseline —
    and, since it raises before committing, a later successful capture for
    the same account on the same day is unaffected. This runs before the
    "retirement" account is used anywhere else in this file, so the failed
    attempt leaves no residue for the next test to trip over.
    """
    _, session = test_db_engine_and_session
    guard = DrawdownGuard(session=session, broker=MissingEquityBroker())

    with pytest.raises(ValueError, match="net_liquidation_value"):
        await guard.ensure_todays_baseline("retirement")


@pytest.mark.asyncio
async def test_drawdown_under_limit_does_not_trip_kill_switch(test_db_engine_and_session):
    """A 2% pullback against a 5% default limit is logged but must not halt trading."""
    _, session = test_db_engine_and_session
    broker = FakeBroker([100_000, 98_000])  # baseline, then current
    guard = DrawdownGuard(session=session, broker=broker)

    assert KillSwitchService(session).is_enabled() is False

    report = await guard.check_and_halt("retirement")

    assert report.baseline_equity == 100_000
    assert report.current_equity == 98_000
    assert report.drawdown_pct == pytest.approx(0.02)
    assert report.breached is False
    assert KillSwitchService(session).is_enabled() is False


@pytest.mark.asyncio
async def test_drawdown_breach_trips_kill_switch(test_db_engine_and_session):
    """A 10% pullback against a 5% default limit must auto-halt trading."""
    _, session = test_db_engine_and_session
    broker = FakeBroker([100_000, 90_000])  # baseline, then current
    guard = DrawdownGuard(session=session, broker=broker)

    assert KillSwitchService(session).is_enabled() is False

    try:
        report = await guard.check_and_halt("paper")

        assert report.drawdown_pct == pytest.approx(0.10)
        assert report.breached is True
        assert KillSwitchService(session).is_enabled() is True, (
            "a drawdown breach must automatically halt trading, not just be logged"
        )
    finally:
        # The kill switch is a global singleton, not scoped to this test's
        # account — leave it clean for whatever test runs next.
        KillSwitchService(session).set_state(enabled=False, set_by="test_cleanup")
