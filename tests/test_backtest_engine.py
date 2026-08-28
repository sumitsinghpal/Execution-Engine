"""
Tests for src/backtest/engine.py — pure simulation logic, no network
(src/backtest/data_source.py's yfinance wrapper has no automated test —
verified manually instead, same reasoning as the Schwab adapter not being
tested against a live broker). Uses a small fake StrategyDefinition with a
controllable evaluate() so entries fire on a known bar rather than needing
200+ days of real crossover data.
"""

import json
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


_RISK_KWARGS = dict(
    risk_pct=Decimal("0.01"), reward_risk_ratio=Decimal("2"), notional_per_trade_usd=Decimal("1000"),
    slippage_bps=Decimal("0"), commission_per_order_usd=Decimal("0"),  # exact-fill arithmetic below assumes zero of both; see TestSlippageAndCommission for those specifically
)


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

    def test_profit_factor_is_none_not_infinite_when_there_are_no_losses(self):
        """
        A real bug this test would have caught: profit_factor used to be
        float("inf") for an all-wins result — Python's stdlib json.dumps
        (what FastAPI's default JSONResponse uses) raises ValueError on
        inf/nan, so POST /v1/backtest/run 500'd the moment a real backtest
        pair happened to have zero losing trades.
        """
        bars = [
            _bar("d0", 100), _bar("d1", 100),
            _bar("d2", 100), _bar("d3", 100, high=103, low=99.5),  # win
        ]
        strategy = _fake_strategy(trigger_at_index=0)
        strategy.evaluate = lambda bars: (
            SignalDetail(entry_price=bars[-1].close, stop_loss_price=0, take_profit_price=0, rationale="fake")
            if bars[-1].timestamp == "d2" else None
        )

        result = run_backtest(bars, "TEST", strategy, starting_capital=100_000, **_RISK_KWARGS)

        assert result.wins == 1
        assert result.losses == 0
        assert result.profit_factor is None
        json.dumps(result.to_dict())  # must not raise — this is the exact boundary the real bug crossed

    def test_zero_sized_quantity_skips_the_trade_entirely(self):
        bars = [_bar("d0", 100), _bar("d1", 100), _bar("d2", 5000)]  # $1000 notional can't afford even 1 share at $5000
        result = run_backtest(
            bars, "TEST", _fake_strategy(trigger_at_index=2),
            risk_pct=Decimal("0.01"), reward_risk_ratio=Decimal("2"), notional_per_trade_usd=Decimal("1000"),
            starting_capital=100_000,
        )

        assert result.total_trades == 0
        # This is the real, reproduced bug this field exists for: QQQ/SPY/
        # IWM trade at $300-770/share, and a $100 notional silently
        # produced "0 trades" for every strategy on every symbol — reading
        # as "the strategies don't work" when the real story is "the
        # budget doesn't reach even 1 share here." Distinguishing the two
        # is the whole point of this field.
        assert result.signals_too_small_for_notional == 1

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


