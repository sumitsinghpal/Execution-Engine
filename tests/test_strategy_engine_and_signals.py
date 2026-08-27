"""Tests for src/strategy/engine.py, src/execution/strategy_signals.py, and the background scanner."""

from typing import Any, Optional

import pytest

from src.config import Settings
from src.execution.strategy_scanner import scan_once
from src.execution.strategy_signals import SignalStatus, StrategySignalService
from src.strategy import engine as strategy_engine
from src.strategy.catalog import STRATEGIES, SignalDetail


class _FixedBarsBroker:
    """A broker double whose get_price_history always returns exactly the bars it was given."""

    def __init__(self, bars: list[dict[str, Any]]):
        self._bars = bars
        self.calls = 0

    async def get_price_history(self, symbol, bar_interval, lookback_days):
        self.calls += 1
        return self._bars

    async def preview_order(self, *a, **k): raise NotImplementedError
    async def submit_order(self, *a, **k): raise NotImplementedError
    async def get_order_status(self, *a, **k): raise NotImplementedError
    async def list_accounts(self): raise NotImplementedError
    async def get_positions(self, *a, **k): raise NotImplementedError
    async def get_balances(self, *a, **k): raise NotImplementedError
    async def get_quote(self, *a, **k): raise NotImplementedError


def _flat_bars(n=250, price=100.0):
    return [
        {"timestamp": f"2026-01-{(i % 28) + 1:02d}T00:00:00Z", "open": price, "high": price, "low": price, "close": price, "volume": 1000}
        for i in range(n)
    ]


class TestEngineScan:
    @pytest.mark.asyncio
    async def test_unknown_strategy_id_raises(self):
        broker = _FixedBarsBroker(_flat_bars())
        with pytest.raises(strategy_engine.UnknownStrategyError):
            await strategy_engine.scan(broker, "QQQ", "not-a-real-strategy")

    @pytest.mark.asyncio
    async def test_flat_bars_produce_no_signal_for_every_strategy(self):
        """Flat, unmoving bars should never satisfy any strategy's entry rule."""
        broker = _FixedBarsBroker(_flat_bars())
        for strategy_id in STRATEGIES:
            signal = await strategy_engine.scan(broker, "QQQ", strategy_id)
            assert signal is None, f"{strategy_id} unexpectedly fired on flat bars"

    @pytest.mark.asyncio
    async def test_fetch_bars_converts_raw_dicts_to_bar_objects(self):
        broker = _FixedBarsBroker(_flat_bars(n=5))
        strategy = STRATEGIES["orb"]
        bars = await strategy_engine.fetch_bars(broker, "QQQ", strategy)
        assert len(bars) == 5
        assert bars[0].close == 100.0


class TestStrategySignalService:
    def test_record_if_new_persists_a_signal(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        service = StrategySignalService(session)
        detail = SignalDetail(entry_price=100, stop_loss_price=95, take_profit_price=110, rationale="test")

        record = service.record_if_new("orb", "SIG-NEW-1", detail)

        assert record is not None
        assert record.status == SignalStatus.PENDING
        assert record.entry_price == 100

    def test_duplicate_pending_signal_same_day_is_not_recorded_twice(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        service = StrategySignalService(session)
        detail = SignalDetail(entry_price=100, stop_loss_price=95, take_profit_price=110, rationale="test")

        first = service.record_if_new("orb", "SIG-DUP-1", detail)
        second = service.record_if_new("orb", "SIG-DUP-1", detail)

        assert first is not None
        assert second is None

    def test_dismissed_signal_can_be_recorded_again(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        service = StrategySignalService(session)
        detail = SignalDetail(entry_price=100, stop_loss_price=95, take_profit_price=110, rationale="test")

        first = service.record_if_new("orb", "SIG-REFIRE-1", detail)
        service.dismiss(first.id)
        second = service.record_if_new("orb", "SIG-REFIRE-1", detail)

        assert second is not None
        assert second.id != first.id

    def test_list_signals_filters_by_status(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        service = StrategySignalService(session)
        detail = SignalDetail(entry_price=100, stop_loss_price=95, take_profit_price=110, rationale="test")
        record = service.record_if_new("orb", "SIG-LIST-1", detail)

        pending = service.list_signals(status=SignalStatus.PENDING)
        assert any(r.id == record.id for r in pending)

        service.dismiss(record.id)
        pending_after = service.list_signals(status=SignalStatus.PENDING)
        assert not any(r.id == record.id for r in pending_after)

        dismissed = service.list_signals(status=SignalStatus.DISMISSED)
        assert any(r.id == record.id for r in dismissed)

    def test_dismiss_unknown_id_raises(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        with pytest.raises(ValueError):
            StrategySignalService(session).dismiss(999_999)

    def test_to_dict_includes_strategy_metadata(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        service = StrategySignalService(session)
        detail = SignalDetail(entry_price=100, stop_loss_price=95, take_profit_price=110, rationale="test")
        record = service.record_if_new("orb", "SIG-DICT-1", detail)

        d = record.to_dict()
        assert d["strategy_name"] == STRATEGIES["orb"].name
        assert d["category"] == STRATEGIES["orb"].category.value


class TestScanOnce:
    @pytest.mark.asyncio
    async def test_scan_once_records_nothing_for_flat_bars(self, test_db_engine_and_session, monkeypatch):
        _, session = test_db_engine_and_session
        settings = Settings(_env_file=None, env="test", STRATEGY_WATCHLIST="ZZZZ")

        async def fake_scan(broker, symbol, strategy_id):
            return None

        monkeypatch.setattr(strategy_engine, "scan", fake_scan)
        new_count = await scan_once(session, settings)

        assert new_count == 0

    @pytest.mark.asyncio
    async def test_scan_once_records_a_fired_signal(self, test_db_engine_and_session, monkeypatch):
        _, session = test_db_engine_and_session
        settings = Settings(_env_file=None, env="test", STRATEGY_WATCHLIST="ZZZZ")
        detail = SignalDetail(entry_price=50, stop_loss_price=45, take_profit_price=60, rationale="forced")

        async def fake_scan(broker, symbol, strategy_id):
            return detail if strategy_id == "orb" else None

        monkeypatch.setattr(strategy_engine, "scan", fake_scan)
        new_count = await scan_once(session, settings)

        assert new_count == 1
        pending = StrategySignalService(session).list_signals(status=SignalStatus.PENDING)
        assert any(r.symbol == "ZZZZ" and r.strategy_id == "orb" for r in pending)

    @pytest.mark.asyncio
    async def test_scan_once_continues_past_a_failing_strategy(self, test_db_engine_and_session, monkeypatch):
        """One strategy raising must not stop the rest of the pass."""
        _, session = test_db_engine_and_session
        settings = Settings(_env_file=None, env="test", STRATEGY_WATCHLIST="ZZZZ")
        detail = SignalDetail(entry_price=50, stop_loss_price=45, take_profit_price=60, rationale="forced")

        async def flaky_scan(broker, symbol, strategy_id):
            if strategy_id == "orb":
                raise RuntimeError("simulated broker outage")
            return detail if strategy_id == "vwap_reversion" else None

        monkeypatch.setattr(strategy_engine, "scan", flaky_scan)
        new_count = await scan_once(session, settings)

        assert new_count == 1
