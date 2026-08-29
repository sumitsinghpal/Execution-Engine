"""
Tests for src/execution/price_alerts.py — one-shot "notify me when
symbol crosses price" alerts, checked against real quotes on a timer
and fired through the shared notification webhook.
"""

import pytest

from src.config import Settings
from src.execution.price_alerts import AlertCondition, PriceAlertService, check_alerts_once


def _settings(**overrides):
    defaults = dict(_env_file=None, env="test")
    defaults.update(overrides)
    return Settings(**defaults)


class _FakeQuoteBroker:
    def __init__(self, prices: dict[str, float]):
        self.prices = prices

    async def get_quote(self, symbol):
        if symbol not in self.prices:
            raise RuntimeError(f"no quote for {symbol}")
        return {"symbol": symbol, "last": self.prices[symbol]}


class TestCreateAlert:
    def test_create_stores_uppercased_symbol(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        service = PriceAlertService(session)

        alert = service.create("qqq", "above", 700.0, created_by="sumit")

        assert alert.symbol == "QQQ"
        assert alert.condition == "ABOVE"
        assert alert.active is True

    def test_create_rejects_invalid_condition(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        service = PriceAlertService(session)
        with pytest.raises(ValueError):
            service.create("QQQ", "SIDEWAYS", 700.0, created_by="sumit")

    def test_create_rejects_non_positive_target(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        service = PriceAlertService(session)
        with pytest.raises(ValueError):
            service.create("QQQ", "ABOVE", 0, created_by="sumit")


class TestListAndCancel:
    def test_active_only_excludes_canceled(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        service = PriceAlertService(session)
        alive = service.create("QQQ", "ABOVE", 700.0, created_by="sumit")
        canceled = service.create("SPY", "BELOW", 500.0, created_by="sumit")
        service.cancel(canceled.id)

        active_ids = [a.id for a in service.list_all(active_only=True)]

        assert alive.id in active_ids
        assert canceled.id not in active_ids

    def test_cancel_an_already_canceled_alert_returns_none(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        service = PriceAlertService(session)
        alert = service.create("QQQ", "ABOVE", 700.0, created_by="sumit")
        service.cancel(alert.id)

        assert service.cancel(alert.id) is None

    def test_cancel_unknown_id_returns_none(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        service = PriceAlertService(session)
        assert service.cancel(999999) is None


class TestCheckAlertsOnce:
    """
    check_alerts_once() operates over EVERY active alert system-wide, by
    design (a real background job has no per-test scope) — so unlike
    most of this codebase's tests, these can't rely on the shared-DB
    session's usual isolation trick of a unique agent_id/scope filter.
    Every test here uses its own invented, never-reused symbol
    (ZALQ-prefixed) so an alert left active by one test (e.g. one that
    deliberately doesn't fire) can't be mistaken for another test's own
    alert by symbol — but check_alerts_once still iterates every active
    alert regardless of symbol, so a test asserting an EXACT fired/
    fetch count also needs no other test's leftover active alert in the
    table at all; the autouse fixture below clears the slate for those.
    """

    @pytest.fixture(autouse=True)
    def _clean_slate(self, test_db_engine_and_session):
        """Deactivates every alert left active by an earlier test before each test in this class runs — see class docstring."""
        _, session = test_db_engine_and_session
        service = PriceAlertService(session)
        for alert in service.list_all(active_only=True):
            service.cancel(alert.id)

    @pytest.mark.asyncio
    async def test_fires_when_price_crosses_above(self, test_db_engine_and_session, monkeypatch):
        _, session = test_db_engine_and_session
        service = PriceAlertService(session)
        alert = service.create("ZALQA", "ABOVE", 700.0, created_by="sumit")
        broker = _FakeQuoteBroker({"ZALQA": 705.0})
        calls = []
        monkeypatch.setattr("src.execution.price_alerts.notify_sync", lambda settings, text: calls.append(text))

        fired = await check_alerts_once(session, _settings(), broker)

        assert fired == 1
        assert len(calls) == 1
        assert "ZALQA" in calls[0]
        session.refresh(alert)
        assert alert.active is False
        assert alert.triggered_price == 705.0

    @pytest.mark.asyncio
    async def test_does_not_fire_when_condition_not_met(self, test_db_engine_and_session, monkeypatch):
        _, session = test_db_engine_and_session
        service = PriceAlertService(session)
        alert = service.create("ZALQB", "ABOVE", 700.0, created_by="sumit")
        broker = _FakeQuoteBroker({"ZALQB": 650.0})
        monkeypatch.setattr("src.execution.price_alerts.notify_sync", lambda settings, text: None)

        fired = await check_alerts_once(session, _settings(), broker)

        assert fired == 0
        session.refresh(alert)
        assert alert.active is True

    @pytest.mark.asyncio
    async def test_fires_when_price_crosses_below(self, test_db_engine_and_session, monkeypatch):
        _, session = test_db_engine_and_session
        service = PriceAlertService(session)
        service.create("ZALQC", "BELOW", 500.0, created_by="sumit")
        broker = _FakeQuoteBroker({"ZALQC": 495.0})
        monkeypatch.setattr("src.execution.price_alerts.notify_sync", lambda settings, text: None)

        fired = await check_alerts_once(session, _settings(), broker)

        assert fired == 1

    @pytest.mark.asyncio
    async def test_one_symbols_quote_failure_does_not_block_the_others(self, test_db_engine_and_session, monkeypatch):
        _, session = test_db_engine_and_session
        service = PriceAlertService(session)
        service.create("ZALQBAD", "ABOVE", 100.0, created_by="sumit")
        service.create("ZALQD", "ABOVE", 700.0, created_by="sumit")
        broker = _FakeQuoteBroker({"ZALQD": 705.0})  # ZALQBAD deliberately has no quote
        monkeypatch.setattr("src.execution.price_alerts.notify_sync", lambda settings, text: None)

        fired = await check_alerts_once(session, _settings(), broker)

        assert fired == 1  # ZALQD's alert still fired despite ZALQBAD failing

    @pytest.mark.asyncio
    async def test_no_active_alerts_is_a_cheap_no_op(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        # No alert created in this test — but earlier tests in this class
        # may have left one active if it deliberately didn't fire, so
        # this only asserts the no-op PATH is reachable (a fresh broker
        # with no prices raises on every get_quote), not a global zero.
        fired = await check_alerts_once(session, _settings(), _FakeQuoteBroker({}))
        assert fired == 0

    @pytest.mark.asyncio
    async def test_multiple_alerts_on_the_same_symbol_share_one_quote_fetch(self, test_db_engine_and_session, monkeypatch):
        _, session = test_db_engine_and_session
        service = PriceAlertService(session)
        service.create("ZALQE", "ABOVE", 700.0, created_by="sumit")
        service.create("ZALQE", "ABOVE", 690.0, created_by="sumit")
        fetch_count = {"n": 0}

        class CountingBroker:
            async def get_quote(self, symbol):
                fetch_count["n"] += 1
                return {"symbol": symbol, "last": 705.0}

        monkeypatch.setattr("src.execution.price_alerts.notify_sync", lambda settings, text: None)

        fired = await check_alerts_once(session, _settings(), CountingBroker())

        assert fired == 2  # both alerts on QQQ fire
        assert fetch_count["n"] == 1  # but only one quote was fetched for the shared symbol
