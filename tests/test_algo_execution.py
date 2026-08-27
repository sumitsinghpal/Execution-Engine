"""
Tests for TWAP/VWAP execution: the plan builders, the background slicing
loop, the kill-switch-mid-flight safety property, and TradeProposal's
validation for these order types.
"""

from datetime import datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import sessionmaker
from sqlmodel import Session, select

from src.accounts.profiles import AccountProfile, BrokerName
from src.config import Settings
from src.execution import algo_slices as algo
from src.execution import executor as executor_module
from src.execution.algo_slices import AlgoSliceRecord, build_twap_plan, build_vwap_plan, execute_algo_slices
from src.execution.executor import Executor, OrderRecord
from src.execution.kill_switch_state import KillSwitchService
from src.models.orders import AssetType, Instruction, OrderType, TradeProposal

# A dedicated account alias, distinct from primary/retirement/paper.
# test_position_reconciliation.py's tests each own one of those three and
# assert *exact* aggregate positions for it (summed across every FILLED
# order for that account, regardless of which test created it) — an algo
# test that reaches FILLED under any of those three would silently pollute
# that count. get_account_profile() needs a real configured alias, so this
# test file registers its own via a patched get_settings() rather than
# reusing one of the three "claimed" ones.
_ALGO_TEST_ACCOUNT = "algo-test-account"


def _session_factory(engine):
    """
    A real session factory bound to the test engine (fresh Session per
    call, matching production's SessionLocal) — execute_algo_slices()
    opens and closes its own session per slice, so tests must give it
    something that behaves the same way, not a single shared Session
    object that gets closed after the first slice.
    """
    return sessionmaker(bind=engine, class_=Session)


def _settings_with_algo_test_account() -> Settings:
    return Settings(
        _env_file=None,
        env="test",
        account_profiles={
            "primary": AccountProfile(broker=BrokerName.PAPER),
            "retirement": AccountProfile(broker=BrokerName.PAPER),
            "paper": AccountProfile(broker=BrokerName.PAPER),
            _ALGO_TEST_ACCOUNT: AccountProfile(broker=BrokerName.PAPER),
        },
    )


class TestTwapPlan:
    def test_splits_evenly_when_divisible(self):
        assert build_twap_plan(100, 5) == [20, 20, 20, 20, 20]

    def test_remainder_goes_to_earliest_slices(self):
        assert build_twap_plan(100, 3) == [34, 33, 33]

    def test_sums_to_total_quantity(self):
        for qty, slices in [(7, 3), (1000, 6), (1, 4), (0, 5)]:
            assert sum(build_twap_plan(qty, slices)) == qty


class TestVwapPlan:
    def test_falls_back_to_twap_with_no_volume_curve(self):
        assert build_vwap_plan(100, 5, []) == build_twap_plan(100, 5)

    def test_falls_back_to_twap_with_zero_volume_curve(self):
        assert build_vwap_plan(100, 4, [0, 0, 0, 0]) == build_twap_plan(100, 4)

    def test_weights_slices_by_relative_volume(self):
        # Two buckets, second one 3x the volume of the first.
        plan = build_vwap_plan(400, 2, [10, 10, 10, 30, 30, 30])
        assert plan[1] > plan[0]

    def test_sums_to_total_quantity_despite_rounding(self):
        plan = build_vwap_plan(1000, 7, [1, 5, 12, 3, 9, 40, 2, 8, 17])
        assert sum(plan) == 1000
        assert len(plan) == 7


