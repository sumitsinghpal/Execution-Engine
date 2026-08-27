"""
Pure-Python technical indicators over OHLC bar series. No numpy/pandas
dependency — this repo has neither, and these series are small (at most a
few hundred bars), so plain Python is fast enough and keeps the dependency
footprint unchanged.

Every function returns one value per input bar, with `None` wherever there
isn't yet enough history to compute a real value (e.g. the first 19 bars of
a 20-period SMA) — never a fabricated 0 or a silently-shortened list, so
callers can't misalign an indicator series against its bars by accident.
"""

from typing import Optional


def sma(values: list[float], period: int) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(values)
    for i in range(period - 1, len(values)):
        window = values[i - period + 1 : i + 1]
        out[i] = sum(window) / period
    return out


def ema(values: list[float], period: int) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return out
    k = 2 / (period + 1)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(values: list[float], period: int = 14) -> list[Optional[float]]:
    """Wilder's RSI — the standard formulation (smoothed average gain/loss)."""
    out: list[Optional[float]] = [None] * len(values)
    if len(values) < period + 1:
        return out

    gains = [max(values[i] - values[i - 1], 0.0) for i in range(1, len(values))]
    losses = [max(values[i - 1] - values[i], 0.0) for i in range(1, len(values))]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    out[period] = _rsi_from_averages(avg_gain, avg_loss)

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i + 1] = _rsi_from_averages(avg_gain, avg_loss)
    return out


def _rsi_from_averages(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def macd(
    values: list[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[list[Optional[float]], list[Optional[float]], list[Optional[float]]]:
    """Returns (macd_line, signal_line, histogram)."""
    ema_fast = ema(values, fast)
    ema_slow = ema(values, slow)
    macd_line: list[Optional[float]] = [
        (f - s) if f is not None and s is not None else None for f, s in zip(ema_fast, ema_slow)
    ]

    # EMA of the MACD line itself, skipping the leading Nones.
    defined = [v for v in macd_line if v is not None]
    signal_defined = ema(defined, signal)
    signal_line: list[Optional[float]] = [None] * len(macd_line)
    offset = len(macd_line) - len(defined)
    for i, v in enumerate(signal_defined):
        signal_line[offset + i] = v

    histogram: list[Optional[float]] = [
        (m - s) if m is not None and s is not None else None for m, s in zip(macd_line, signal_line)
    ]
    return macd_line, signal_line, histogram


def bollinger_bands(
    values: list[float], period: int = 20, num_std: float = 2.0
) -> tuple[list[Optional[float]], list[Optional[float]], list[Optional[float]]]:
    """Returns (upper, mid, lower)."""
    mid = sma(values, period)
    upper: list[Optional[float]] = [None] * len(values)
    lower: list[Optional[float]] = [None] * len(values)
    for i in range(period - 1, len(values)):
        window = values[i - period + 1 : i + 1]
        m = mid[i]
        if m is None:
            continue
        variance = sum((x - m) ** 2 for x in window) / period
        std = variance**0.5
        upper[i] = m + num_std * std
        lower[i] = m - num_std * std
    return upper, mid, lower


def donchian_channel(
    highs: list[float], lows: list[float], period: int = 20
) -> tuple[list[Optional[float]], list[Optional[float]]]:
    """Returns (upper, lower) — the highest high / lowest low of the trailing period, NOT including the current bar."""
    upper: list[Optional[float]] = [None] * len(highs)
    lower: list[Optional[float]] = [None] * len(lows)
    for i in range(period, len(highs)):
        upper[i] = max(highs[i - period : i])
        lower[i] = min(lows[i - period : i])
    return upper, lower


def atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> list[Optional[float]]:
    """Average True Range (Wilder smoothing)."""
    n = len(closes)
    out: list[Optional[float]] = [None] * n
    if n < period + 1:
        return out

    true_ranges = []
    for i in range(1, n):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        true_ranges.append(tr)

    avg = sum(true_ranges[:period]) / period
    out[period] = avg
    for i in range(period, len(true_ranges)):
        avg = (avg * (period - 1) + true_ranges[i]) / period
        out[i + 1] = avg
    return out


def vwap(highs: list[float], lows: list[float], closes: list[float], volumes: list[float]) -> list[Optional[float]]:
    """
    Session VWAP: cumulative(typical price * volume) / cumulative(volume),
    reset from the start of the given bar series (callers pass only the
    current session's intraday bars, not a multi-day series).
    """
    out: list[Optional[float]] = [None] * len(closes)
    cum_pv = 0.0
    cum_vol = 0.0
    for i in range(len(closes)):
        typical = (highs[i] + lows[i] + closes[i]) / 3
        cum_pv += typical * volumes[i]
        cum_vol += volumes[i]
        out[i] = (cum_pv / cum_vol) if cum_vol > 0 else None
    return out


__all__ = ["sma", "ema", "rsi", "macd", "bollinger_bands", "donchian_channel", "atr", "vwap"]
