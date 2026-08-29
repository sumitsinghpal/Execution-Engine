"""
Tests for src/execution/strategy_ranking.py — ranks daily-bar strategies
by score = win_rate * total_trades over a recent rolling window, feeding
the "which strategies should I arm today" decision (see
src/execution/daily_plan.py). run_backtest_suite is monkeypatched so
these run hermetically, no network.
"""

from decimal import Decimal

import pytest

import src.execution.strategy_ranking as strategy_ranking
from src.backtest.engine import BacktestResult


def _result(symbol, strategy_id, trades, wins, pnl=0.0):
    return BacktestResult(
        symbol=symbol, strategy_id=strategy_id, starting_capital=100_000.0,
        ending_capital=100_000.0 + pnl, total_trades=trades, wins=wins, losses=trades - wins,
        win_rate=(wins / trades) if trades else 0.0, profit_factor=None, max_drawdown_pct=0.0,
        total_return_pct=0.0,
    )


class TestRankStrategiesByRecentPerformance:
    @pytest.mark.asyncio
    async def test_ranks_by_win_rate_times_trade_count(self, monkeypatch):
        # golden_cross: 10 trades @ 60% -> score 6.0. rsi2_connors: 1 trade @ 100% -> score 1.0.
        results = [
            _result("QQQ", "golden_cross", trades=10, wins=6),
            _result("QQQ", "rsi2_connors", trades=1, wins=1),
        ]

        async def fake_suite(symbols, strategy_ids, start, end, **kwargs):
            return results, []

        monkeypatch.setattr(strategy_ranking, "run_backtest_suite", fake_suite)

        ranking = await strategy_ranking.rank_strategies_by_recent_performance(["QQQ"], top_n=2)

        assert ranking.rankings[0].strategy_id == "golden_cross"
        assert ranking.rankings[0].score == pytest.approx(6.0)
        assert ranking.rankings[1].strategy_id == "rsi2_connors"
        assert ranking.rankings[1].score == pytest.approx(1.0)
        assert ranking.top_picks == ["golden_cross", "rsi2_connors"]

    @pytest.mark.asyncio
    async def test_aggregates_one_strategy_across_multiple_symbols(self, monkeypatch):
        results = [
            _result("QQQ", "golden_cross", trades=4, wins=2),
            _result("SPY", "golden_cross", trades=6, wins=4),
        ]

        async def fake_suite(symbols, strategy_ids, start, end, **kwargs):
            return results, []

        monkeypatch.setattr(strategy_ranking, "run_backtest_suite", fake_suite)

        ranking = await strategy_ranking.rank_strategies_by_recent_performance(["QQQ", "SPY"])

        assert len(ranking.rankings) == 1
        entry = ranking.rankings[0]
        assert entry.total_trades == 10
        assert entry.wins == 6
        assert entry.win_rate == pytest.approx(0.6)
        assert entry.score == pytest.approx(6.0)

    @pytest.mark.asyncio
    async def test_top_picks_excludes_strategies_with_zero_trades(self, monkeypatch):
        results = [
            _result("QQQ", "golden_cross", trades=3, wins=2),
            _result("QQQ", "turtle_donchian", trades=0, wins=0),
        ]

        async def fake_suite(symbols, strategy_ids, start, end, **kwargs):
            return results, []

        monkeypatch.setattr(strategy_ranking, "run_backtest_suite", fake_suite)

        ranking = await strategy_ranking.rank_strategies_by_recent_performance(["QQQ"], top_n=5)

        assert "turtle_donchian" not in ranking.top_picks
        assert ranking.top_picks == ["golden_cross"]

    @pytest.mark.asyncio
    async def test_top_n_limits_the_number_of_picks(self, monkeypatch):
        results = [
            _result("QQQ", "golden_cross", trades=5, wins=4),
            _result("QQQ", "turtle_donchian", trades=5, wins=3),
            _result("QQQ", "rsi2_connors", trades=5, wins=2),
        ]

        async def fake_suite(symbols, strategy_ids, start, end, **kwargs):
            return results, []

        monkeypatch.setattr(strategy_ranking, "run_backtest_suite", fake_suite)

        ranking = await strategy_ranking.rank_strategies_by_recent_performance(["QQQ"], top_n=2)

        assert len(ranking.top_picks) == 2
        assert ranking.top_picks == ["golden_cross", "turtle_donchian"]

    @pytest.mark.asyncio
    async def test_only_requests_daily_bar_strategies_not_intraday(self, monkeypatch):
        captured = {}

        async def fake_suite(symbols, strategy_ids, start, end, **kwargs):
            captured["strategy_ids"] = strategy_ids
            return [], []

        monkeypatch.setattr(strategy_ranking, "run_backtest_suite", fake_suite)

        await strategy_ranking.rank_strategies_by_recent_performance(["QQQ"])

        assert "opening_range_breakout" not in captured["strategy_ids"]
        assert "vwap_reversion" not in captured["strategy_ids"]
        assert "golden_cross" in captured["strategy_ids"]

    @pytest.mark.asyncio
    async def test_passes_through_lookback_days_as_the_date_range(self, monkeypatch):
        captured = {}

        async def fake_suite(symbols, strategy_ids, start, end, **kwargs):
            captured["start"] = start
            captured["end"] = end
            return [], []

        monkeypatch.setattr(strategy_ranking, "run_backtest_suite", fake_suite)

        ranking = await strategy_ranking.rank_strategies_by_recent_performance(["QQQ"], lookback_days=30)

        assert (captured["end"] - captured["start"]).days == 30
        assert ranking.lookback_days == 30

    @pytest.mark.asyncio
    async def test_errors_from_the_suite_are_carried_through_not_swallowed(self, monkeypatch):
        async def fake_suite(symbols, strategy_ids, start, end, **kwargs):
            return [], [{"symbol": "BADSYM", "strategy_id": "golden_cross", "error": "no data"}]

        monkeypatch.setattr(strategy_ranking, "run_backtest_suite", fake_suite)

        ranking = await strategy_ranking.rank_strategies_by_recent_performance(["BADSYM"])

        assert len(ranking.errors) == 1
        assert ranking.errors[0]["symbol"] == "BADSYM"

    @pytest.mark.asyncio
    async def test_empty_results_yields_an_empty_ranking_without_crashing(self, monkeypatch):
        async def fake_suite(symbols, strategy_ids, start, end, **kwargs):
            return [], []

        monkeypatch.setattr(strategy_ranking, "run_backtest_suite", fake_suite)

        ranking = await strategy_ranking.rank_strategies_by_recent_performance(["QQQ"])

        assert ranking.rankings == []
        assert ranking.top_picks == []
