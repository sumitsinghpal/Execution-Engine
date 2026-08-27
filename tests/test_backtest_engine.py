"""
Tests for src/backtest/engine.py — pure simulation logic, no network (see
tests/test_backtest_data_source.py note below for why the yfinance-backed
data_source.py itself has no automated test). Uses a small fake
StrategyDefinition with a controllable evaluate() so entries fire on a
known bar rather than needing 200+ days of real crossover data.
"""

from decimal import Decimal

import pytest

from src.backtest.engine import run_backtest
from src.strategy.bars import Bar
from src.strategy.catalog import SignalDetail, StrategyCategory, StrategyDefinition


def _bar(timestamp: str, close: float, high: float = None, low: float = None, open_: float = None) -> Bar:
    return Bar(
        timestamp=timestamp,
        open=open_ if open_ is not None else close,
        high=high if high is not None else close,
        low=low if low is not None else close,
        close=close,
        volume=1000.0,
    )


def _fake_strategy(trigger_at_index: int, repeat: bool = False) -> StrategyDefinition:
    """
    Fires exactly at bar index `trigger_at_index` (entry_price = that
    bar's close). If repeat=True, fires on every bar from trigger_at_index
    onward instead of just once — used to test that run_backtest doesn't
    pyramid into a second position while one is already open.
    """

    def evaluate(bars):
        i = len(bars) - 1
        if i == trigger_at_index or (repeat and i >= trigger_at_index):
            return SignalDetail(entry_price=bars[-1].close, stop_loss_price=0, take_profit_price=0, rationale="fake trigger")
        return None

    return StrategyDefinition(
        id="fake",
        name="Fake Strategy",
        category=StrategyCategory.OTHER,
        description="test double",
        famous_for="",
        risk_reward_label="",
        stop_rule="",
        target_rule="",
        bar_interval="daily",
        lookback_days=1,
        evaluate=evaluate,
    )


_RISK_KWARGS = dict(risk_pct=Decimal("0.01"), reward_risk_ratio=Decimal("2"), notional_per_trade_usd=Decimal("1000"))


class TestRunBacktest:
    def test_target_hit_records_a_winning_trade(self):
        bars = [
            _bar("d0", 100), _bar("d1", 100),
            _bar("d2", 100),  # entry fires here: entry=100, stop=99, target=102
            _bar("d3", 100, high=103, low=99.5),  # target hit (103 >= 102), stop not touched
        ]
        result = run_backtest(bars, "TEST", _fake_strategy(trigger_at_index=2), starting_capital=100_000, **_RISK_KWARGS)

        assert result.total_trades == 1
        trade = result.trades[0]
        assert trade.exit_reason == "TARGET"
        assert trade.entry_price == 100.0
        assert trade.stop_loss_price == pytest.approx(99.0)
        assert trade.take_profit_price == pytest.approx(102.0)
        assert trade.quantity == 10  # $1000 / $100
        assert trade.pnl_usd == pytest.approx(20.0)  # (102-100)*10
        assert trade.r_multiple == pytest.approx(2.0)
        assert result.wins == 1
        assert result.losses == 0
        assert result.win_rate == 1.0
        assert result.ending_capital == pytest.approx(100_020.0)

    def test_stop_hit_records_a_losing_trade(self):
        bars = [
            _bar("d0", 100), _bar("d1", 100),
            _bar("d2", 100),
            _bar("d3", 100, high=100.5, low=98.0),  # stop hit (98 <= 99), target not touched
        ]
        result = run_backtest(bars, "TEST", _fake_strategy(trigger_at_index=2), starting_capital=100_000, **_RISK_KWARGS)

        trade = result.trades[0]
        assert trade.exit_reason == "STOP"
        assert trade.pnl_usd == pytest.approx(-10.0)  # (99-100)*10
        assert trade.r_multiple == pytest.approx(-1.0)
        assert result.wins == 0
        assert result.losses == 1
        assert result.win_rate == 0.0

    def test_ambiguous_same_day_bar_scores_as_a_stop_not_a_target(self):
        """Conservative convention (see engine.py's module docstring): can't know which was hit first intraday from a daily bar, so assume the worse outcome."""
        bars = [
            _bar("d0", 100), _bar("d1", 100),
            _bar("d2", 100),
            _bar("d3", 100, high=105, low=95),  # both stop (99) and target (102) are inside this range
        ]
        result = run_backtest(bars, "TEST", _fake_strategy(trigger_at_index=2), starting_capital=100_000, **_RISK_KWARGS)

        assert result.trades[0].exit_reason == "STOP"

    def test_still_open_at_end_is_marked_to_market_not_dropped(self):
        bars = [
            _bar("d0", 100), _bar("d1", 100),
            _bar("d2", 100),  # entry: 100, stop 99, target 102
            _bar("d3", 100.5, high=100.8, low=99.5),  # inside the band — never resolves
        ]
        result = run_backtest(bars, "TEST", _fake_strategy(trigger_at_index=2), starting_capital=100_000, **_RISK_KWARGS)

        assert result.total_trades == 1
        trade = result.trades[0]
        assert trade.exit_reason == "OPEN_AT_END"
        assert trade.exit_price == pytest.approx(100.5)  # marked at the last close
        assert trade.exit_date == "d3"

    def test_does_not_pyramid_while_a_position_is_open(self):
        bars = [
            _bar("d0", 100), _bar("d1", 100),
            _bar("d2", 100),  # entry fires; strategy would ALSO fire again on d3/d4 (repeat=True) but shouldn't be allowed to
            _bar("d3", 100.5, high=100.8, low=99.5),
            _bar("d4", 101, high=103, low=100),  # target (102) hit here — closes the ONE open trade
        ]
        result = run_backtest(bars, "TEST", _fake_strategy(trigger_at_index=2, repeat=True), starting_capital=100_000, **_RISK_KWARGS)

        assert result.total_trades == 1  # not 3, despite the fake strategy firing on every bar from index 2 onward

    def test_zero_sized_quantity_skips_the_trade_entirely(self):
        bars = [_bar("d0", 100), _bar("d1", 100), _bar("d2", 5000)]  # $1000 notional can't afford even 1 share at $5000
        result = run_backtest(
            bars, "TEST", _fake_strategy(trigger_at_index=2),
            risk_pct=Decimal("0.01"), reward_risk_ratio=Decimal("2"), notional_per_trade_usd=Decimal("1000"),
            starting_capital=100_000,
        )

        assert result.total_trades == 0

    def test_no_signal_ever_fires_yields_an_empty_flat_result(self):
        bars = [_bar("d0", 100), _bar("d1", 100), _bar("d2", 100)]

        strategy = _fake_strategy(trigger_at_index=999)  # never reached — only 3 bars in this fixture
        result = run_backtest(bars, "TEST", strategy, starting_capital=100_000, **_RISK_KWARGS)

        assert result.total_trades == 0
        assert result.win_rate == 0.0
        assert result.ending_capital == 100_000
        assert result.total_return_pct == 0.0
        assert result.max_drawdown_pct == 0.0
        assert result.profit_factor is None


