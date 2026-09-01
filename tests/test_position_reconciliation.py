"""
Tests for pre-session position reconciliation: does this system's own
believed positions (summed from its fill history) match what the broker
actually reports for the account.

Note on account choice: this repo's test suite shares one physical SQLite
file across the whole test session (test_settings is session-scoped), so
order records persist across tests within a session. Because position
reconciliation aggregates *all* orders for an account, tests here each use
a distinct account alias rather than relying on a fresh account per test,
the same way other test files in this repo use unique decision_ids for the
same reason. Only three account aliases are pre-configured
(Settings.account_profiles: primary/retirement/paper) — "primary" in
particular is the default alias nearly every other test file in this repo
uses for its own real executed orders (test_api.py's fixtures, the
bracket-order tests, etc.), so an exact-dict-equality assertion against
"primary"'s full position set is inherently fragile. The first test below
monkeypatches in a fourth, wholly private account instead of reusing one
of the three shared ones.
"""

from datetime import datetime

import pytest

import src.execution.position_reconciliation as position_reconciliation_module
from src.accounts.profiles import AccountProfile, BrokerName
from src.config import get_settings
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

    async def get_quote(self, symbol):
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
async def test_reconciliation_matches_and_leaves_kill_switch_untouched(test_db_engine_and_session, monkeypatch):
    _, session = test_db_engine_and_session
    # A dedicated fourth account, NOT one of the three shared aliases
    # (primary/retirement/paper) — "primary" in particular is the default
    # used throughout the rest of this suite (test_api.py's fixtures, the
    # bracket-order tests, etc.), and now that PaperBrokerAdapter reports a
    # real fill on submit (see Executor.execute_order()), any of those
    # other tests' real executed orders on "primary" would leak into this
    # test's exact-dict-equality assertion below via the shared on-disk DB.
    # get_account_profile() requires a registered profile, so this
    # monkeypatches one in rather than reusing a shared alias.
    account = "recon-isolated-test"
    settings = get_settings()
    settings.account_profiles = {**settings.account_profiles, account: AccountProfile(broker=BrokerName.PAPER)}
    monkeypatch.setattr(position_reconciliation_module, "get_settings", lambda: settings)

    _make_filled_order(session, account, "QQQ", "BUY", 100)

    broker = FakeBroker([{"instrument": {"symbol": "QQQ"}, "longQuantity": 100, "shortQuantity": 0}])
    service = PositionReconciliationService(session=session, broker=broker)

    report = await service.reconcile(account)
    assert report.matched is True
    assert report.mismatches == []
    assert report.local_positions == {"QQQ": 100}
    assert report.broker_positions == {"QQQ": 100}

    # reconcile_or_halt must not touch the kill switch when everything matches
    report2 = await service.reconcile_or_halt(account)
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


class TestPaperBrokerNeverFalselyMismatches:
    """
    Regression coverage for a real bug found while fixing paper-mode fill
    tracking (Executor.execute_order() now correctly reaches FILLED — see
    test_brokers.py's TestPaperAdapterReportsAnImmediateFill): once
    local_positions started being genuinely populated, comparing it
    against PaperBrokerAdapter.get_positions() (hardcoded to always
    return []) would flag every single paper trade as a "mismatch" and
    auto-trip the kill switch via reconcile_or_halt — not a real drift
    signal, just an artifact of a broker adapter with no ledger of its
    own to compare against. See PositionReconciliationService.reconcile()'s
    docstring for the full reasoning.
    """

    @pytest.mark.asyncio
    async def test_a_real_paper_position_with_no_broker_positions_still_matches(self, test_db_engine_and_session, monkeypatch):
        from src.brokers.paper import PaperBrokerAdapter

        _, session = test_db_engine_and_session
        account = "recon-paper-broker-test"
        settings = get_settings()
        settings.account_profiles = {**settings.account_profiles, account: AccountProfile(broker=BrokerName.PAPER)}
        monkeypatch.setattr(position_reconciliation_module, "get_settings", lambda: settings)

        _make_filled_order(session, account, "QQQ", "BUY", 25)
        service = PositionReconciliationService(session=session, broker=PaperBrokerAdapter())

        report = await service.reconcile(account)

        assert report.matched is True
        assert report.mismatches == []
        # local_positions is still honestly reported — only the
        # mismatch/halt behavior is skipped for this broker.
        assert report.local_positions == {"QQQ": 25}
        assert report.broker_positions == {}

    @pytest.mark.asyncio
    async def test_reconcile_or_halt_never_trips_the_kill_switch_for_paper_positions(self, test_db_engine_and_session, monkeypatch):
        from src.brokers.paper import PaperBrokerAdapter

        _, session = test_db_engine_and_session
        account = "recon-paper-broker-halt-test"
        settings = get_settings()
        settings.account_profiles = {**settings.account_profiles, account: AccountProfile(broker=BrokerName.PAPER)}
        monkeypatch.setattr(position_reconciliation_module, "get_settings", lambda: settings)

        _make_filled_order(session, account, "SPY", "BUY", 40)
        service = PositionReconciliationService(session=session, broker=PaperBrokerAdapter())

        assert KillSwitchService(session).is_enabled() is False

        report = await service.reconcile_or_halt(account)

        assert report.matched is True
        assert KillSwitchService(session).is_enabled() is False, (
            "PaperBrokerAdapter has no independent ledger — its always-empty get_positions() must never "
            "be treated as evidence of drift"
        )

    @pytest.mark.asyncio
    async def test_a_non_paper_broker_still_gets_real_mismatch_detection(self, test_db_engine_and_session):
        """The paper-mode special case must not silently swallow a genuine mismatch against a real broker adapter."""
        _, session = test_db_engine_and_session
        _make_filled_order(session, "primary", "GLD", "BUY", 15)

        broker = FakeBroker([{"instrument": {"symbol": "GLD"}, "longQuantity": 5, "shortQuantity": 0}])
        service = PositionReconciliationService(session=session, broker=broker)

        report = await service.reconcile("primary")

        assert report.matched is False
        assert len(report.mismatches) >= 1
        assert any(m.symbol == "GLD" for m in report.mismatches)


class TestGetLocalPositions:
    """
    get_local_positions() — the public, no-broker-call accessor added for
    external read-only callers (e.g. signal-integrity-layer's portfolio-
    impact check). Must never touch self.broker at all, unlike
    reconcile()/reconcile_or_halt().
    """

    def test_returns_the_same_data_reconcile_computes_locally(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        _make_filled_order(session, "local-pos-test", "TLT", "BUY", 12)

        # A broker whose get_positions() would raise if ever called —
        # proves this method genuinely never reaches out to the broker.
        class ExplodingBroker:
            async def get_positions(self, profile):
                raise AssertionError("get_local_positions() must never call the broker")

        service = PositionReconciliationService(session=session, broker=ExplodingBroker())

        positions = service.get_local_positions("local-pos-test")

        assert positions == {"TLT": 12}

    def test_a_sell_after_a_buy_nets_out_correctly(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        account = "local-pos-net-test"
        _make_filled_order(session, account, "EEM", "BUY", 20)
        _make_filled_order(session, account, "EEM", "SELL", 8)

        positions = PositionReconciliationService(session=session).get_local_positions(account)

        assert positions == {"EEM": 12}

    def test_no_orders_for_the_account_returns_an_empty_dict(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        positions = PositionReconciliationService(session=session).get_local_positions("never-traded-account")
        assert positions == {}
