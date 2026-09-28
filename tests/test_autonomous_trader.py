"""
Tests for src/execution/autonomous_trader.py — the fully-autonomous
(no human approval) trading loop. Strategy entry detection itself is
covered by tests/test_strategy_engine_and_signals.py; these tests
monkeypatch strategy_engine.scan directly (same technique
test_strategy_engine_and_signals.py's TestScanOnce uses) so they exercise
sizing, standardized exits, order submission, position tracking, and the
kill switch — not the indicator math.

Migrated from DailyPlanService-based arming to HedgeHog Silo (see
src/execution/silo_runtime.py) by the Silo/Paper-Autonomy patch:
scan_for_entries()/manage_open_positions() no longer consult DailyPlan at
all post-patch — they gate on an active Silo mandate instead, and
scan_for_entries() now iterates mandate.authorized_symbols/
authorized_strategy_ids directly rather than settings.autonomous_watchlist.
See _arm_silo() below for the replacement helper, and
test_manage_open_positions_does_nothing_without_an_active_silo_mandate for
an explicit test of a real behavior change this migration brought with it:
manage_open_positions() previously ran unconditionally (no DailyPlan gate
at all); post-patch it now requires an active Silo mandate too, so an
already-open position's stop-loss/take-profit stops being enforced if the
Silo is disarmed or expires while that position is still open.

Every test uses its own unique symbol (test_db_engine_and_session shares
one on-disk SQLite file across the whole test session — see conftest.py —
so a fixed symbol like "QQQ" reused across tests would let one test's
leftover OPEN position leak into another's assertions).
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from src.accounts.profiles import BrokerName
from src.brokers.paper import PaperBrokerAdapter
from src.brokers.robinhood.adapter import RobinhoodHostBridgeAdapter
from src.brokers.router import BrokerRouter
from src.brokers.schwab.adapter import SchwabBrokerAdapter
from src.brokers.schwab_data_paper import SchwabDataPaperBroker
from src.config import Settings
from src.execution.autonomous_positions import AutonomousPositionService, AutonomousPositionStatus
from src.execution.autonomous_trader import manage_open_positions, scan_for_entries
from src.execution.executor import OrderRecord
from src.execution.kill_switch_state import KillSwitchService
from src.execution.silo_runtime import ProbabilityBands, SiloMandate, SiloRuntime
import src.execution.autonomous_trader as autonomous_trader
import src.execution.executor as executor_module
import src.risk.limits as risk_limits_module
from src.strategy import engine as strategy_engine
from src.strategy.catalog import SignalDetail
from sqlmodel import select

# Captured before the autouse _fake_broker fixture below ever monkeypatches
# autonomous_trader._build_broker — TestBuildBroker calls this directly to
# test the real function, unaffected by that fixture's patch.
_real_build_broker = autonomous_trader._build_broker


class _FakeAutoBroker:
    """
    A broker double with a per-symbol, settable quote. default_price
    (100.5) is deliberately inside every seeded stop=99/target=102 band
    used in this file, so a leftover OPEN position from an earlier test
    (test_db_engine_and_session shares one on-disk SQLite file across the
    whole test session) never gets spuriously closed by a later test's
    manage_open_positions() call unless that test explicitly set_price()s
    its own symbol to trigger it.
    """

    def __init__(self, default_price: float = 100.5):
        self.default_price = default_price
        self.prices: dict[str, float] = {}
        self.submitted_specs = []

    def set_price(self, symbol: str, price: float) -> None:
        self.prices[symbol] = price

    async def get_quote(self, symbol):
        price = self.prices.get(symbol, self.default_price)
        return {
            "symbol": symbol,
            "bid": price,
            "ask": price,
            "last": price,
            "quote_time": datetime.now(UTC).isoformat(),
            "mode": "TEST",
        }

    async def preview_order(self, profile, order_spec):
        return {"estimatedCommission": 0, "estimatedTotalInvestment": 100, "status": "OK"}

    async def submit_order(self, profile, order_spec):
        self.submitted_specs.append(order_spec)
        return {"orderId": f"fake-{len(self.submitted_specs)}", "status": "ACCEPTED"}

    async def get_order_status(self, profile, order_id): raise NotImplementedError
    async def list_accounts(self): raise NotImplementedError
    async def get_positions(self, profile): raise NotImplementedError

    async def get_balances(self, profile):
        # A large, constant equity: scan_for_entries()/manage_open_positions()
        # now both read this once per pass for the Silo's concentration
        # check, and DrawdownGuard.check_drawdown() reads it too (see
        # silo_evidence.py / the patched autonomous_trader.py). Constant
        # across every test in this file means every candidate's
        # concentration_pct and the account's drawdown_pct are both
        # predictable and near-zero, so these functional tests aren't
        # incidentally exercising the Silo's concentration/drawdown gates
        # (covered on their own in tests/test_silo_runtime.py).
        return {"net_liquidation_value": 100_000.0}

    async def get_price_history(self, symbol, bar_interval, lookback_days): raise NotImplementedError


_ALL_TEST_SYMBOLS = "QQQ,SPY,IWM,EEM,GLD,TLT,ZAUTA,ZAUTB,ZAUTC,ZAUTD,ZAUTE,ZAUTF,ZAUTG,ZAUTH,ZAUTI,ZAUTJ,ZAUTK,ZAUTL,ZAUTM,ZAUTN,ZAUTO,ZAUTP,ZAUTQ,ZAUTR,ZAUXY,ZAUTS,ZAUTT,ZAUTX"


def _settings(monkeypatch, strategy_ids="golden_cross", **overrides):
    """
    Builds a Settings instance for the test AND makes it the one Executor
    and RiskChecker actually see. Both call the module-level get_settings()
    themselves (src.execution.executor.get_settings / src.risk.limits.
    get_settings) rather than accepting an injected Settings — an
    autonomous_trader.py-constructed Executor ignores whatever Settings
    object this function returns unless get_settings is patched in both of
    those modules too (same technique tests/test_algo_execution.py uses).

    SYMBOL_ALLOWLIST is still set here for hygiene, but note it no longer
    actually gates autonomous PAPER entries — risk_mode="silo_paper"
    bypasses RiskChecker (and therefore the allowlist) entirely; the Silo
    mandate's own authorized_symbols is what gates them now (see
    _arm_silo below and test_a_symbol_outside_the_silo_mandate_is_never_scanned_even_while_others_trade).
    """
    defaults = dict(
        _env_file=None,
        env="test",
        autonomous_trading_enabled=True,
        AUTONOMOUS_STRATEGY_IDS=strategy_ids,
        autonomous_notional_per_trade_usd="1000",
        api_key_admin="change-me-in-prod",
        SYMBOL_ALLOWLIST=_ALL_TEST_SYMBOLS,
    )
    defaults.update(overrides)
    settings = Settings(**defaults)
    monkeypatch.setattr(executor_module, "get_settings", lambda: settings)
    monkeypatch.setattr(risk_limits_module, "get_settings", lambda: settings)
    return settings


def _arm_silo(monkeypatch, tmp_path, symbols, strategy_ids="golden_cross", notional_per_trade_usd="1000",
              max_concentration_pct="100", max_drawdown_pct="100", trades_per_minute=100):
    """
    scan_for_entries()/manage_open_positions() are gated by an active
    HedgeHog Silo mandate instead of DailyPlanService (see
    src/execution/silo_runtime.py) — every test that needs entries or
    position management to actually run arms one here.

    autonomous_trader.py constructs SiloRuntime() itself with no
    arguments (always DEFAULT_STATE_FILE), so this monkeypatches the
    class it imports to always hand back one runtime scoped to this
    test's own tmp_path — keeping every test's mandate state isolated
    from every other test's, and from any real .hedgehog_silo.json on
    disk. probability_weights is deliberately left empty so
    evaluate_candidate() takes its "no probabilistic weights configured"
    EXECUTE shortcut; the probabilistic gate itself is covered on its own
    in tests/test_silo_runtime.py, not re-tested here.
    """
    ids = strategy_ids.split(",") if isinstance(strategy_ids, str) else list(strategy_ids)
    syms = symbols.split(",") if isinstance(symbols, str) else list(symbols)
    runtime = SiloRuntime(str(tmp_path / "silo-state.json"))
    monkeypatch.setattr(autonomous_trader, "SiloRuntime", lambda *a, **k: runtime)
    mandate = SiloMandate(
        silo_id="test-silo",
        armed_by="test",
        timezone="UTC",
        runtime_end="23:59",
        trades_per_minute=trades_per_minute,
        max_per_trade_usd=Decimal(notional_per_trade_usd),
        max_concentration_pct=Decimal(max_concentration_pct),
        max_drawdown_pct=Decimal(max_drawdown_pct),
        authorized_symbols=syms,
        authorized_strategy_ids=ids,
        decision_bands=ProbabilityBands(execute_above=0.7, resize_above=0.5),
    )
    runtime.arm(mandate)
    return runtime


def _no_active_silo(monkeypatch, tmp_path):
    """
    Points autonomous_trader.SiloRuntime at a fresh, never-armed state
    file — guards against a leftover armed mandate from another test (or
    a real on-disk .hedgehog_silo.json) making a "nothing should happen
    without an active Silo" test pass for the wrong reason.
    """
    monkeypatch.setattr(autonomous_trader, "SiloRuntime", lambda *a, **k: SiloRuntime(str(tmp_path / "silo-state.json")))


@pytest.fixture(autouse=True)
def _fake_broker(monkeypatch):
    """Every test in this file gets a fresh _FakeAutoBroker in place of the real PaperBrokerAdapter."""
    broker = _FakeAutoBroker()
    monkeypatch.setattr(autonomous_trader, "_build_broker", lambda settings: broker)
    return broker


class TestScanForEntries:
    @pytest.mark.asyncio
    async def test_a_fired_signal_opens_a_position_with_standardized_exits(self, test_db_engine_and_session, monkeypatch, tmp_path):
        _, session = test_db_engine_and_session
        settings = _settings(monkeypatch, autonomous_risk_pct="0.01", autonomous_reward_risk_ratio="2")
        _arm_silo(monkeypatch, tmp_path, symbols=["ZAUTA"])
        detail = SignalDetail(entry_price=100.0, stop_loss_price=90.0, take_profit_price=130.0, rationale="forced golden cross")

        async def fake_scan(broker, symbol, strategy_id):
            return detail

        monkeypatch.setattr(strategy_engine, "scan", fake_scan)

        opened = await scan_for_entries(session, settings)

        assert opened == 1
        positions = [p for p in AutonomousPositionService(session).list_all() if p.symbol == "ZAUTA"]
        assert len(positions) == 1
        position = positions[0]
        assert position.strategy_id == "golden_cross"
        # The strategy's OWN stop/target (90/130) must be ignored — the
        # standardized 1% / 1:2 ratio computed from entry_price=100 is what
        # actually gets used (99 / 102), not the strategy's authentic numbers.
        assert position.stop_loss_price == pytest.approx(99.0)
        assert position.take_profit_price == pytest.approx(102.0)
        assert position.status == AutonomousPositionStatus.OPEN
        assert position.quantity == 10  # $1000 notional / $100 entry

    @pytest.mark.asyncio
    async def test_llm_evidence_makes_no_network_call_when_disabled_by_default(self, test_db_engine_and_session, monkeypatch, tmp_path):
        """
        settings.llm_evidence_enabled defaults to False (src/execution/
        llm_evidence.py) -- proves that through the real scan loop, not
        just by reading the default: no httpx.AsyncClient is even
        constructed when scoring a real candidate, so Groq/Gemini are
        fully inert unless explicitly turned on.
        """
        import httpx
        _, session = test_db_engine_and_session
        settings = _settings(monkeypatch)  # llm_evidence_enabled left at its default (False)
        _arm_silo(monkeypatch, tmp_path, symbols=["ZLLMD"])
        detail = SignalDetail(entry_price=100.0, stop_loss_price=90.0, take_profit_price=130.0, rationale="forced")

        async def fake_scan(broker, symbol, strategy_id):
            return detail

        calls = []
        monkeypatch.setattr(strategy_engine, "scan", fake_scan)
        monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: calls.append(1))

        opened = await scan_for_entries(session, settings)

        assert opened == 1
        assert calls == []

    @pytest.mark.asyncio
    async def test_order_actually_reaches_the_broker(self, test_db_engine_and_session, monkeypatch, tmp_path, _fake_broker):
        _, session = test_db_engine_and_session
        settings = _settings(monkeypatch)
        _arm_silo(monkeypatch, tmp_path, symbols=["ZAUTB"])
        detail = SignalDetail(entry_price=100.0, stop_loss_price=90.0, take_profit_price=130.0, rationale="forced")

        async def fake_scan(broker, symbol, strategy_id):
            return detail

        monkeypatch.setattr(strategy_engine, "scan", fake_scan)
        await scan_for_entries(session, settings)

        assert len(_fake_broker.submitted_specs) == 1

    @pytest.mark.asyncio
    async def test_order_record_uses_the_autonomous_agent_id_not_default(self, test_db_engine_and_session, monkeypatch, tmp_path):
        _, session = test_db_engine_and_session
        settings = _settings(monkeypatch, autonomous_agent_id="my-auto-agent")
        _arm_silo(monkeypatch, tmp_path, symbols=["ZAUTC"])
        detail = SignalDetail(entry_price=100.0, stop_loss_price=90.0, take_profit_price=130.0, rationale="forced")

        async def fake_scan(broker, symbol, strategy_id):
            return detail

        monkeypatch.setattr(strategy_engine, "scan", fake_scan)
        await scan_for_entries(session, settings)

        order = session.exec(select(OrderRecord).where(OrderRecord.symbol == "ZAUTC")).first()
        assert order is not None
        assert order.agent_id == "my-auto-agent"
        assert order.status == "SUBMITTED"

    @pytest.mark.asyncio
    async def test_no_signal_opens_nothing(self, test_db_engine_and_session, monkeypatch, tmp_path):
        _, session = test_db_engine_and_session
        settings = _settings(monkeypatch)
        _arm_silo(monkeypatch, tmp_path, symbols=["ZAUTD"])

        async def fake_scan(broker, symbol, strategy_id):
            return None

        monkeypatch.setattr(strategy_engine, "scan", fake_scan)
        opened = await scan_for_entries(session, settings)

        assert opened == 0

    @pytest.mark.asyncio
    async def test_does_not_pyramid_an_existing_open_position(self, test_db_engine_and_session, monkeypatch, tmp_path):
        _, session = test_db_engine_and_session
        settings = _settings(monkeypatch)
        _arm_silo(monkeypatch, tmp_path, symbols=["ZAUTE"])
        detail = SignalDetail(entry_price=100.0, stop_loss_price=90.0, take_profit_price=130.0, rationale="forced")

        async def fake_scan(broker, symbol, strategy_id):
            return detail

        monkeypatch.setattr(strategy_engine, "scan", fake_scan)

        first_pass = await scan_for_entries(session, settings)
        second_pass = await scan_for_entries(session, settings)

        assert first_pass == 1
        assert second_pass == 0  # ZAUTE/golden_cross already has an OPEN position
        positions = [p for p in AutonomousPositionService(session).list_all() if p.symbol == "ZAUTE"]
        assert len(positions) == 1

    @pytest.mark.asyncio
    async def test_too_small_a_notional_for_one_share_skips_the_trade(self, test_db_engine_and_session, monkeypatch, tmp_path):
        _, session = test_db_engine_and_session
        settings = _settings(monkeypatch)
        _arm_silo(monkeypatch, tmp_path, symbols=["ZAUTF"], notional_per_trade_usd="1000")
        # $5000 entry price, $1000 notional budget: not even one share fits.
        detail = SignalDetail(entry_price=5000.0, stop_loss_price=4500.0, take_profit_price=6000.0, rationale="forced")

        async def fake_scan(broker, symbol, strategy_id):
            return detail

        monkeypatch.setattr(strategy_engine, "scan", fake_scan)
        opened = await scan_for_entries(session, settings)

        assert opened == 0
        assert [p for p in AutonomousPositionService(session).list_all() if p.symbol == "ZAUTF"] == []

    @pytest.mark.asyncio
    async def test_fleet_wide_kill_switch_blocks_new_entries(self, test_db_engine_and_session, monkeypatch, tmp_path):
        _, session = test_db_engine_and_session
        settings = _settings(monkeypatch)
        _arm_silo(monkeypatch, tmp_path, symbols=["ZAUTG"])
        KillSwitchService(session).set_state(enabled=True, set_by="test", reason="halt everything")
        detail = SignalDetail(entry_price=100.0, stop_loss_price=90.0, take_profit_price=130.0, rationale="forced")

        async def fake_scan(broker, symbol, strategy_id):
            return detail

        try:
            monkeypatch.setattr(strategy_engine, "scan", fake_scan)
            opened = await scan_for_entries(session, settings)
            assert opened == 0
        finally:
            KillSwitchService(session).set_state(enabled=False, set_by="test", reason="cleanup")

    @pytest.mark.asyncio
    async def test_per_agent_kill_switch_blocks_only_the_autonomous_agent(self, test_db_engine_and_session, monkeypatch, tmp_path):
        _, session = test_db_engine_and_session
        settings = _settings(monkeypatch, autonomous_agent_id="auto-halted-test-agent")
        _arm_silo(monkeypatch, tmp_path, symbols=["ZAUTH"])
        KillSwitchService(session).set_state(enabled=True, set_by="test", reason="halt this agent only", scope="auto-halted-test-agent")
        detail = SignalDetail(entry_price=100.0, stop_loss_price=90.0, take_profit_price=130.0, rationale="forced")

        async def fake_scan(broker, symbol, strategy_id):
            return detail

        monkeypatch.setattr(strategy_engine, "scan", fake_scan)
        opened = await scan_for_entries(session, settings)

        assert opened == 0

    @pytest.mark.asyncio
    async def test_opening_a_position_fires_a_notification(self, test_db_engine_and_session, monkeypatch, tmp_path):
        _, session = test_db_engine_and_session
        settings = _settings(monkeypatch)
        _arm_silo(monkeypatch, tmp_path, symbols=["ZAUTO"])
        detail = SignalDetail(entry_price=100.0, stop_loss_price=90.0, take_profit_price=130.0, rationale="forced")
        calls = []

        async def fake_scan(broker, symbol, strategy_id):
            return detail

        async def fake_notify(s, text):
            calls.append(text)

        monkeypatch.setattr(strategy_engine, "scan", fake_scan)
        monkeypatch.setattr(autonomous_trader, "notify", fake_notify)
        await scan_for_entries(session, settings)

        assert len(calls) == 1
        assert "ZAUTO" in calls[0]
        assert "golden_cross" in calls[0]

    @pytest.mark.asyncio
    async def test_a_failing_strategy_does_not_stop_the_rest_of_the_scan(self, test_db_engine_and_session, monkeypatch, tmp_path):
        _, session = test_db_engine_and_session
        settings = _settings(monkeypatch, strategy_ids="golden_cross")
        _arm_silo(monkeypatch, tmp_path, symbols=["ZAUTI", "ZAUTJ"], strategy_ids="golden_cross")
        detail = SignalDetail(entry_price=100.0, stop_loss_price=90.0, take_profit_price=130.0, rationale="forced")

        async def flaky_scan(broker, symbol, strategy_id):
            if symbol == "ZAUTI":
                raise RuntimeError("simulated broker outage")
            return detail

        monkeypatch.setattr(strategy_engine, "scan", flaky_scan)
        opened = await scan_for_entries(session, settings)

        assert opened == 1  # ZAUTJ still went through despite ZAUTI failing

    @pytest.mark.asyncio
    async def test_a_symbol_outside_the_silo_mandate_is_never_scanned_even_while_others_trade(self, test_db_engine_and_session, monkeypatch, tmp_path, _fake_broker):
        """
        RiskChecker's symbol allowlist is bypassed entirely for autonomous
        PAPER execution (risk_mode="silo_paper" — see the patched
        Executor.preview_order()), so a symbol never being in the Silo
        mandate's own authorized_symbols is now the ONLY thing keeping it
        out of autonomous trading: scan_for_entries() iterates
        mandate.authorized_symbols directly, so a symbol simply not listed
        there is never even looked at. This replaces the old
        "test_risk_rejection_blocks_the_entry_without_opening_a_position"
        test, whose mechanism (a symbol allowlist rejection reaching
        RiskChecker) no longer exists on this path -- that original test's
        own fake symbol ("ZAUTNOTALLOWED", 14 chars) was actually already
        invalid input for TradeProposal (EQUITY symbols are 1-5 uppercase
        letters), so it was silently passing for the wrong reason (a
        pydantic validation error caught by scan_for_entries' own
        try/except) even before this migration; both symbols here are
        kept within that 5-char limit so this test exercises what it
        claims to.
        """
        _, session = test_db_engine_and_session
        outside_symbol = "ZEXCL"
        authorized_symbol = "ZSILO"
        settings = _settings(monkeypatch)
        _arm_silo(monkeypatch, tmp_path, symbols=[authorized_symbol], strategy_ids="golden_cross")
        detail = SignalDetail(entry_price=100.0, stop_loss_price=90.0, take_profit_price=130.0, rationale="forced")

        async def fake_scan(broker, symbol, strategy_id):
            return detail

        monkeypatch.setattr(strategy_engine, "scan", fake_scan)
        opened = await scan_for_entries(session, settings)

        assert opened == 1  # the authorized symbol still opened normally
        assert [p for p in AutonomousPositionService(session).list_all() if p.symbol == outside_symbol] == []
        assert all(spec.get("symbol") != outside_symbol for spec in _fake_broker.submitted_specs)

    @pytest.mark.asyncio
    async def test_without_an_armed_silo_nothing_opens_even_with_a_fired_signal(self, test_db_engine_and_session, monkeypatch, tmp_path):
        """The core gate: settings.autonomous_trading_enabled=True alone is no longer enough — see silo_runtime.py."""
        _, session = test_db_engine_and_session
        _no_active_silo(monkeypatch, tmp_path)
        settings = _settings(monkeypatch)  # deliberately no _arm_silo(...) call
        detail = SignalDetail(entry_price=100.0, stop_loss_price=90.0, take_profit_price=130.0, rationale="forced")

        async def fake_scan(broker, symbol, strategy_id):
            return detail

        monkeypatch.setattr(strategy_engine, "scan", fake_scan)
        opened = await scan_for_entries(session, settings)

        assert opened == 0

    @pytest.mark.asyncio
    async def test_an_armed_silo_has_no_expiry_before_runtime_end_and_stays_active_across_cycles(self, test_db_engine_and_session, monkeypatch, tmp_path):
        """
        The whole point of arm(): a one-time authorization, not a
        per-cycle chore — see silo_runtime.py's module docstring. No TTL
        before runtime_end, so a mandate armed once keeps authorizing new
        entries across as many scan cycles as it takes, until explicitly
        disarmed or its runtime_end passes. Two symbols so the second
        call's open isn't blocked by the no-pyramiding rule on the first.
        """
        _, session = test_db_engine_and_session
        settings = _settings(monkeypatch)
        _arm_silo(monkeypatch, tmp_path, symbols=["ZAUTR", "ZAUXY"])
        detail = SignalDetail(entry_price=100.0, stop_loss_price=90.0, take_profit_price=130.0, rationale="forced")

        async def fake_scan(broker, symbol, strategy_id):
            return detail

        monkeypatch.setattr(strategy_engine, "scan", fake_scan)
        first = await scan_for_entries(session, settings)
        second = await scan_for_entries(session, settings)

        assert first == 2  # both ZAUTR and ZAUXY opened on the first pass
        assert second == 0  # both already have open positions on the second pass — still armed, just nothing new to open

    @pytest.mark.asyncio
    async def test_disarming_stops_new_entries_immediately(self, test_db_engine_and_session, monkeypatch, tmp_path):
        _, session = test_db_engine_and_session
        settings = _settings(monkeypatch)
        runtime = _arm_silo(monkeypatch, tmp_path, symbols=["ZAUTS"])
        runtime.disarm()
        detail = SignalDetail(entry_price=100.0, stop_loss_price=90.0, take_profit_price=130.0, rationale="forced")

        async def fake_scan(broker, symbol, strategy_id):
            return detail

        monkeypatch.setattr(strategy_engine, "scan", fake_scan)
        opened = await scan_for_entries(session, settings)

        assert opened == 0

    @pytest.mark.asyncio
    async def test_armed_silos_notional_and_strategy_ids_take_priority_over_settings_defaults(self, test_db_engine_and_session, monkeypatch, tmp_path):
        """The whole point of arming: what's used is what was explicitly armed today, not the static settings fallback."""
        _, session = test_db_engine_and_session
        # settings itself says a different strategy ("turtle_donchian") and a much bigger notional — the mandate must win.
        settings = _settings(monkeypatch, strategy_ids="turtle_donchian", autonomous_notional_per_trade_usd="50000")
        _arm_silo(monkeypatch, tmp_path, symbols=["ZAUTT"], strategy_ids="golden_cross", notional_per_trade_usd="500")
        detail = SignalDetail(entry_price=100.0, stop_loss_price=90.0, take_profit_price=130.0, rationale="forced")

        async def fake_scan(broker, symbol, strategy_id):
            return detail if strategy_id == "golden_cross" else None

        monkeypatch.setattr(strategy_engine, "scan", fake_scan)
        opened = await scan_for_entries(session, settings)

        assert opened == 1
        position = next(p for p in AutonomousPositionService(session).list_all() if p.symbol == "ZAUTT")
        assert position.strategy_id == "golden_cross"
        assert position.quantity == 5  # $500 mandate notional / $100 entry, NOT $50000 / $100


