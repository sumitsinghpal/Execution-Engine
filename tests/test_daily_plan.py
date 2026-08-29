"""
Tests for src/execution/daily_plan.py — the "armed for today" gate that
autonomous_trader.scan_for_entries() checks before opening any new
position. See that module's own docstring for how this sits alongside
the master enable setting and the kill switch as a third, independent
layer.
"""

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from src.execution.daily_plan import DailyPlanService


class TestArmAndGetActivePlan:
    def test_no_plan_armed_returns_none(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        service = DailyPlanService(session)
        service.disarm(disarmed_by="test-setup")  # clean slate — see this file's shared-DB note below

        assert service.get_active_plan() is None

    def test_arming_makes_it_the_active_plan(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        service = DailyPlanService(session)

        armed = service.arm(["golden_cross", "turtle_donchian"], Decimal("750"), armed_by="sumit")
        active = service.get_active_plan()

        assert active is not None
        assert active.id == armed.id
        assert active.strategy_ids == ["golden_cross", "turtle_donchian"]
        assert active.notional_per_trade_usd == "750"
        assert active.armed_by == "sumit"
        assert active.active is True

    def test_arming_again_replaces_the_previous_plan(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        service = DailyPlanService(session)

        first = service.arm(["golden_cross"], Decimal("500"), armed_by="sumit")
        second = service.arm(["rsi2_connors"], Decimal("1000"), armed_by="sumit")

        active = service.get_active_plan()
        assert active.id == second.id
        assert active.strategy_ids == ["rsi2_connors"]
        session.refresh(first)
        assert first.active is False
        assert first.disarmed_by == "sumit"

    def test_arming_with_empty_strategy_list_raises(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        service = DailyPlanService(session)

        with pytest.raises(ValueError, match="empty"):
            service.arm([], Decimal("500"), armed_by="sumit")

    def test_arming_with_non_positive_notional_raises(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        service = DailyPlanService(session)

        with pytest.raises(ValueError, match="positive"):
            service.arm(["golden_cross"], Decimal("0"), armed_by="sumit")


class TestDisarm:
    def test_disarm_clears_the_active_plan(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        service = DailyPlanService(session)
        service.arm(["golden_cross"], Decimal("500"), armed_by="sumit")

        disarmed = service.disarm(disarmed_by="sumit")

        assert disarmed is not None
        assert disarmed.active is False
        assert service.get_active_plan() is None

    def test_disarming_with_nothing_active_returns_none(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        service = DailyPlanService(session)
        service.disarm(disarmed_by="test-setup")

        result = service.disarm(disarmed_by="sumit")

        assert result is None

    def test_disarmed_positions_stay_disarmed_after_further_get_calls(self, test_db_engine_and_session):
        """Regression guard: get_active_plan() must not resurrect a disarmed row."""
        _, session = test_db_engine_and_session
        service = DailyPlanService(session)
        service.arm(["golden_cross"], Decimal("500"), armed_by="sumit")
        service.disarm(disarmed_by="sumit")

        assert service.get_active_plan() is None
        assert service.get_active_plan() is None  # calling twice must not flip anything back on


class TestNoExpiry:
    """
    arm() is a one-time "take this money and trade it" authorization, not
    a daily chore — see daily_plan.py's module docstring. There is no TTL
    at all: a plan stays active until explicitly disarmed, however much
    time passes.
    """

    def test_a_plan_armed_long_ago_is_still_active(self, test_db_engine_and_session, monkeypatch):
        _, session = test_db_engine_and_session
        service = DailyPlanService(session)
        plan = service.arm(["golden_cross"], Decimal("500"), armed_by="sumit")
        # Simulate a week having passed since arming — still no expiry to check against.
        plan.armed_at = datetime.utcnow() - timedelta(days=7)
        session.add(plan)
        session.commit()

        active = service.get_active_plan()
        assert active is not None
        assert active.id == plan.id

    def test_arm_does_not_set_any_expiry_field(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        service = DailyPlanService(session)

        plan = service.arm(["golden_cross"], Decimal("500"), armed_by="sumit")

        assert not hasattr(plan, "expires_at")
        assert plan.to_dict().get("expires_at") is None  # not even a key with a None value — the field doesn't exist at all


class TestRotateStrategies:
    def test_rotates_the_active_plans_strategy_list_in_place(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        service = DailyPlanService(session)
        armed = service.arm(["golden_cross"], Decimal("500"), armed_by="sumit")

        rotated = service.rotate_strategies(["turtle_donchian", "rsi2_connors"])

        assert rotated.id == armed.id  # same row/plan, not a new one
        assert rotated.strategy_ids == ["turtle_donchian", "rsi2_connors"]
        assert rotated.notional_per_trade_usd == "500"  # untouched by rotation
        assert rotated.armed_by == "sumit"  # untouched by rotation
        assert rotated.last_rotated_at is not None

    def test_rotating_with_nothing_armed_is_a_no_op(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        service = DailyPlanService(session)
        service.disarm(disarmed_by="test-setup")

        result = service.rotate_strategies(["golden_cross"])

        assert result is None

    def test_rotating_to_an_empty_list_raises(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        service = DailyPlanService(session)
        service.arm(["golden_cross"], Decimal("500"), armed_by="sumit")

        with pytest.raises(ValueError, match="empty"):
            service.rotate_strategies([])

    def test_last_rotated_at_is_none_until_the_first_rotation(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        service = DailyPlanService(session)

        plan = service.arm(["golden_cross"], Decimal("500"), armed_by="sumit")

        assert plan.last_rotated_at is None


class TestToDict:
    def test_to_dict_has_the_expected_shape(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        service = DailyPlanService(session)
        plan = service.arm(["golden_cross", "macd_crossover"], Decimal("1250.50"), armed_by="sumit")

        d = plan.to_dict()

        assert d["strategy_ids"] == ["golden_cross", "macd_crossover"]
        assert d["notional_per_trade_usd"] == "1250.50"
        assert d["armed_by"] == "sumit"
        assert d["active"] is True
        assert d["disarmed_at"] is None
        assert d["last_rotated_at"] is None
