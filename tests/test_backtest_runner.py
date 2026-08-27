"""
Tests for src/backtest/runner.py's run_backtest_suite — mocks
fetch_daily_bars (no network) so these run hermetically, but the specific
thing being guarded here is real: fetching the same symbol's history once
per strategy_id, rather than once per symbol, was observed live to degrade
data quality on repeat fetches against Yahoo's unofficial endpoint (see
run_backtest_suite's docstring) — a strategy needing 200+ days of history
silently stopped firing on data that, fetched once, had plenty.
"""

from datetime import date
from decimal import Decimal

import pytest

import src.backtest.runner as runner


def _flat_daily_bars(n: int, start_price: float = 100.0) -> list[dict]:
    return [
        {
            "timestamp": f"2024-01-{(i % 28) + 1:02d}T00:00:00-05:00",
            "open": start_price, "high": start_price, "low": start_price, "close": start_price, "volume": 1000.0,
        }
        for i in range(n)
    ]


_RISK_KWARGS = dict(risk_pct=Decimal("0.01"), reward_risk_ratio=Decimal("2"), notional_per_trade_usd=Decimal("1000"))


class TestRunBacktestSuiteFetchesOncePerSymbol:
    @pytest.mark.asyncio
    async def test_fetches_each_symbol_exactly_once_regardless_of_strategy_count(self, monkeypatch):
        calls = []

        async def fake_fetch(symbol, start, end):
            calls.append(symbol)
            return _flat_daily_bars(300)

        monkeypatch.setattr(runner, "fetch_daily_bars", fake_fetch)

        await runner.run_backtest_suite(
            ["QQQ", "SPY"],
            ["golden_cross", "turtle_donchian", "rsi2_connors", "macd_crossover", "bollinger_breakout", "fifty_two_week_high"],
            date(2024, 1, 1), date(2024, 12, 1),
            **_RISK_KWARGS,
        )

        assert calls.count("QQQ") == 1
        assert calls.count("SPY") == 1
        assert len(calls) == 2  # not 2 symbols x 6 strategies = 12

    @pytest.mark.asyncio
    async def test_every_strategy_still_runs_against_the_one_fetched_bar_set(self, monkeypatch):
        async def fake_fetch(symbol, start, end):
            return _flat_daily_bars(300)

        monkeypatch.setattr(runner, "fetch_daily_bars", fake_fetch)

        results, errors = await runner.run_backtest_suite(
            ["QQQ"], ["golden_cross", "turtle_donchian", "rsi2_connors"],
            date(2024, 1, 1), date(2024, 12, 1),
            **_RISK_KWARGS,
        )

        assert errors == []
        assert {r.strategy_id for r in results} == {"golden_cross", "turtle_donchian", "rsi2_connors"}

    @pytest.mark.asyncio
    async def test_one_symbols_data_failure_does_not_block_other_symbols(self, monkeypatch):
        from src.backtest.data_source import BacktestDataError

        async def flaky_fetch(symbol, start, end):
            if symbol == "BADSYM":
                raise BacktestDataError("no data")
            return _flat_daily_bars(300)

        monkeypatch.setattr(runner, "fetch_daily_bars", flaky_fetch)

        results, errors = await runner.run_backtest_suite(
            ["BADSYM", "QQQ"], ["golden_cross"],
            date(2024, 1, 1), date(2024, 12, 1),
            **_RISK_KWARGS,
        )

        assert len(errors) == 1
        assert errors[0]["symbol"] == "BADSYM"
        assert len(results) == 1
        assert results[0].symbol == "QQQ"

    @pytest.mark.asyncio
    async def test_too_few_bars_for_a_symbol_is_recorded_as_an_error_per_strategy(self, monkeypatch):
        async def thin_fetch(symbol, start, end):
            return _flat_daily_bars(5)  # below MIN_BARS_REQUIRED

        monkeypatch.setattr(runner, "fetch_daily_bars", thin_fetch)

        results, errors = await runner.run_backtest_suite(
            ["QQQ"], ["golden_cross", "turtle_donchian"],
            date(2024, 1, 1), date(2024, 12, 1),
            **_RISK_KWARGS,
        )

        assert results == []
        assert len(errors) == 2  # one per strategy_id for the one symbol that failed

    @pytest.mark.asyncio
    async def test_unknown_strategy_id_is_recorded_as_an_error_not_a_crash(self, monkeypatch):
        async def fake_fetch(symbol, start, end):
            return _flat_daily_bars(300)

        monkeypatch.setattr(runner, "fetch_daily_bars", fake_fetch)

        results, errors = await runner.run_backtest_suite(
            ["QQQ"], ["golden_cross", "not-a-real-strategy"],
            date(2024, 1, 1), date(2024, 12, 1),
            **_RISK_KWARGS,
        )

        assert any(e["strategy_id"] == "not-a-real-strategy" for e in errors)
        assert any(r.strategy_id == "golden_cross" for r in results)
