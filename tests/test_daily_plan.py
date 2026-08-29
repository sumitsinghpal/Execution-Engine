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


class TestExpiry:
    def test_an_expired_plan_is_not_returned_as_active(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        service = DailyPlanService(session)
        service.arm(["golden_cross"], Decimal("500"), armed_by="sumit", ttl_hours=-0.01)  # already in the past

        assert service.get_active_plan() is None

    def test_a_plan_well_within_its_ttl_is_still_active(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        service = DailyPlanService(session)
        service.arm(["golden_cross"], Decimal("500"), armed_by="sumit", ttl_hours=24)

        active = service.get_active_plan()
        assert active is not None
        assert active.expires_at > datetime.utcnow() + timedelta(hours=23)

    def test_default_ttl_is_24_hours(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        service = DailyPlanService(session)
        before = datetime.utcnow()

        plan = service.arm(["golden_cross"], Decimal("500"), armed_by="sumit")

        assert plan.expires_at - before >= timedelta(hours=23, minutes=59)
        assert plan.expires_at - before <= timedelta(hours=24, minutes=1)


class TestToDict:
    def test_to_dict_has_the_expected_shape(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        service = DailyPlanService(session)
        plan = service.arm(["golden_cross", "macd_crossover"], Decimal("1250.50"), armed_by="sumit", ttl_hours=12)

        d = plan.to_dict()

        assert d["strategy_ids"] == ["golden_cross", "macd_crossover"]
        assert d["notional_per_trade_usd"] == "1250.50"
        assert d["armed_by"] == "sumit"
        assert d["active"] is True
        assert d["disarmed_at"] is None
