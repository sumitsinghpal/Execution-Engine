"""Unit tests for src/strategy/indicators.py against known, hand-checkable series."""

import pytest

from src.strategy import indicators as ind


def test_sma_basic():
    values = [1, 2, 3, 4, 5]
    result = ind.sma(values, 3)
    assert result[:2] == [None, None]
    assert result[2] == pytest.approx(2.0)  # (1+2+3)/3
    assert result[3] == pytest.approx(3.0)  # (2+3+4)/3
    assert result[4] == pytest.approx(4.0)  # (3+4+5)/3


def test_ema_seeds_with_sma_then_smooths():
    values = [1, 2, 3, 4, 5, 6, 7]
    result = ind.ema(values, 3)
    assert result[:2] == [None, None]
    assert result[2] == pytest.approx(2.0)  # seeded as SMA(1,2,3)
    # k = 2/(3+1) = 0.5; ema[3] = 4*0.5 + 2*0.5 = 3.0
    assert result[3] == pytest.approx(3.0)


def test_rsi_all_gains_is_100():
    values = [1, 2, 3, 4, 5, 6, 7, 8]  # strictly increasing -> no losses
    result = ind.rsi(values, period=6)
    assert result[6] == pytest.approx(100.0)


def test_rsi_all_losses_is_zero():
    values = [8, 7, 6, 5, 4, 3, 2, 1]  # strictly decreasing -> no gains
    result = ind.rsi(values, period=6)
    assert result[6] == pytest.approx(0.0)


def test_macd_returns_none_until_enough_history():
    values = [float(i) for i in range(10)]
    macd_line, signal_line, hist = ind.macd(values, fast=12, slow=26, signal=9)
    assert all(v is None for v in macd_line)
    assert all(v is None for v in signal_line)
    assert all(v is None for v in hist)


def test_bollinger_bands_bracket_the_sma():
    values = [10, 11, 9, 12, 8, 13, 7, 14, 6, 15, 20]  # some spread, then a spike
    upper, mid, lower = ind.bollinger_bands(values, period=10, num_std=2.0)
    assert mid[9] is not None
    assert upper[9] > mid[9] > lower[9]


def test_donchian_channel_excludes_current_bar():
    highs = [10, 11, 12, 13, 14, 100]  # current bar spikes to 100
    lows = [5, 4, 3, 2, 1, 0]
    upper, lower = ind.donchian_channel(highs, lows, period=5)
    # upper[5] must be the highest high of bars[0:5] = 14, NOT include the 100 spike at index 5
    assert upper[5] == 14
    assert lower[5] == 1


def test_atr_is_positive_for_volatile_series():
    highs = [10, 11, 12, 11, 13, 12, 14, 13, 15, 14, 16, 15, 17, 16, 18]
    lows = [9, 9, 10, 9, 11, 10, 12, 11, 13, 12, 14, 13, 15, 14, 16]
    closes = [9.5, 10, 11, 10, 12, 11, 13, 12, 14, 13, 15, 14, 16, 15, 17]
    result = ind.atr(highs, lows, closes, period=14)
    assert result[14] is not None
    assert result[14] > 0


def test_vwap_equals_typical_price_for_a_single_bar():
    highs, lows, closes, volumes = [12.0], [10.0], [11.0], [1000.0]
    result = ind.vwap(highs, lows, closes, volumes)
    assert result[0] == pytest.approx((12 + 10 + 11) / 3)


def test_vwap_none_when_zero_volume():
    result = ind.vwap([10.0], [10.0], [10.0], [0.0])
    assert result[0] is None