class TestSlippageAndCommission:
    def test_slippage_worsens_both_entry_and_exit_fills(self):
        bars = [
            _bar("d0", 100), _bar("d1", 100),
            _bar("d2", 100),  # entry signal at close=100
            _bar("d3", 100, high=103, low=99.5),  # target (102, pre-slippage) hit
        ]
        result = run_backtest(
            bars, "TEST", _fake_strategy(trigger_at_index=2), starting_capital=100_000,
            risk_pct=Decimal("0.01"), reward_risk_ratio=Decimal("2"), notional_per_trade_usd=Decimal("1000"),
            slippage_bps=Decimal("100"), commission_per_order_usd=Decimal("0"),  # 1% — exaggerated on purpose so the effect is unmistakable
        )

        trade = result.trades[0]
        # Entry: BUY fills worse (higher) — 100 * 1.01 = 101
        assert trade.entry_price == pytest.approx(101.0)
        # Exit: SELL fills worse (lower) — 102 * 0.99 = 100.98
        assert trade.exit_price == pytest.approx(100.98)

    def test_commission_is_charged_on_a_real_exit_but_not_an_open_at_end_mark(self):
        bars_with_exit = [
            _bar("d0", 100), _bar("d1", 100),
            _bar("d2", 100),
            _bar("d3", 100, high=103, low=99.5),  # target hit — a real exit
        ]
        result_with_exit = run_backtest(
            bars_with_exit, "TEST", _fake_strategy(trigger_at_index=2), starting_capital=100_000,
            risk_pct=Decimal("0.01"), reward_risk_ratio=Decimal("2"), notional_per_trade_usd=Decimal("1000"),
            slippage_bps=Decimal("0"), commission_per_order_usd=Decimal("5"),
        )
        # (102-100)*10 - 5 = 15, not the commission-free 20
        assert result_with_exit.trades[0].pnl_usd == pytest.approx(15.0)

        bars_open_at_end = [
            _bar("d0", 100), _bar("d1", 100),
            _bar("d2", 100),  # entry
            _bar("d3", 100.5, high=100.8, low=99.5),  # never resolves — OPEN_AT_END
        ]
        result_open = run_backtest(
            bars_open_at_end, "TEST", _fake_strategy(trigger_at_index=2), starting_capital=100_000,
            risk_pct=Decimal("0.01"), reward_risk_ratio=Decimal("2"), notional_per_trade_usd=Decimal("1000"),
            slippage_bps=Decimal("0"), commission_per_order_usd=Decimal("5"),
        )
        # (100.5-100)*10 = 5, no commission deducted — nothing was actually bought or sold at exit
        assert result_open.trades[0].pnl_usd == pytest.approx(5.0)


class TestBenchmarkReturn:
    def test_benchmark_is_buy_and_hold_of_the_same_symbol_over_the_same_window(self):
        bars = [_bar("d0", 100), _bar("d1", 105), _bar("d2", 110)]
        strategy = _fake_strategy(trigger_at_index=999)  # never fires — isolates the benchmark calc

        result = run_backtest(bars, "TEST", strategy, starting_capital=100_000, **_RISK_KWARGS)

        assert result.benchmark_return_pct == pytest.approx(10.0)  # (110-100)/100 * 100

    def test_empty_bars_returns_a_flat_zero_benchmark_not_a_crash(self):
        strategy = _fake_strategy(trigger_at_index=0)
        result = run_backtest([], "TEST", strategy, starting_capital=100_000, **_RISK_KWARGS)
        assert result.benchmark_return_pct == 0.0
        assert result.total_trades == 0


class TestEquityCurve:
    def test_equity_curve_starts_at_starting_capital_and_has_one_point_per_closed_trade(self):
        bars = [
            _bar("d0", 100), _bar("d1", 100),
            _bar("d2", 100), _bar("d3", 100, high=103, low=99.5),  # win, closes d3
            _bar("d4", 100), _bar("d5", 100, high=100.5, low=98.0),  # loss, closes d5
        ]
        strategy = _fake_strategy(trigger_at_index=0)

        def fires_on(bars):
            if bars[-1].timestamp in ("d2", "d4"):
                return SignalDetail(entry_price=bars[-1].close, stop_loss_price=0, take_profit_price=0, rationale="fake")
            return None

        strategy.evaluate = fires_on
        result = run_backtest(bars, "TEST", strategy, starting_capital=100_000, **_RISK_KWARGS)

        assert len(result.equity_curve) == 3  # starting point + 2 closed trades
        assert result.equity_curve[0].date == "d0"
        assert result.equity_curve[0].equity == pytest.approx(100_000)
        assert result.equity_curve[1].date == "d3"
        assert result.equity_curve[1].equity == pytest.approx(100_020)  # +20 win
        assert result.equity_curve[2].date == "d5"
        assert result.equity_curve[2].equity == pytest.approx(100_010)  # -10 loss
