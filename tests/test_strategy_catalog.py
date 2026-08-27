"""
Tests for src/strategy/catalog.py's entry-rule evaluation. Bar sequences
are hand-constructed to deterministically cross each strategy's condition
rather than relying on the paper broker's randomized synthetic history, so
these tests are not flaky.
"""

from src.strategy import indicators as ind
from src.strategy.bars import Bar, closes, highs, lows, volumes
from src.strategy.catalog import STRATEGIES


def _bar(close, high=None, low=None, open_=None, volume=1000.0, ts="2026-01-01T00:00:00Z"):
    return Bar(
        timestamp=ts,
        open=open_ if open_ is not None else close,
        high=high if high is not None else close,
        low=low if low is not None else close,
        close=close,
        volume=volume,
    )


class TestInsufficientHistoryReturnsNone:
    """Every strategy must fail closed (None, not a crash) when there isn't enough history yet."""

    def test_all_strategies_return_none_on_empty_bars(self):
        for strategy in STRATEGIES.values():
            assert strategy.evaluate([]) is None

    def test_all_strategies_return_none_on_a_handful_of_bars(self):
        bars = [_bar(100 + i) for i in range(5)]
        for strategy in STRATEGIES.values():
            assert strategy.evaluate(bars) is None


class TestOpeningRangeBreakout:
    def test_fires_on_a_clean_break_above_the_opening_range(self):
        opening = [_bar(100, high=101, low=99) for _ in range(6)]
        holding = [_bar(100.5, high=100.8, low=100.2)]
        breakout = _bar(102, high=102.2, low=100.5)
        bars = opening + holding + [breakout]

        signal = STRATEGIES["orb"].evaluate(bars)

        assert signal is not None
        assert signal.entry_price == 102
        assert signal.stop_loss_price == 99  # opening range low
        # target = entry + 2*(or_high - or_low) = 102 + 2*(101-99) = 106
        assert signal.take_profit_price == 106
        assert signal.take_profit_price > signal.entry_price > signal.stop_loss_price

    def test_does_not_fire_when_still_inside_the_range(self):
        opening = [_bar(100, high=101, low=99) for _ in range(6)]
        holding = [_bar(100.2), _bar(100.5)]
        bars = opening + holding

        assert STRATEGIES["orb"].evaluate(bars) is None

    def test_does_not_re_fire_on_the_bar_after_breakout(self):
        opening = [_bar(100, high=101, low=99) for _ in range(6)]
        breakout = _bar(102, high=102.2, low=100.5)
        still_above = _bar(102.5, high=102.7, low=102.1)  # prev close already above OR high
        bars = opening + [breakout, still_above]

        assert STRATEGIES["orb"].evaluate(bars) is None


class TestTurtleDonchianBreakout:
    def test_fires_on_a_new_20_day_high(self):
        # 20 quiet bars establishing the channel, then a clean breakout.
        history = [_bar(100 + (i % 3), high=101 + (i % 3), low=99 + (i % 3)) for i in range(20)]
        breakout = _bar(110, high=110.5, low=105)
        bars = history + [breakout]

        signal = STRATEGIES["turtle_donchian"].evaluate(bars)

        assert signal is not None
        assert signal.entry_price == 110
        assert signal.stop_loss_price < signal.entry_price
        assert signal.take_profit_price > signal.entry_price

    def test_does_not_fire_without_a_new_high(self):
        history = [_bar(100 + (i % 3), high=101 + (i % 3), low=99 + (i % 3)) for i in range(21)]
        assert STRATEGIES["turtle_donchian"].evaluate(history) is None


class TestFiftyTwoWeekHighBreakout:
    def test_fires_on_a_new_52_week_high(self):
        history = [_bar(100, high=105, low=95) for _ in range(252)]
        breakout = _bar(110, high=110, low=108)
        bars = history + [breakout]

        signal = STRATEGIES["fifty_two_week_high"].evaluate(bars)

        assert signal is not None
        assert signal.entry_price == 110
        assert signal.stop_loss_price == 110 * 0.92
        assert signal.take_profit_price == 110 * 1.20

    def test_does_not_fire_without_enough_history(self):
        history = [_bar(100, high=105, low=95) for _ in range(100)]
        assert STRATEGIES["fifty_two_week_high"].evaluate(history) is None


