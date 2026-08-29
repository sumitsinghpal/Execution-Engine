"""
Tests for src/execution/strategy_rotation.py — the background loop that
keeps an armed daily plan's strategy selection current on its own, so
"take this money and trade it" stays a one-time action (see
src/execution/daily_plan.py's module docstring). rank_strategies_by_
recent_performance is monkeypatched so these run hermetically, no
network.
"""

from decimal import Decimal

import pytest

import src.execution.strategy_rotation as strategy_rotation
from src.config import Settings
from src.execution.daily_plan import DailyPlanService
from src.execution.strategy_ranking import StrategyRanking


def _settings(**overrides):
    defaults = dict(_env_file=None, env="test", AUTONOMOUS_WATCHLIST="QQQ,SPY,IWM")
    defaults.update(overrides)
    return Settings(**defaults)


def _fake_ranking(top_picks):
    return StrategyRanking(lookback_days=90, symbols=["QQQ"], computed_for_date="2026-01-01", rankings=[], top_picks=top_picks, errors=[])


class TestRotateOnce:
    @pytest.mark.asyncio
    async def test_no_op_when_nothing_is_armed(self, test_db_engine_and_session, monkeypatch):
        _, session = test_db_engine_and_session
        DailyPlanService(session).disarm(disarmed_by="test-setup")
        called = []

        async def fake_rank(*args, **kwargs):
            called.append(True)
            return _fake_ranking(["golden_cross"])

        monkeypatch.setattr(strategy_rotation, "rank_strategies_by_recent_performance", fake_rank)

        rotated = await strategy_rotation.rotate_once(session, _settings())

        assert rotated is False
        assert called == []  # not even attempted — no armed plan to rotate, so no need to hit the (network-bound) ranking at all

    @pytest.mark.asyncio
    async def test_rotates_the_active_plan_to_the_new_top_picks(self, test_db_engine_and_session, monkeypatch):
        _, session = test_db_engine_and_session
        DailyPlanService(session).arm(["golden_cross"], Decimal("500"), armed_by="sumit")

        async def fake_rank(*args, **kwargs):
            return _fake_ranking(["turtle_donchian", "rsi2_connors"])

        monkeypatch.setattr(strategy_rotation, "rank_strategies_by_recent_performance", fake_rank)

        rotated = await strategy_rotation.rotate_once(session, _settings())

        assert rotated is True
        active = DailyPlanService(session).get_active_plan()
        assert active.strategy_ids == ["turtle_donchian", "rsi2_connors"]
        assert active.notional_per_trade_usd == "500"  # untouched by rotation

    @pytest.mark.asyncio
    async def test_uses_the_plans_own_notional_for_the_ranking_backtest(self, test_db_engine_and_session, monkeypatch):
        _, session = test_db_engine_and_session
        DailyPlanService(session).arm(["golden_cross"], Decimal("2500"), armed_by="sumit")
        captured = {}

        async def fake_rank(symbols, **kwargs):
            captured["notional_per_trade_usd"] = kwargs.get("notional_per_trade_usd")
            return _fake_ranking(["golden_cross"])

        monkeypatch.setattr(strategy_rotation, "rank_strategies_by_recent_performance", fake_rank)

        await strategy_rotation.rotate_once(session, _settings())

        assert captured["notional_per_trade_usd"] == Decimal("2500")

    @pytest.mark.asyncio
    async def test_keeps_the_current_selection_when_ranking_returns_no_picks(self, test_db_engine_and_session, monkeypatch):
        _, session = test_db_engine_and_session
        DailyPlanService(session).arm(["golden_cross"], Decimal("500"), armed_by="sumit")

        async def fake_rank(*args, **kwargs):
            return _fake_ranking([])  # nothing fired in the window

        monkeypatch.setattr(strategy_rotation, "rank_strategies_by_recent_performance", fake_rank)

        rotated = await strategy_rotation.rotate_once(session, _settings())

        assert rotated is False
        active = DailyPlanService(session).get_active_plan()
        assert active.strategy_ids == ["golden_cross"]  # unchanged

    @pytest.mark.asyncio
    async def test_a_ranking_failure_does_not_disarm_or_crash(self, test_db_engine_and_session, monkeypatch):
        _, session = test_db_engine_and_session
        DailyPlanService(session).arm(["golden_cross"], Decimal("500"), armed_by="sumit")

        async def failing_rank(*args, **kwargs):
            raise RuntimeError("simulated network failure")

        monkeypatch.setattr(strategy_rotation, "rank_strategies_by_recent_performance", failing_rank)

        rotated = await strategy_rotation.rotate_once(session, _settings())

        assert rotated is False
        active = DailyPlanService(session).get_active_plan()
        assert active is not None  # still armed — a transient ranking failure must not disarm anything
        assert active.strategy_ids == ["golden_cross"]