class TestTradeProposalAlgoValidation:
    def _base(self, **overrides):
        fields = dict(
            decision_id="algo-model-test",
            account="primary",
            symbol="QQQ",
            asset_type=AssetType.ETF,
            instruction=Instruction.BUY,
            quantity=100,
            order_type=OrderType.TWAP,
        )
        fields.update(overrides)
        return TradeProposal(**fields)

    def test_twap_defaults_duration_and_slices_when_omitted(self):
        proposal = self._base()
        assert proposal.algo_duration_minutes == 30
        assert proposal.algo_slices == 6

    def test_explicit_duration_and_slices_are_respected(self):
        proposal = self._base(algo_duration_minutes=10, algo_slices=4)
        assert proposal.algo_duration_minutes == 10
        assert proposal.algo_slices == 4

    def test_twap_rejects_limit_price(self):
        with pytest.raises(ValidationError, match="don't apply"):
            self._base(limit_price=Decimal("100"))

    def test_vwap_rejects_stop_price(self):
        with pytest.raises(ValidationError, match="don't apply"):
            self._base(order_type=OrderType.VWAP, stop_price=Decimal("100"))

    def test_market_order_rejects_algo_fields(self):
        with pytest.raises(ValidationError, match="only apply to TWAP/VWAP"):
            self._base(order_type=OrderType.MARKET, algo_slices=4)


@pytest.mark.asyncio
class TestExecuteAlgoSlices:
    async def test_all_slices_submitted_when_kill_switch_stays_off(self, test_db_engine_and_session, monkeypatch):
        engine, session = test_db_engine_and_session
        factory = _session_factory(engine)
        monkeypatch.setattr(algo, "get_settings", _settings_with_algo_test_account)
        decision_id = "algo-happy-path-001"
        session.add(OrderRecord(
            decision_id=decision_id, account=_ALGO_TEST_ACCOUNT, symbol="QQQ", quantity=30,
            instruction="BUY", asset_type="ETF", order_type="TWAP",
            payload_checksum="test", status="SUBMITTED",
        ))
        session.commit()

        await execute_algo_slices(
            decision_id=decision_id, account=_ALGO_TEST_ACCOUNT, agent_id="default", symbol="QQQ",
            asset_type="ETF", instruction="BUY", quantities=[10, 10, 10], interval_seconds=0,
            session_factory=factory,
        )

        check = factory()
        try:
            slices = check.exec(select(AlgoSliceRecord).where(AlgoSliceRecord.parent_decision_id == decision_id)).all()
            assert len(slices) == 3
            assert all(s.status == "SUBMITTED" for s in slices)

            order = check.exec(select(OrderRecord).where(OrderRecord.decision_id == decision_id)).first()
            assert order.status == "FILLED"
            assert order.filled_quantity == 30
        finally:
            check.close()

    async def test_kill_switch_mid_flight_stops_remaining_slices(self, test_db_engine_and_session):
        """The core safety property: halting an agent must actually stop an in-progress algo order."""
        engine, session = test_db_engine_and_session
        factory = _session_factory(engine)
        decision_id = "algo-kill-switch-001"
        agent_id = "algo-halt-test-agent"
        session.add(OrderRecord(
            decision_id=decision_id, account="primary", symbol="IWM", quantity=30,
            instruction="BUY", asset_type="ETF", order_type="TWAP", agent_id=agent_id,
            payload_checksum="test", status="SUBMITTED",
        ))
        session.commit()

        try:
            KillSwitchService(session).set_state(enabled=True, set_by="test", scope=agent_id)

            await execute_algo_slices(
                decision_id=decision_id, account="primary", agent_id=agent_id, symbol="IWM",
                asset_type="ETF", instruction="BUY", quantities=[10, 10, 10], interval_seconds=0,
                session_factory=factory,
            )

            check = factory()
            try:
                slices = check.exec(select(AlgoSliceRecord).where(AlgoSliceRecord.parent_decision_id == decision_id)).all()
                assert len(slices) == 3
                assert all(s.status == "SKIPPED_KILL_SWITCH" for s in slices)

                order = check.exec(select(OrderRecord).where(OrderRecord.decision_id == decision_id)).first()
                assert order.status == "CANCELED"
                assert order.filled_quantity == 0
            finally:
                check.close()
        finally:
            KillSwitchService(session).set_state(enabled=False, set_by="test_cleanup", scope=agent_id)

    async def test_halt_partway_through_leaves_a_partial_fill(self, test_db_engine_and_session, monkeypatch):
        """If the switch flips only after some slices already went out, the order is PARTIAL_FILL, not CANCELED."""
        engine, session = test_db_engine_and_session
        factory = _session_factory(engine)
        monkeypatch.setattr(algo, "get_settings", _settings_with_algo_test_account)
        decision_id = "algo-partial-001"
        agent_id = "algo-partial-test-agent"
        session.add(OrderRecord(
            decision_id=decision_id, account=_ALGO_TEST_ACCOUNT, symbol="SPY", quantity=30,
            instruction="BUY", asset_type="ETF", order_type="TWAP", agent_id=agent_id,
            payload_checksum="test", status="SUBMITTED",
        ))
        session.commit()

        async def flip_kill_switch_instead_of_sleeping(*args, **kwargs):
            KillSwitchService(session).set_state(enabled=True, set_by="test", scope=agent_id)

        monkeypatch.setattr(algo.asyncio, "sleep", flip_kill_switch_instead_of_sleeping)

        try:
            await execute_algo_slices(
                decision_id=decision_id, account=_ALGO_TEST_ACCOUNT, agent_id=agent_id, symbol="SPY",
                asset_type="ETF", instruction="BUY", quantities=[10, 10, 10], interval_seconds=0,
                session_factory=factory,
            )

            check = factory()
            try:
                order = check.exec(select(OrderRecord).where(OrderRecord.decision_id == decision_id)).first()
                assert order.status == "PARTIAL_FILL"
                assert order.filled_quantity == 10
            finally:
                check.close()
        finally:
            KillSwitchService(session).set_state(enabled=False, set_by="test_cleanup", scope=agent_id)


