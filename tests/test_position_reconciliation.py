"""
Tests for pre-session position reconciliation: does this system's own
believed positions (summed from its fill history) match what the broker
actually reports for the account.

Note on account choice: this repo's test suite shares one physical SQLite
file across the whole test session (test_settings is session-scoped), so
order records persist across tests within a session. Because position
reconciliation aggregates *all* orders for an account, tests here each use
a distinct account alias (primary / retirement / paper — the three
configured by default) rather than relying on a fresh account per test, the
same way other test files in this repo use unique decision_ids for the same
reason.
"""

from datetime import datetime

import pytest

from src.execution.executor import OrderRecord
from src.execution.kill_switch_state import KillSwitchService
from src.execution.position_reconciliation import PositionReconciliationService
from src.models.orders import OrderStatus


class FakeBroker:
    """Returns a fixed, caller-specified set of broker positions."""

    def __init__(self, positions):
        self._positions = positions

    async def get_positions(self, profile):
        return self._positions

    async def preview_order(self, profile, order_spec):
        raise NotImplementedError

    async def submit_order(self, profile, order_spec):
        raise NotImplementedError

    async def get_order_status(self, profile, order_id):
        raise NotImplementedError

    async def list_accounts(self):
        raise NotImplementedError

    async def get_balances(self, profile):
        raise NotImplementedError


def _make_filled_order(session, account, symbol, instruction, filled_quantity):
    order = OrderRecord(
        decision_id=f"recon-test-{symbol}-{instruction}-{filled_quantity}-{datetime.utcnow().timestamp()}",
        account=account,
        symbol=symbol,
        quantity=filled_quantity,
        instruction=instruction,
        status=OrderStatus.FILLED.value,
        payload_checksum="test-checksum",
        filled_quantity=filled_quantity,
    )
    session.add(order)
    session.commit()
    return order


@pytest.mark.asyncio
async def test_reconciliation_matches_and_leaves_kill_switch_untouched(test_db_engine_and_session):
    _, session = test_db_engine_and_session
    _make_filled_order(session, "primary", "QQQ", "BUY", 100)

    broker = FakeBroker([{"instrument": {"symbol": "QQQ"}, "longQuantity": 100, "shortQuantity": 0}])
    service = PositionReconciliationService(session=session, broker=broker)

    report = await service.reconcile("primary")
    assert report.matched is True
    assert report.mismatches == []
    assert report.local_positions == {"QQQ": 100}
    assert report.broker_positions == {"QQQ": 100}

    # reconcile_or_halt must not touch the kill switch when everything matches
    report2 = await service.reconcile_or_halt("primary")
    assert report2.matched is True
    assert KillSwitchService(session).is_enabled() is False


@pytest.mark.asyncio
async def test_reconciliation_detects_mismatch(test_db_engine_and_session):
    _, session = test_db_engine_and_session
    _make_filled_order(session, "retirement", "SPY", "BUY", 50)

    # Broker shows fewer shares than our own fill history believes —
    # exactly the kind of drift this check exists to catch.
    broker = FakeBroker([{"instrument": {"symbol": "SPY"}, "longQuantity": 30, "shortQuantity": 0}])
    service = PositionReconciliationService(session=session, broker=broker)

    report = await service.reconcile("retirement")

    assert report.matched is False
    assert len(report.mismatches) == 1
    mismatch = report.mismatches[0]
    assert mismatch.symbol == "SPY"
    assert mismatch.local_quantity == 50
    assert mismatch.broker_quantity == 30
    assert mismatch.delta == -20


@pytest.mark.asyncio
async def test_reconcile_or_halt_trips_kill_switch_on_mismatch(test_db_engine_and_session):
    _, session = test_db_engine_and_session
    _make_filled_order(session, "paper", "IWM", "BUY", 10)

    broker = FakeBroker([])  # broker reports nothing — total drift
    service = PositionReconciliationService(session=session, broker=broker)

    assert KillSwitchService(session).is_enabled() is False

    try:
        report = await service.reconcile_or_halt("paper")

        assert report.matched is False
        assert KillSwitchService(session).is_enabled() is True, (
            "a position mismatch must automatically halt trading, not just be logged"
        )
    finally:
        # The kill switch is a global singleton, not scoped to this test's
        # account — leave it clean for whatever test runs next.
        KillSwitchService(session).set_state(enabled=False, set_by="test_cleanup")