def _downtrend_then_uptrend_bars(n_down=40, n_up=40, start=200.0, down_step=1.5, up_step=2.0):
    """
    A generic price path that reliably produces a moving-average /
    momentum crossover somewhere in the up-leg — used for strategies whose
    exact trigger bar is impractical to hand-compute (MACD, Bollinger,
    RSI(2)), so these assert the *shape* of any fired signal rather than
    an exact index.
    """
    bars = []
    price = start
    for _ in range(n_down):
        price -= down_step
        bars.append(_bar(round(price, 2), high=round(price * 1.002, 2), low=round(price * 0.998, 2)))
    for _ in range(n_up):
        price += up_step
        bars.append(_bar(round(price, 2), high=round(price * 1.002, 2), low=round(price * 0.998, 2)))
    return bars


class TestMacdCrossover:
    def test_a_sustained_reversal_eventually_fires_a_well_formed_signal(self):
        bars = _downtrend_then_uptrend_bars()
        fired = None
        # Scan every prefix's tail so the test doesn't depend on knowing the exact crossover bar.
        for i in range(35, len(bars) + 1):
            signal = STRATEGIES["macd_crossover"].evaluate(bars[:i])
            if signal is not None:
                fired = signal
                break
        assert fired is not None, "expected a MACD crossover somewhere in a sustained reversal"
        assert fired.take_profit_price > fired.entry_price > fired.stop_loss_price


class TestBollingerBreakout:
    def test_a_sustained_reversal_eventually_fires_a_well_formed_signal(self):
        bars = _downtrend_then_uptrend_bars()
        fired = None
        for i in range(25, len(bars) + 1):
            signal = STRATEGIES["bollinger_breakout"].evaluate(bars[:i])
            if signal is not None:
                fired = signal
                break
        assert fired is not None
        assert fired.take_profit_price > fired.entry_price > fired.stop_loss_price


class TestGoldenCross:
    def test_a_sustained_reversal_eventually_fires_a_well_formed_signal(self):
        bars = _downtrend_then_uptrend_bars(n_down=210, n_up=100, down_step=0.3, up_step=1.0)
        fired = None
        for i in range(202, len(bars) + 1):
            signal = STRATEGIES["golden_cross"].evaluate(bars[:i])
            if signal is not None:
                fired = signal
                break
        assert fired is not None
        assert fired.take_profit_price > fired.entry_price > fired.stop_loss_price


class TestRsi2PullbackInUptrend:
    def test_a_dip_within_a_long_uptrend_eventually_fires_a_well_formed_signal(self):
        # A steady climb (keeps price above its 200-day SMA) with a sharp
        # short pullback layered late, so RSI(2) dips oversold without the
        # long-term trend filter failing.
        bars = _downtrend_then_uptrend_bars(n_down=0, n_up=205, start=100.0, up_step=0.8)
        dip = []
        price = bars[-1].close
        for _ in range(4):
            price -= 3.0
            # A real bar's low sits below its close (a wick); without that,
            # the strategy's "recent 5-day low" stop trivially equals the
            # current bar's own close whenever it's the lowest of the five,
            # which a decline like this guarantees, making stop >= entry.
            dip.append(_bar(round(price, 2), low=round(price - 0.5, 2)))
        bars = bars + dip

        fired = None
        for i in range(202, len(bars) + 1):
            signal = STRATEGIES["rsi2_connors"].evaluate(bars[:i])
            if signal is not None:
                fired = signal
                break
        assert fired is not None
        assert fired.take_profit_price > fired.entry_price > fired.stop_loss_price


class TestVwapReversion:
    def test_reclaiming_vwap_after_trading_below_it_fires_a_well_formed_signal(self):
        session = [_bar(100, high=100.3, low=99.7, volume=1000) for _ in range(5)]
        dip = [_bar(95, high=95.3, low=94.5, volume=1000) for _ in range(4)]
        bars = session + dip

        # Confirm the constructed scenario actually has the dip trading
        # below the running VWAP before asserting on the strategy itself.
        vwap_series = ind.vwap(highs(bars), lows(bars), closes(bars), volumes(bars))
        assert bars[-1].close < vwap_series[-1]

        reclaim = _bar(vwap_series[-1] + 1, high=vwap_series[-1] + 1.2, low=95.0, volume=1000)
        bars = bars + [reclaim]

        signal = STRATEGIES["vwap_reversion"].evaluate(bars)

        assert signal is not None
        assert signal.entry_price == reclaim.close
        assert signal.stop_loss_price == 94.5  # session low
        assert signal.take_profit_price > signal.entry_price