@pytest.mark.asyncio
class TestExecutorTwapIntegration:
    async def test_preview_then_execute_schedules_algo_slices(self, test_db_engine_and_session, monkeypatch):
        """Full Executor.preview_order() -> execute_order() path for a TWAP order, then awaiting the scheduled slice loop."""
        engine, session = test_db_engine_and_session
        factory = _session_factory(engine)
        executor = Executor(session=session)  # default PAPER broker — safe, deterministic

        scheduled_tasks = []

        def capturing_schedule(**kwargs):
            kwargs["session_factory"] = factory
            task = algo.asyncio.create_task(algo.execute_algo_slices(**kwargs))
            scheduled_tasks.append(task)
            return task

        monkeypatch.setattr(executor_module, "schedule_algo_execution", capturing_schedule)

        proposal = TradeProposal(
            decision_id="algo-integration-001",
            account="primary",
            symbol="QQQ",
            asset_type=AssetType.ETF,
            instruction=Instruction.BUY,
            quantity=12,
            order_type=OrderType.TWAP,
            algo_duration_minutes=1,
            algo_slices=3,
        )

        preview = await executor.preview_order(proposal)
        assert preview.risk_verdict == "APPROVED", preview.risk_details

        receipt = await executor.execute_order(
            decision_id=proposal.decision_id,
            preview_id=preview.preview_id,
            approved_by="test-approver",
            approved_at=datetime.utcnow(),
            attestation="reviewed",
            idempotency_key="idem-algo-integration-001",
        )

        assert receipt.execution_id == f"algo-{proposal.decision_id}"
        assert receipt.broker_response["mode"] == "ALGO"
        assert receipt.broker_response["algo_type"] == "TWAP"
        assert sum(receipt.broker_response["planned_slices"]) == 12
        assert len(scheduled_tasks) == 1

        # A real (short) interval was scheduled — cancel it rather than
        # waiting it out; the plan/receipt assertions above are the point
        # of this test, not observing every slice complete in real time
        # (that's covered directly by TestExecuteAlgoSlices with interval=0).
        scheduled_tasks[0].cancel()
