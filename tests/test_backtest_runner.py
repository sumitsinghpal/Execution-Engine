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
from src.backtest.engine import BacktestResult, BacktestTrade, EquityPoint


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


def _trade(exit_date: str, pnl_usd: float) -> BacktestTrade:
    return BacktestTrade(
        symbol="QQQ", strategy_id="golden_cross", entry_date="2024-01-01T00:00:00-05:00",
        entry_price=100.0, stop_loss_price=95.0, take_profit_price=110.0, quantity=10,
        exit_date=exit_date, exit_price=101.0, exit_reason="TARGET", pnl_usd=pnl_usd, r_multiple=1.0,
    )


def _result(symbol: str, strategy_id: str, first_bar_date: str, trades: list[BacktestTrade]) -> BacktestResult:
    return BacktestResult(
        symbol=symbol, strategy_id=strategy_id, starting_capital=100_000.0,
        ending_capital=100_000.0 + sum(t.pnl_usd or 0.0 for t in trades),
        total_trades=len(trades), wins=sum(1 for t in trades if (t.pnl_usd or 0) > 0),
        losses=sum(1 for t in trades if (t.pnl_usd or 0) < 0), win_rate=1.0,
        profit_factor=None, max_drawdown_pct=0.0, total_return_pct=0.0, benchmark_return_pct=5.0,
        equity_curve=[EquityPoint(date=first_bar_date, equity=100_000.0)],
        trades=trades,
    )


class TestCombineEquityCurves:
    def test_merges_trades_from_every_pair_in_chronological_order(self):
        results = [
            _result("QQQ", "golden_cross", "2024-01-01T00:00:00-05:00", [
                _trade("2024-01-10T00:00:00-05:00", 200.0),
                _trade("2024-01-20T00:00:00-05:00", -50.0),
            ]),
            _result("SPY", "turtle_donchian", "2024-01-03T00:00:00-05:00", [
                _trade("2024-01-15T00:00:00-05:00", 100.0),
            ]),
        ]

        combined = runner.combine_equity_curves(results, starting_capital=10_000.0)

        # First point is the earliest first_bar_date across every pair, not just the first result's.
        assert combined["equity_curve"][0]["date"] == "2024-01-01T00:00:00-05:00"
        # Trades from different pairs interleave by exit_date, not grouped by pair.
        exit_dates = [p["date"] for p in combined["equity_curve"][1:]]
        assert exit_dates == ["2024-01-10T00:00:00-05:00", "2024-01-15T00:00:00-05:00", "2024-01-20T00:00:00-05:00"]
        assert combined["ending_capital"] == pytest.approx(10_000.0 + 200.0 + 100.0 - 50.0)
        assert combined["total_return_pct"] == pytest.approx((250.0 / 10_000.0) * 100)

    def test_tracks_combined_drawdown_across_pairs_not_per_pair(self):
        results = [
            _result("QQQ", "golden_cross", "2024-01-01T00:00:00-05:00", [
                _trade("2024-01-10T00:00:00-05:00", 500.0),
            ]),
            _result("SPY", "turtle_donchian", "2024-01-01T00:00:00-05:00", [
                _trade("2024-01-12T00:00:00-05:00", -800.0),  # combined equity dips below starting_capital
            ]),
        ]

        combined = runner.combine_equity_curves(results, starting_capital=10_000.0)

        # Peak was 10,500 after the first trade; dropped to 9,700 after the second.
        assert combined["max_drawdown_pct"] == pytest.approx((10_500.0 - 9_700.0) / 10_500.0 * 100)

    def test_empty_results_yields_empty_curve_without_crashing(self):
        combined = runner.combine_equity_curves([], starting_capital=10_000.0)
        assert combined["equity_curve"] == []
        assert combined["ending_capital"] == 10_000.0
        assert combined["total_return_pct"] == 0.0


class TestSummarizeSuiteIncludesCombinedPortfolio:
    def test_combined_portfolio_uses_the_passed_starting_capital(self):
        results = [_result("QQQ", "golden_cross", "2024-01-01T00:00:00-05:00", [_trade("2024-01-10T00:00:00-05:00", 300.0)])]

        summary = runner.summarize_suite(results, combined_starting_capital=5_000.0)

        assert summary["combined_portfolio"]["starting_capital"] == 5_000.0
        assert summary["combined_portfolio"]["ending_capital"] == pytest.approx(5_300.0)

    def test_defaults_to_100k_when_not_specified(self):
        summary = runner.summarize_suite([])
        assert summary["combined_portfolio"]["starting_capital"] == 100_000.0
