"""
Tests for src/execution/bracket_orders.py — attaching a stop-loss /
take-profit / trailing-stop exit plan to an already-executed BUY order,
then auto-closing it once a level is hit.

Every test uses its own unique symbol (test_db_engine_and_session shares
one on-disk SQLite file across the whole test session — see conftest.py —
so a fixed symbol reused across tests would let one test's leftover OPEN
bracket leak into another's assertions), same convention as
tests/test_autonomous_trader.py.
"""

from datetime import UTC, datetime

import pytest

import src.execution.executor as executor_module
import src.risk.limits as risk_limits_module
from src.config import Settings
from src.execution.bracket_orders import BracketOrderService, BracketOrderStatus, manage_bracket_orders

_ALL_TEST_SYMBOLS = "QQQ,SPY,IWM,EEM,GLD,TLT,ZBRKA,ZBRKB,ZBRKC,ZBRKD,ZBRKE,ZBRKF,ZBRKX"


def _settings(monkeypatch, **overrides):
    """
    Builds a Settings instance AND makes it the one Executor and
    RiskChecker actually see — both call the module-level get_settings()
    themselves rather than accepting an injected Settings (same technique
    tests/test_autonomous_trader.py uses; see its _settings() docstring).
    """
    defaults = dict(
        _env_file=None,
        env="test",
        kill_switch_enabled=False,
        api_key_admin="change-me-in-prod",
        SYMBOL_ALLOWLIST=_ALL_TEST_SYMBOLS,
    )
    defaults.update(overrides)
    settings = Settings(**defaults)
    monkeypatch.setattr(executor_module, "get_settings", lambda: settings)
    monkeypatch.setattr(risk_limits_module, "get_settings", lambda: settings)
    return settings


class _FakeQuoteBroker:
    """Same shape as PaperBrokerAdapter — enough for manage_bracket_orders to price and submit the exit through the real Executor/RiskChecker."""

    def __init__(self, prices: dict[str, float]):
        self.prices = prices

    async def get_quote(self, symbol):
        if symbol not in self.prices:
            raise RuntimeError(f"no quote for {symbol}")
        price = self.prices[symbol]
        return {"symbol": symbol, "bid": price, "ask": price, "last": price, "quote_time": datetime.now(UTC).isoformat(), "mode": "TEST"}

    async def preview_order(self, profile, order_spec):
        quantity = order_spec.get("quantity", 1)
        price = self.prices.get(order_spec.get("symbol"), 100.0)
        return {"estimatedTotalInvestment": quantity * price, "estimatedCommission": 0.0, "status": "OK"}

    async def submit_order(self, profile, order_spec):
        return {"orderId": "fake-exit-order", "status": "ACCEPTED"}


async def _noop_notify(settings, text):
    return None