class TestManageOpenPositions:
    @pytest.mark.asyncio
    async def test_price_at_take_profit_closes_the_position(self, test_db_engine_and_session, _fake_broker, monkeypatch, tmp_path):
        _, session = test_db_engine_and_session
        settings = _settings(monkeypatch)
        _arm_silo(monkeypatch, tmp_path, symbols=["ZAUTK"])
        service = AutonomousPositionService(session)
        position = service.open_position(
            symbol="ZAUTK", strategy_id="golden_cross", account="primary", agent_id="autonomous-trader",
            entry_decision_id="auto-entry-seed-target", quantity=10, entry_price=100.0,
            stop_loss_price=99.0, take_profit_price=102.0, entry_rationale="seeded for test",
        )
        _fake_broker.set_price("ZAUTK", 103.0)  # above take_profit_price

        closed = await manage_open_positions(session, settings)

        assert closed >= 1
        session.refresh(position)
        assert position.status == AutonomousPositionStatus.CLOSED_TARGET
        assert position.exit_price == pytest.approx(103.0)
        assert position.pnl_usd == pytest.approx((103.0 - 100.0) * 10)
        assert any(spec.get("symbol") == "ZAUTK" for spec in _fake_broker.submitted_specs)

    @pytest.mark.asyncio
    async def test_price_at_stop_loss_closes_the_position(self, test_db_engine_and_session, _fake_broker, monkeypatch, tmp_path):
        _, session = test_db_engine_and_session
        settings = _settings(monkeypatch)
        _arm_silo(monkeypatch, tmp_path, symbols=["ZAUTL"])
        service = AutonomousPositionService(session)
        service.open_position(
            symbol="ZAUTL", strategy_id="golden_cross", account="primary", agent_id="autonomous-trader",
            entry_decision_id="auto-entry-seed-stop", quantity=10, entry_price=100.0,
            stop_loss_price=99.0, take_profit_price=102.0, entry_rationale="seeded for test",
        )
        _fake_broker.set_price("ZAUTL", 98.5)  # below stop_loss_price

        closed = await manage_open_positions(session, settings)

        position = next(p for p in service.list_all() if p.symbol == "ZAUTL")
        assert position.status == AutonomousPositionStatus.CLOSED_STOP
        assert position.pnl_usd < 0
        assert closed >= 1

    @pytest.mark.asyncio
    async def test_price_between_stop_and_target_leaves_position_open(self, test_db_engine_and_session, _fake_broker, monkeypatch, tmp_path):
        _, session = test_db_engine_and_session
        settings = _settings(monkeypatch)
        _arm_silo(monkeypatch, tmp_path, symbols=["ZAUTM"])
        service = AutonomousPositionService(session)
        service.open_position(
            symbol="ZAUTM", strategy_id="golden_cross", account="primary", agent_id="autonomous-trader",
            entry_decision_id="auto-entry-seed-hold", quantity=10, entry_price=100.0,
            stop_loss_price=99.0, take_profit_price=102.0, entry_rationale="seeded for test",
        )
        _fake_broker.set_price("ZAUTM", 100.5)  # still inside the band

        await manage_open_positions(session, settings)

        position = next(p for p in service.list_open() if p.symbol == "ZAUTM")
        assert position.status == AutonomousPositionStatus.OPEN

    @pytest.mark.asyncio
    async def test_closed_position_is_not_managed_again(self, test_db_engine_and_session, _fake_broker, monkeypatch, tmp_path):
        _, session = test_db_engine_and_session
        settings = _settings(monkeypatch)
        _arm_silo(monkeypatch, tmp_path, symbols=["ZAUTN"])
        service = AutonomousPositionService(session)
        service.open_position(
            symbol="ZAUTN", strategy_id="golden_cross", account="primary", agent_id="autonomous-trader",
            entry_decision_id="auto-entry-seed-once", quantity=10, entry_price=100.0,
            stop_loss_price=99.0, take_profit_price=102.0, entry_rationale="seeded for test",
        )
        _fake_broker.set_price("ZAUTN", 103.0)

        await manage_open_positions(session, settings)
        zautn_orders_after_first_pass = sum(1 for spec in _fake_broker.submitted_specs if spec.get("symbol") == "ZAUTN")
        await manage_open_positions(session, settings)
        zautn_orders_after_second_pass = sum(1 for spec in _fake_broker.submitted_specs if spec.get("symbol") == "ZAUTN")

        # The second pass must not touch ZAUTN again — it's already CLOSED_TARGET.
        assert zautn_orders_after_first_pass == 1
        assert zautn_orders_after_second_pass == 1

    @pytest.mark.asyncio
    async def test_closing_a_position_fires_a_notification_with_pnl(self, test_db_engine_and_session, _fake_broker, monkeypatch, tmp_path):
        _, session = test_db_engine_and_session
        settings = _settings(monkeypatch)
        _arm_silo(monkeypatch, tmp_path, symbols=["ZAUTP"])
        service = AutonomousPositionService(session)
        service.open_position(
            symbol="ZAUTP", strategy_id="golden_cross", account="primary", agent_id="autonomous-trader",
            entry_decision_id="auto-entry-seed-notify", quantity=10, entry_price=100.0,
            stop_loss_price=99.0, take_profit_price=102.0, entry_rationale="seeded for test",
        )
        _fake_broker.set_price("ZAUTP", 103.0)  # above take_profit_price
        calls = []

        async def fake_notify(s, text):
            calls.append(text)

        monkeypatch.setattr(autonomous_trader, "notify", fake_notify)
        await manage_open_positions(session, settings)

        matching = [c for c in calls if "ZAUTP" in c]
        assert len(matching) == 1
        assert "+30.00" in matching[0]  # (103 - 100) * 10

    @pytest.mark.asyncio
    async def test_manage_open_positions_does_nothing_without_an_active_silo_mandate(self, test_db_engine_and_session, _fake_broker, monkeypatch, tmp_path):
        """
        A real behavior change this migration surfaced: manage_open_positions()
        previously ran unconditionally, with no DailyPlan gate at all (only
        scan_for_entries() checked it). Post-patch it requires an active
        Silo mandate too — so a position already past its take-profit
        stays OPEN and unmanaged if the Silo isn't armed (e.g. it expired
        at its own runtime_end, or was disarmed) while that position is
        still open. This pins that behavior explicitly rather than leaving
        it as an implicit side effect nobody has a test for.
        """
        _, session = test_db_engine_and_session
        settings = _settings(monkeypatch)
        _no_active_silo(monkeypatch, tmp_path)
        service = AutonomousPositionService(session)
        position = service.open_position(
            symbol="ZAUTNOMANDATE", strategy_id="golden_cross", account="primary", agent_id="autonomous-trader",
            entry_decision_id="auto-entry-seed-no-mandate", quantity=10, entry_price=100.0,
            stop_loss_price=99.0, take_profit_price=102.0, entry_rationale="seeded for test",
        )
        _fake_broker.set_price("ZAUTNOMANDATE", 103.0)  # above take_profit_price

        closed = await manage_open_positions(session, settings)

        assert closed == 0
        session.refresh(position)
        assert position.status == AutonomousPositionStatus.OPEN
        assert _fake_broker.submitted_specs == []


