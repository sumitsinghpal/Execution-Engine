"""Shared OHLCV bar type used across price history sources and strategies."""

from dataclasses import dataclass


@dataclass
class Bar:
    timestamp: str  # ISO-8601
    open: float
    high: float
    low: float
    close: float
    volume: float


def closes(bars: list[Bar]) -> list[float]:
    return [b.close for b in bars]


def highs(bars: list[Bar]) -> list[float]:
    return [b.high for b in bars]


def lows(bars: list[Bar]) -> list[float]:
    return [b.low for b in bars]


def volumes(bars: list[Bar]) -> list[float]:
    return [b.volume for b in bars]


__all__ = ["Bar", "closes", "highs", "lows", "volumes"]