class TestAttach:
    def test_attach_stores_fixed_stop_and_target(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        service = BracketOrderService(session)

        record = service.attach(
            entry_decision_id="edge-bracket-001",
            account="primary",
            symbol="qqq",
            quantity=10,
            entry_price=400.0,
            stop_loss_price=390.0,
            take_profit_price=420.0,
            trailing_stop_pct=None,
            created_by="sumit",
        )

        assert record.symbol == "QQQ"
        assert record.status == BracketOrderStatus.OPEN
        assert record.stop_loss_price == "390.0"
        assert record.take_profit_price == "420.0"
        assert record.trailing_stop_pct is None
        assert record.highest_price_seen is None

    def test_attach_with_trailing_stop_seeds_highest_price_at_entry(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        service = BracketOrderService(session)

        record = service.attach(
            entry_decision_id="edge-bracket-002",
            account="primary",
            symbol="QQQ",
            quantity=10,
            entry_price=400.0,
            stop_loss_price=None,
            take_profit_price=None,
            trailing_stop_pct=0.05,
            created_by="sumit",
        )

        assert record.trailing_stop_pct == "0.05"
        assert record.highest_price_seen == "400.0"

    def test_attaching_twice_to_the_same_order_raises(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        service = BracketOrderService(session)
        service.attach(
            entry_decision_id="edge-bracket-003", account="primary", symbol="QQQ", quantity=10,
            entry_price=400.0, stop_loss_price=390.0, take_profit_price=None, trailing_stop_pct=None, created_by="sumit",
        )
        with pytest.raises(ValueError):
            service.attach(
                entry_decision_id="edge-bracket-003", account="primary", symbol="QQQ", quantity=10,
                entry_price=400.0, stop_loss_price=380.0, take_profit_price=None, trailing_stop_pct=None, created_by="sumit",
            )


class TestCancel:
    def test_cancel_an_open_bracket_stops_it_without_selling(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        service = BracketOrderService(session)
        record = service.attach(
            entry_decision_id="edge-bracket-004", account="primary", symbol="QQQ", quantity=10,
            entry_price=400.0, stop_loss_price=390.0, take_profit_price=None, trailing_stop_pct=None, created_by="sumit",
        )

        canceled = service.cancel(record.id)

        assert canceled.status == BracketOrderStatus.CANCELED
        assert canceled.closed_at is not None
        assert canceled.exit_decision_id is None

    def test_cancel_unknown_id_returns_none(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        assert BracketOrderService(session).cancel(999999) is None

    def test_cancel_an_already_closed_bracket_returns_none(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        service = BracketOrderService(session)
        record = service.attach(
            entry_decision_id="edge-bracket-005", account="primary", symbol="QQQ", quantity=10,
            entry_price=400.0, stop_loss_price=390.0, take_profit_price=None, trailing_stop_pct=None, created_by="sumit",
        )
        service.cancel(record.id)
        assert service.cancel(record.id) is None


class TestManageBracketOrders:
    @pytest.mark.asyncio
    async def test_closes_on_take_profit(self, test_db_engine_and_session, monkeypatch):
        _, session = test_db_engine_and_session
        service = BracketOrderService(session)
        service.attach(
            entry_decision_id="edge-bracket-tp", account="primary", symbol="ZBRKA", quantity=5,
            entry_price=100.0, stop_loss_price=90.0, take_profit_price=110.0, trailing_stop_pct=None, created_by="sumit",
        )
        monkeypatch.setattr("src.execution.bracket_orders.notify", _noop_notify)
        settings = _settings(monkeypatch)
        broker = _FakeQuoteBroker({"ZBRKA": 111.0})

        closed = await manage_bracket_orders(session, settings, broker)

        assert closed == 1
        records = service.list_all()
        record = next(r for r in records if r.entry_decision_id == "edge-bracket-tp")
        assert record.status == BracketOrderStatus.CLOSED_TARGET
        assert record.exit_price == "111.0"

    @pytest.mark.asyncio
    async def test_closes_on_fixed_stop_loss(self, test_db_engine_and_session, monkeypatch):
        _, session = test_db_engine_and_session
        service = BracketOrderService(session)
        service.attach(
            entry_decision_id="edge-bracket-sl", account="primary", symbol="ZBRKB", quantity=5,
            entry_price=100.0, stop_loss_price=90.0, take_profit_price=110.0, trailing_stop_pct=None, created_by="sumit",
        )
        monkeypatch.setattr("src.execution.bracket_orders.notify", _noop_notify)
        settings = _settings(monkeypatch)
        broker = _FakeQuoteBroker({"ZBRKB": 85.0})

        closed = await manage_bracket_orders(session, settings, broker)

        assert closed == 1
        record = next(r for r in service.list_all() if r.entry_decision_id == "edge-bracket-sl")
        assert record.status == BracketOrderStatus.CLOSED_STOP

    @pytest.mark.asyncio
    async def test_does_not_close_while_between_stop_and_target(self, test_db_engine_and_session, monkeypatch):
        _, session = test_db_engine_and_session
        service = BracketOrderService(session)
        service.attach(
            entry_decision_id="edge-bracket-mid", account="primary", symbol="ZBRKC", quantity=5,
            entry_price=100.0, stop_loss_price=90.0, take_profit_price=110.0, trailing_stop_pct=None, created_by="sumit",
        )
        monkeypatch.setattr("src.execution.bracket_orders.notify", _noop_notify)
        settings = _settings(monkeypatch)
        broker = _FakeQuoteBroker({"ZBRKC": 102.0})

        closed = await manage_bracket_orders(session, settings, broker)

        assert closed == 0
        record = next(r for r in service.list_all() if r.entry_decision_id == "edge-bracket-mid")
        assert record.status == BracketOrderStatus.OPEN

    @pytest.mark.asyncio
    async def test_trailing_stop_ratchets_up_and_then_triggers_on_pullback(self, test_db_engine_and_session, monkeypatch):
        _, session = test_db_engine_and_session
        service = BracketOrderService(session)
        record = service.attach(
            entry_decision_id="edge-bracket-trail", account="primary", symbol="ZBRKD", quantity=5,
            entry_price=100.0, stop_loss_price=None, take_profit_price=None, trailing_stop_pct=0.10, created_by="sumit",
        )
        monkeypatch.setattr("src.execution.bracket_orders.notify", _noop_notify)
        settings = _settings(monkeypatch)

        # Price rises to 150 — trailing stop should ratchet up to 135 (10% below 150) and NOT fire yet.
        closed = await manage_bracket_orders(session, settings, _FakeQuoteBroker({"ZBRKD": 150.0}))
        assert closed == 0
        session.refresh(record)
        assert record.highest_price_seen == "150.0"

        # Price pulls back to 130 — below the 135 ratcheted stop — should now close.
        closed = await manage_bracket_orders(session, settings, _FakeQuoteBroker({"ZBRKD": 130.0}))
        assert closed == 1
        record = next(r for r in service.list_all() if r.entry_decision_id == "edge-bracket-trail")
        assert record.status == BracketOrderStatus.CLOSED_TRAILING_STOP

    @pytest.mark.asyncio
    async def test_a_canceled_bracket_is_not_managed(self, test_db_engine_and_session, monkeypatch):
        _, session = test_db_engine_and_session
        service = BracketOrderService(session)
        record = service.attach(
            entry_decision_id="edge-bracket-canceled", account="primary", symbol="ZBRKE", quantity=5,
            entry_price=100.0, stop_loss_price=90.0, take_profit_price=110.0, trailing_stop_pct=None, created_by="sumit",
        )
        service.cancel(record.id)
        monkeypatch.setattr("src.execution.bracket_orders.notify", _noop_notify)
        settings = _settings(monkeypatch)

        closed = await manage_bracket_orders(session, settings, _FakeQuoteBroker({"ZBRKE": 200.0}))

        assert closed == 0
        record = next(r for r in service.list_all() if r.entry_decision_id == "edge-bracket-canceled")
        assert record.status == BracketOrderStatus.CANCELED

    @pytest.mark.asyncio
    async def test_one_symbols_quote_failure_does_not_block_the_others(self, test_db_engine_and_session, monkeypatch):
        _, session = test_db_engine_and_session
        service = BracketOrderService(session)
        service.attach(
            entry_decision_id="edge-bracket-bad", account="primary", symbol="ZBRKX", quantity=5,
            entry_price=100.0, stop_loss_price=90.0, take_profit_price=110.0, trailing_stop_pct=None, created_by="sumit",
        )
        service.attach(
            entry_decision_id="edge-bracket-good", account="primary", symbol="ZBRKF", quantity=5,
            entry_price=100.0, stop_loss_price=90.0, take_profit_price=110.0, trailing_stop_pct=None, created_by="sumit",
        )
        monkeypatch.setattr("src.execution.bracket_orders.notify", _noop_notify)
        settings = _settings(monkeypatch)
        broker = _FakeQuoteBroker({"ZBRKF": 111.0})  # ZBRKX deliberately has no quote

        closed = await manage_bracket_orders(session, settings, broker)

        assert closed == 1
