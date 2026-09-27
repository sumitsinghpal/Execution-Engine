"""
Tests for src/execution/multi_leg.py — 2-leg options combos (vertical
spreads, straddles, strangles) previewed and executed as one logical
trade through the same Executor/RiskChecker pipeline every other order
goes through. See that module's docstring for the execution-risk
rationale behind the mid-combo kill-switch trip tested below.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from src.config import Settings
from src.execution.executor import Executor
import src.execution.executor as executor_module
from src.execution.kill_switch_state import KillSwitchService
from src.execution.multi_leg import (
    LegRef,
    execute_multi_leg_order,
    preview_multi_leg_order,
    validate_combo_structure,
)
import src.risk.limits as risk_limits_module
from src.models.occ_symbol import format_occ_symbol
from src.models.orders import AssetType, Instruction, OrderType, TradeProposal


def _occ(underlying="QQQ", days_out=45, right="C", strike="400"):
    return format_occ_symbol(underlying, date.today() + timedelta(days=days_out), right, Decimal(strike))


def _leg(decision_id, symbol, instruction, quantity=2, agent_id="default", order_type=OrderType.MARKET, limit_price=None):
    return TradeProposal(
        decision_id=decision_id, agent_id=agent_id, account="primary", symbol=symbol,
        asset_type=AssetType.OPTION, instruction=instruction, quantity=quantity,
        order_type=order_type, limit_price=limit_price,
    )


def _settings(monkeypatch, **overrides):
    defaults = dict(_env_file=None, env="test", api_key_admin="change-me-in-prod")
    defaults.update(overrides)
    settings = Settings(**defaults)
    monkeypatch.setattr(executor_module, "get_settings", lambda: settings)
    monkeypatch.setattr(risk_limits_module, "get_settings", lambda: settings)
    return settings


class _FakeOptionsBroker:
    """Per-symbol settable preview price, standing in for a real options premium quote."""

    def __init__(self, default_price: float = 250.0):
        self.default_price = default_price
        self.prices: dict[str, float] = {}
        self.submitted_specs = []
        self.fail_symbols: set[str] = set()

    def set_price(self, symbol: str, price: float) -> None:
        self.prices[symbol] = price

    async def get_quote(self, symbol):
        price = self.prices.get(symbol, self.default_price)
        return {"symbol": symbol, "bid": price, "ask": price, "last": price, "quote_time": datetime.now(UTC).isoformat(), "mode": "TEST"}

    async def preview_order(self, profile, order_spec):
        price = self.prices.get(order_spec["symbol"], self.default_price)
        return {"estimatedCommission": 0, "estimatedTotalInvestment": price, "status": "OK"}

    async def submit_order(self, profile, order_spec):
        if order_spec["symbol"] in self.fail_symbols:
            raise RuntimeError(f"simulated broker rejection for {order_spec['symbol']}")
        self.submitted_specs.append(order_spec)
        return {"orderId": f"fake-{len(self.submitted_specs)}", "status": "ACCEPTED"}

    async def get_order_status(self, profile, order_id): raise NotImplementedError
    async def list_accounts(self): raise NotImplementedError
    async def get_positions(self, profile): raise NotImplementedError
    async def get_balances(self, profile): raise NotImplementedError
    async def get_price_history(self, symbol, bar_interval, lookback_days): raise NotImplementedError


class TestValidateComboStructure:
    def test_valid_vertical_spread_passes(self):
        legs = [
            _leg("v1", _occ(strike="400"), Instruction.BUY),
            _leg("v2", _occ(strike="410"), Instruction.SELL),
        ]
        validate_combo_structure("vertical_spread", legs)  # must not raise

    def test_vertical_spread_rejects_mixed_call_and_put(self):
        legs = [
            _leg("v1", _occ(strike="400", right="C"), Instruction.BUY),
            _leg("v2", _occ(strike="410", right="P"), Instruction.SELL),
        ]
        with pytest.raises(ValueError, match="same right"):
            validate_combo_structure("vertical_spread", legs)

    def test_vertical_spread_rejects_same_strike(self):
        # Same underlying/right/expiration/strike is the identical contract
        # on both legs — caught by the earlier "not a combo" check, since
        # for a vertical spread there's no way to have matching everything
        # else AND a distinct strike without it being a different symbol.
        legs = [
            _leg("v1", _occ(strike="400"), Instruction.BUY),
            _leg("v2", _occ(strike="400"), Instruction.SELL),
        ]
        with pytest.raises(ValueError, match="not a combo"):
            validate_combo_structure("vertical_spread", legs)

    def test_vertical_spread_rejects_same_instruction_on_both_legs(self):
        legs = [
            _leg("v1", _occ(strike="400"), Instruction.BUY),
            _leg("v2", _occ(strike="410"), Instruction.BUY),
        ]
        with pytest.raises(ValueError, match="one leg BUY and the other SELL"):
            validate_combo_structure("vertical_spread", legs)

    def test_valid_long_straddle_passes(self):
        legs = [
            _leg("s1", _occ(strike="400", right="C"), Instruction.BUY),
            _leg("s2", _occ(strike="400", right="P"), Instruction.BUY),
        ]
        validate_combo_structure("straddle", legs)

    def test_straddle_rejects_mismatched_strikes(self):
        legs = [
            _leg("s1", _occ(strike="400", right="C"), Instruction.BUY),
            _leg("s2", _occ(strike="410", right="P"), Instruction.BUY),
        ]
        with pytest.raises(ValueError, match="same strike"):
            validate_combo_structure("straddle", legs)

    def test_straddle_rejects_mixed_instructions(self):
        legs = [
            _leg("s1", _occ(strike="400", right="C"), Instruction.BUY),
            _leg("s2", _occ(strike="400", right="P"), Instruction.SELL),
        ]
        with pytest.raises(ValueError, match="same instruction"):
            validate_combo_structure("straddle", legs)

    def test_valid_strangle_passes(self):
        legs = [
            _leg("g1", _occ(strike="410", right="C"), Instruction.BUY),
            _leg("g2", _occ(strike="390", right="P"), Instruction.BUY),
        ]
        validate_combo_structure("strangle", legs)

    def test_strangle_rejects_equal_strikes_as_actually_a_straddle(self):
        legs = [
            _leg("g1", _occ(strike="400", right="C"), Instruction.BUY),
            _leg("g2", _occ(strike="400", right="P"), Instruction.BUY),
        ]
        with pytest.raises(ValueError, match="not a strangle"):
            validate_combo_structure("strangle", legs)

    def test_custom_skips_shape_checks_but_still_requires_same_underlying(self):
        legs = [
            _leg("c1", _occ(underlying="QQQ", strike="400", right="C"), Instruction.BUY),
            _leg("c2", _occ(underlying="SPY", strike="400", right="C"), Instruction.SELL),
        ]
        with pytest.raises(ValueError, match="same underlying"):
            validate_combo_structure("custom", legs)

    def test_rejects_mismatched_quantities(self):
        legs = [
            _leg("q1", _occ(strike="400"), Instruction.BUY, quantity=1),
            _leg("q2", _occ(strike="410"), Instruction.SELL, quantity=2),
        ]
        with pytest.raises(ValueError, match="same quantity"):
            validate_combo_structure("vertical_spread", legs)

    def test_rejects_non_option_leg(self):
        legs = [
            _leg("n1", _occ(strike="400"), Instruction.BUY),
            TradeProposal(decision_id="n2", account="primary", symbol="QQQ", asset_type=AssetType.EQUITY, instruction=Instruction.SELL, quantity=2, order_type=OrderType.MARKET),
        ]
        with pytest.raises(ValueError, match="asset_type=OPTION"):
            validate_combo_structure("custom", legs)

    def test_rejects_wrong_leg_count(self):
        legs = [_leg("o1", _occ(strike="400"), Instruction.BUY)]
        with pytest.raises(ValueError, match="exactly 2 legs"):
            validate_combo_structure("custom", legs)


class TestPreviewMultiLegOrder:
    @pytest.mark.asyncio
    async def test_vertical_debit_spread_computes_net_debit_and_max_loss_profit(self, test_db_engine_and_session, monkeypatch):
        _, session = test_db_engine_and_session
        _settings(monkeypatch)
        broker = _FakeOptionsBroker()
        long_leg = _occ(underlying="QQQ", strike="400", days_out=45)
        short_leg = _occ(underlying="QQQ", strike="410", days_out=45)
        broker.set_price(long_leg, 500.0)   # buy: $500 debit
        broker.set_price(short_leg, 200.0)  # sell: $200 credit
        legs = [_leg("ml-vd-1", long_leg, Instruction.BUY, quantity=1), _leg("ml-vd-2", short_leg, Instruction.SELL, quantity=1)]

        executor = Executor(session=session, broker=broker)
        preview = await preview_multi_leg_order(executor, legs, combo_type="vertical_spread")

        assert preview.risk_verdict == "APPROVED"
        assert preview.net_debit_or_credit_usd == pytest.approx(300.0)  # 500 paid - 200 received
        # strike width = $10 * 100 * 1 contract = $1000; net debit $300 -> max_loss=300, max_profit=700
        assert preview.max_loss_usd == pytest.approx(300.0)
        assert preview.max_profit_usd == pytest.approx(700.0)
        assert len(preview.legs) == 2

    @pytest.mark.asyncio
    async def test_vertical_credit_spread_computes_net_credit_and_max_loss_profit(self, test_db_engine_and_session, monkeypatch):
        _, session = test_db_engine_and_session
        _settings(monkeypatch)
        broker = _FakeOptionsBroker()
        short_leg = _occ(underlying="QQQ", strike="400", days_out=45)
        long_leg = _occ(underlying="QQQ", strike="410", days_out=45)
        broker.set_price(short_leg, 300.0)  # sell: $300 credit
        broker.set_price(long_leg, 100.0)   # buy: $100 debit
        legs = [_leg("ml-vc-1", short_leg, Instruction.SELL, quantity=1), _leg("ml-vc-2", long_leg, Instruction.BUY, quantity=1)]

        executor = Executor(session=session, broker=broker)
        preview = await preview_multi_leg_order(executor, legs, combo_type="vertical_spread")

        assert preview.net_debit_or_credit_usd == pytest.approx(-200.0)  # net credit received
        # width $1000, credit $200 -> max_profit=200, max_loss=800
        assert preview.max_profit_usd == pytest.approx(200.0)
        assert preview.max_loss_usd == pytest.approx(800.0)

    @pytest.mark.asyncio
    async def test_custom_combo_has_no_max_loss_profit_figure(self, test_db_engine_and_session, monkeypatch):
        _, session = test_db_engine_and_session
        _settings(monkeypatch)
        broker = _FakeOptionsBroker()
        legs = [
            _leg("ml-custom-1", _occ(strike="400", right="C"), Instruction.BUY, quantity=1),
            _leg("ml-custom-2", _occ(strike="400", right="P"), Instruction.BUY, quantity=1),
        ]
        executor = Executor(session=session, broker=broker)
        preview = await preview_multi_leg_order(executor, legs, combo_type="straddle")

        assert preview.max_loss_usd is None
        assert preview.max_profit_usd is None

    @pytest.mark.asyncio
    async def test_a_rejected_leg_makes_the_whole_combo_rejected(self, test_db_engine_and_session, monkeypatch):
        _, session = test_db_engine_and_session
        _settings(monkeypatch, max_order_notional_usd=Decimal("50"))  # tiny cap — every leg will breach it
        broker = _FakeOptionsBroker(default_price=5000.0)
        legs = [
            _leg("ml-rej-1", _occ(strike="400"), Instruction.BUY, quantity=1),
            _leg("ml-rej-2", _occ(strike="410"), Instruction.SELL, quantity=1),
        ]
        executor = Executor(session=session, broker=broker)
        preview = await preview_multi_leg_order(executor, legs, combo_type="vertical_spread")

        assert preview.risk_verdict == "REJECTED"
        assert any(lp.risk_verdict == "REJECTED" for lp in preview.legs)


class TestExecuteMultiLegOrder:
    @pytest.mark.asyncio
    async def test_both_legs_execute_successfully(self, test_db_engine_and_session, monkeypatch):
        _, session = test_db_engine_and_session
        _settings(monkeypatch)
        broker = _FakeOptionsBroker()
        legs = [
            _leg("ml-exec-1", _occ(strike="400"), Instruction.BUY, quantity=1),
            _leg("ml-exec-2", _occ(strike="410"), Instruction.SELL, quantity=1),
        ]
        executor = Executor(session=session, broker=broker)
        preview = await preview_multi_leg_order(executor, legs, combo_type="vertical_spread")
        assert preview.risk_verdict == "APPROVED"

        leg_refs = [lp.to_leg_ref() for lp in preview.legs]
        result = await execute_multi_leg_order(executor, preview.combo_id, leg_refs, approved_by="test-op", attestation="test combo")

        assert result.fully_executed is True
        assert len(result.executed_legs) == 2
        assert len(broker.submitted_specs) == 2

    @pytest.mark.asyncio
    async def test_leg_2_failure_after_leg_1_succeeds_trips_the_agent_kill_switch(self, test_db_engine_and_session, monkeypatch):
        _, session = test_db_engine_and_session
        _settings(monkeypatch)
        broker = _FakeOptionsBroker()
        long_leg = _occ(underlying="QQQ", strike="400", days_out=50)
        short_leg = _occ(underlying="QQQ", strike="410", days_out=50)
        broker.fail_symbols.add(short_leg)  # leg 2 (the SELL) fails at submit time
        legs = [
            _leg("ml-fail-1", long_leg, Instruction.BUY, quantity=1, agent_id="multi-leg-test-agent"),
            _leg("ml-fail-2", short_leg, Instruction.SELL, quantity=1, agent_id="multi-leg-test-agent"),
        ]
        executor = Executor(session=session, broker=broker)
        preview = await preview_multi_leg_order(executor, legs, combo_type="vertical_spread")
        assert preview.risk_verdict == "APPROVED"

        try:
            leg_refs = [lp.to_leg_ref() for lp in preview.legs]
            result = await execute_multi_leg_order(executor, preview.combo_id, leg_refs, approved_by="test-op", attestation="test combo")

            assert result.fully_executed is False
            assert result.failed_leg_index == 1
            assert len(result.executed_legs) == 1  # leg 1 went through before leg 2 failed
            assert KillSwitchService(session).is_enabled("multi-leg-test-agent") is True
        finally:
            KillSwitchService(session).set_state(enabled=False, set_by="test_cleanup", scope="multi-leg-test-agent")

    @pytest.mark.asyncio
    async def test_leg_1_failure_with_nothing_executed_yet_does_not_trip_the_kill_switch(self, test_db_engine_and_session, monkeypatch):
        _, session = test_db_engine_and_session
        _settings(monkeypatch)
        broker = _FakeOptionsBroker()
        long_leg = _occ(underlying="QQQ", strike="400", days_out=55)
        short_leg = _occ(underlying="QQQ", strike="410", days_out=55)
        broker.fail_symbols.add(long_leg)  # the FIRST leg fails — nothing has executed yet
        legs = [
            _leg("ml-fail1st-1", long_leg, Instruction.BUY, quantity=1, agent_id="multi-leg-test-agent-2"),
            _leg("ml-fail1st-2", short_leg, Instruction.SELL, quantity=1, agent_id="multi-leg-test-agent-2"),
        ]
        executor = Executor(session=session, broker=broker)
        preview = await preview_multi_leg_order(executor, legs, combo_type="vertical_spread")
        assert preview.risk_verdict == "APPROVED"

        leg_refs = [lp.to_leg_ref() for lp in preview.legs]
        result = await execute_multi_leg_order(executor, preview.combo_id, leg_refs, approved_by="test-op", attestation="test combo")

        assert result.fully_executed is False
        assert result.failed_leg_index == 0
        assert result.executed_legs == []
        assert KillSwitchService(session).is_enabled("multi-leg-test-agent-2") is False  # nothing was open, so no need to halt


class TestMultiLegSubmissionClaims:
    """
    src/execution/submission_guard.py's durable per-decision submission
    claims (see src/execution/executor.py's execute_order) key on each
    leg's OWN decision_id — execute_multi_leg_order() gives every leg a
    distinct one (see its idempotency_key=f"{leg.decision_id}:{combo_id}:
    leg{i}" call), so a combo's two legs never share a claim row and can
    never collide with each other. These tests make that explicit rather
    than leaving it implied by the plain success-path test above, and
    cover the restart/retry semantics the reconciliation notes flagged as
    "not certified by the new single-order claim tests": a caller retrying
    the same leg with its original idempotency key must be a no-op (not a
    second broker submission), a caller retrying with a DIFFERENT key must
    be refused outright, and a process restart between legs (nothing but
    DB state survives) must still let the remaining leg execute normally.
    """

    @pytest.mark.asyncio
    async def test_both_legs_get_independent_submission_claims(self, test_db_engine_and_session, monkeypatch):
        from src.execution.submission_guard import SubmissionClaim

        _, session = test_db_engine_and_session
        _settings(monkeypatch)
        broker = _FakeOptionsBroker()
        legs = [
            _leg("ml-claim-1", _occ(strike="400", days_out=60), Instruction.BUY, quantity=1),
            _leg("ml-claim-2", _occ(strike="410", days_out=60), Instruction.SELL, quantity=1),
        ]
        executor = Executor(session=session, broker=broker)
        preview = await preview_multi_leg_order(executor, legs, combo_type="vertical_spread")
        leg_refs = [lp.to_leg_ref() for lp in preview.legs]
        result = await execute_multi_leg_order(executor, preview.combo_id, leg_refs, approved_by="test-op", attestation="test combo")
        assert result.fully_executed is True

        claim_1 = session.get(SubmissionClaim, "ml-claim-1")
        claim_2 = session.get(SubmissionClaim, "ml-claim-2")
        assert claim_1 is not None and claim_2 is not None
        assert claim_1.idempotency_key != claim_2.idempotency_key  # distinct rows, not one shared claim
        assert claim_1.preview_id == leg_refs[0].preview_id
        assert claim_2.preview_id == leg_refs[1].preview_id

    @pytest.mark.asyncio
    async def test_retrying_a_leg_with_its_original_idempotency_key_does_not_resubmit(self, test_db_engine_and_session, monkeypatch):
        _, session = test_db_engine_and_session
        _settings(monkeypatch)
        broker = _FakeOptionsBroker()
        legs = [
            _leg("ml-retry-same-1", _occ(strike="400", days_out=65), Instruction.BUY, quantity=1),
            _leg("ml-retry-same-2", _occ(strike="410", days_out=65), Instruction.SELL, quantity=1),
        ]
        executor = Executor(session=session, broker=broker)
        preview = await preview_multi_leg_order(executor, legs, combo_type="vertical_spread")
        leg_refs = [lp.to_leg_ref() for lp in preview.legs]
        first = await execute_multi_leg_order(executor, preview.combo_id, leg_refs, approved_by="test-op", attestation="test combo")
        assert first.fully_executed is True
        assert len(broker.submitted_specs) == 2

        # Simulates a caller (or a client library) retrying leg 1 after, say,
        # a network timeout on the original response — same decision_id, same
        # preview_id, same idempotency_key it used the first time.
        leg0 = leg_refs[0]
        retry_key = f"{leg0.decision_id}:{preview.combo_id}:leg0"
        retried = await executor.execute_order(
            decision_id=leg0.decision_id, preview_id=leg0.preview_id, approved_by="test-op",
            approved_at=datetime.now(UTC), attestation="retry same leg", idempotency_key=retry_key,
        )
        assert retried.execution_id == first.executed_legs[0]["execution_id"]
        assert len(broker.submitted_specs) == 2  # unchanged — no second broker call for leg 1

    @pytest.mark.asyncio
    async def test_retrying_a_leg_with_a_different_idempotency_key_is_refused_not_double_submitted(self, test_db_engine_and_session, monkeypatch):
        _, session = test_db_engine_and_session
        _settings(monkeypatch)
        broker = _FakeOptionsBroker()
        legs = [
            _leg("ml-retry-diff-1", _occ(strike="400", days_out=70), Instruction.BUY, quantity=1),
            _leg("ml-retry-diff-2", _occ(strike="410", days_out=70), Instruction.SELL, quantity=1),
        ]
        executor = Executor(session=session, broker=broker)
        preview = await preview_multi_leg_order(executor, legs, combo_type="vertical_spread")
        leg_refs = [lp.to_leg_ref() for lp in preview.legs]
        await execute_multi_leg_order(executor, preview.combo_id, leg_refs, approved_by="test-op", attestation="test combo")
        assert len(broker.submitted_specs) == 2

        # A caller that (incorrectly) generates a FRESH idempotency key on
        # retry, instead of reusing the one from its own original request —
        # this must be refused outright, not treated as a new submission.
        leg0 = leg_refs[0]
        with pytest.raises(ValueError, match="idempotency key conflict"):
            await executor.execute_order(
                decision_id=leg0.decision_id, preview_id=leg0.preview_id, approved_by="test-op",
                approved_at=datetime.now(UTC), attestation="retry with a fresh key", idempotency_key="a-brand-new-key",
            )
        assert len(broker.submitted_specs) == 2  # still unchanged — refused before ever reaching the broker

    @pytest.mark.asyncio
    async def test_a_restart_between_legs_still_lets_the_remaining_leg_execute_normally(self, test_db_engine_and_session, monkeypatch):
        """
        Nothing but DB state survives a real process restart — this
        simulates that by executing leg 1 through one Executor instance,
        discarding it, then finishing the combo on a brand-new Executor
        built from the same session/DB, the same way a fresh request after
        a restart would. Each leg's own persisted preview/claim is what
        makes this safe, not any in-memory state carried between legs.
        """
        _, session = test_db_engine_and_session
        _settings(monkeypatch)
        broker = _FakeOptionsBroker()
        legs = [
            _leg("ml-restart-1", _occ(strike="400", days_out=75), Instruction.BUY, quantity=1),
            _leg("ml-restart-2", _occ(strike="410", days_out=75), Instruction.SELL, quantity=1),
        ]
        first_executor = Executor(session=session, broker=broker)
        preview = await preview_multi_leg_order(first_executor, legs, combo_type="vertical_spread")
        leg_refs = [lp.to_leg_ref() for lp in preview.legs]

        leg0 = leg_refs[0]
        await first_executor.execute_order(
            decision_id=leg0.decision_id, preview_id=leg0.preview_id, approved_by="test-op",
            approved_at=datetime.now(UTC), attestation="leg 1 before the (simulated) restart",
            idempotency_key=f"{leg0.decision_id}:{preview.combo_id}:leg0",
        )
        assert len(broker.submitted_specs) == 1
        del first_executor  # the process "restarts" here — nothing in memory survives

        second_executor = Executor(session=session, broker=broker)
        leg1 = leg_refs[1]
        receipt = await second_executor.execute_order(
            decision_id=leg1.decision_id, preview_id=leg1.preview_id, approved_by="test-op",
            approved_at=datetime.now(UTC), attestation="leg 2 resumed after the restart, on a fresh Executor",
            idempotency_key=f"{leg1.decision_id}:{preview.combo_id}:leg1",
        )
        assert receipt.execution_id is not None
        assert len(broker.submitted_specs) == 2