class TestSummaryMetrics:
    def test_win_rate_and_profit_factor_across_multiple_trades(self):
        # Two winning $20 trades, one losing $10 trade: win_rate 2/3, profit_factor 40/10 = 4.0
        bars = [
            _bar("d0", 100), _bar("d1", 100),
            _bar("d2", 100), _bar("d3", 100, high=103, low=99.5),      # win: entry 100 -> target 102 hit
            _bar("d4", 100), _bar("d5", 100, high=103, low=99.5),      # win: entry 100 -> target 102 hit
            _bar("d6", 100), _bar("d7", 100, high=100.5, low=98.0),    # loss: entry 100 -> stop 99 hit
        ]
        strategy = _fake_strategy(trigger_at_index=0)  # placeholder, overridden below

        def fires_every_other_bar_when_flat(bars):
            # Fires on d2, d4, d6 (the "entry" bars in the fixture above) — d0/d1 are just padding for lookback.
            if bars[-1].timestamp in ("d2", "d4", "d6"):
                return SignalDetail(entry_price=bars[-1].close, stop_loss_price=0, take_profit_price=0, rationale="fake")
            return None

        strategy.evaluate = fires_every_other_bar_when_flat

        result = run_backtest(bars, "TEST", strategy, starting_capital=100_000, **_RISK_KWARGS)

        assert result.total_trades == 3
        assert result.wins == 2
        assert result.losses == 1
        assert result.win_rate == pytest.approx(2 / 3)
        assert result.profit_factor == pytest.approx(40.0 / 10.0)
        assert result.ending_capital == pytest.approx(100_000 + 20 + 20 - 10)

    def test_max_drawdown_reflects_a_losing_trade_after_a_winning_one(self):
        bars = [
            _bar("d0", 100), _bar("d1", 100),
            _bar("d2", 100), _bar("d3", 100, high=103, low=99.5),   # win: +20, equity 100_020, peak 100_020
            _bar("d4", 100), _bar("d5", 100, high=100.5, low=98.0),  # loss: -10, equity 100_010
        ]
        strategy = _fake_strategy(trigger_at_index=0)

        def fires_on(bars):
            if bars[-1].timestamp in ("d2", "d4"):
                return SignalDetail(entry_price=bars[-1].close, stop_loss_price=0, take_profit_price=0, rationale="fake")
            return None

        strategy.evaluate = fires_on

        result = run_backtest(bars, "TEST", strategy, starting_capital=100_000, **_RISK_KWARGS)

        # Drawdown from peak 100_020 to 100_010 = 10 / 100_020 * 100
        assert result.max_drawdown_pct == pytest.approx(10 / 100_020 * 100, rel=1e-3)