class TestBuildBroker:
    """
    _build_broker(settings) decides where the autonomous trader's market
    data comes from (real Schwab vs. synthetic) — never whether its orders
    are real, which stays fixed. See src/brokers/schwab_data_paper.py.
    """

    def test_paper_settings_return_a_plain_paper_broker(self):
        settings = Settings(_env_file=None, env="test", execution_mode="PAPER")

        broker = _real_build_broker(settings)

        assert isinstance(broker, PaperBrokerAdapter)
        assert not isinstance(broker, SchwabDataPaperBroker)

    def test_schwab_settings_wrap_in_schwab_data_paper_broker(self, monkeypatch):
        settings = Settings(_env_file=None, env="test", execution_mode="SCHWAB")
        fake_schwab = SchwabBrokerAdapter.__new__(SchwabBrokerAdapter)  # isinstance-only double; never calls its methods
        monkeypatch.setattr(autonomous_trader, "build_broker_adapter", lambda s: fake_schwab)

        broker = _real_build_broker(settings)

        assert isinstance(broker, SchwabDataPaperBroker)

    def test_schwab_not_actually_configured_falls_back_to_plain_paper(self, monkeypatch):
        """build_broker_adapter itself falls back to PaperBrokerAdapter when Schwab isn't fully configured — _build_broker must not wrap that in SchwabDataPaperBroker."""
        settings = Settings(_env_file=None, env="test", execution_mode="SCHWAB")
        monkeypatch.setattr(autonomous_trader, "build_broker_adapter", lambda s: PaperBrokerAdapter())

        broker = _real_build_broker(settings)

        assert isinstance(broker, PaperBrokerAdapter)
        assert not isinstance(broker, SchwabDataPaperBroker)

    def test_submit_order_is_never_reachable_through_the_real_schwab_adapter(self):
        """
        Even in the Schwab-wrapped case, preview_order/submit_order must
        resolve to PaperBrokerAdapter's simulated implementations, not
        SchwabBrokerAdapter's real ones — confirmed by class identity
        rather than a live call, since this double's methods aren't safe
        to invoke.
        """
        fake_schwab = SchwabBrokerAdapter.__new__(SchwabBrokerAdapter)
        broker = SchwabDataPaperBroker(fake_schwab)

        assert broker.submit_order.__func__ is PaperBrokerAdapter.submit_order
        assert broker.preview_order.__func__ is PaperBrokerAdapter.preview_order

    def test_a_broker_router_wrapping_schwab_still_gets_wrapped_in_schwab_data_paper_broker(self, monkeypatch):
        """
        build_broker_adapter() returns a BrokerRouter (not a bare
        SchwabBrokerAdapter) whenever more than one broker is configured —
        which is effectively always once Schwab is added alongside the
        default "primary" PAPER profile (see src/brokers/factory.py). This
        is the regression this test guards: _build_broker() must look
        inside the router for the real Schwab adapter and wrap THAT,
        not silently hand the router itself to the autonomous loop.
        """
        settings = Settings(_env_file=None, env="test", execution_mode="SCHWAB")
        fake_schwab = SchwabBrokerAdapter.__new__(SchwabBrokerAdapter)
        router = BrokerRouter({BrokerName.PAPER: PaperBrokerAdapter(), BrokerName.SCHWAB: fake_schwab}, BrokerName.SCHWAB)
        monkeypatch.setattr(autonomous_trader, "build_broker_adapter", lambda s: router)

        broker = _real_build_broker(settings)

        assert isinstance(broker, SchwabDataPaperBroker)
        assert broker.submit_order.__func__ is PaperBrokerAdapter.submit_order

    def test_a_live_capable_broker_this_function_cannot_wrap_falls_back_to_plain_paper_not_the_live_adapter(self, monkeypatch):
        """
        A RobinhoodHostBridgeAdapter (or any BrokerRouter with no Schwab
        adapter inside it) is real and live-capable, and there is no
        simulate-fills wrapper for it yet — _build_broker() must never
        return it, or a router containing it, directly. Fail closed to a
        fully-synthetic PaperBrokerAdapter instead, even though that means
        losing real market data for the autonomous loop in this
        configuration — the alternative is a live order path with no
        simulation at all, which is never acceptable here.
        """
        settings = Settings(_env_file=None, env="test", execution_mode="LIVE")
        fake_robinhood = RobinhoodHostBridgeAdapter.__new__(RobinhoodHostBridgeAdapter)

        monkeypatch.setattr(autonomous_trader, "build_broker_adapter", lambda s: fake_robinhood)
        broker = _real_build_broker(settings)
        assert isinstance(broker, PaperBrokerAdapter)
        assert not isinstance(broker, SchwabDataPaperBroker)

        router_without_schwab = BrokerRouter({BrokerName.PAPER: PaperBrokerAdapter(), BrokerName.ROBINHOOD: fake_robinhood}, BrokerName.ROBINHOOD)
        monkeypatch.setattr(autonomous_trader, "build_broker_adapter", lambda s: router_without_schwab)
        broker = _real_build_broker(settings)
        assert isinstance(broker, PaperBrokerAdapter)
        assert not isinstance(broker, SchwabDataPaperBroker)
