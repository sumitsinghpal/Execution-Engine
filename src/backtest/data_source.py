"""
Real historical OHLCV data for backtesting, via yfinance (free, no API
key — see pyproject.toml's dependency comment for why this over
TradingView). This is the only module in the codebase that imports
yfinance; nothing else depends on it, and it's never used for live
trading — only src/backtest/engine.py consumes it.

Shapes its output as the same {"timestamp", "open", "high", "low",
"close", "volume"} dict list src/brokers/base.py's BrokerAdapter.
get_price_history() returns, so src/strategy/engine.py's fetch_bars() and
every strategy in src/strategy/catalog.py run completely unchanged here —
a backtest evaluates the exact same evaluate() functions live trading does,
not a reimplementation of them.
"""

from __future__ import annotations

import asyncio
import math
from datetime import date
from typing import Any

import yfinance as yf


class BacktestDataError(RuntimeError):
    """Raised when historical data can't be fetched or is unusable (e.g. an unknown symbol, or too little history)."""


def _fetch_history_sync(symbol: str, start: date, end: date) -> list[dict[str, Any]]:
    ticker = yf.Ticker(symbol)
    try:
        df = ticker.history(start=start.isoformat(), end=end.isoformat(), interval="1d")
    except Exception as exc:
        raise BacktestDataError(f"Failed to fetch history for {symbol}: {exc}") from exc

    if df is None or df.empty:
        raise BacktestDataError(f"No historical data returned for {symbol} between {start} and {end}")

    bars: list[dict[str, Any]] = []
    for timestamp, row in df.iterrows():
        # A 0-volume/NaN row (a data gap, or a split-adjustment artifact)
        # would corrupt an indicator computed over it — skip rather than
        # feed a strategy a fabricated bar.
        if row[["Open", "High", "Low", "Close"]].isna().any():
            continue
        volume = float(row["Volume"])
        bars.append(
            {
                "timestamp": timestamp.isoformat(),
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
                "volume": 0.0 if math.isnan(volume) else volume,
            }
        )
    return bars


async def fetch_daily_bars(symbol: str, start: date, end: date) -> list[dict[str, Any]]:
    """
    Oldest-first daily OHLCV bars for `symbol` between start and end
    (inclusive-ish; yfinance's own end-date convention). Runs the
    synchronous yfinance call in a thread so it doesn't block the event
    loop — same reason PaperBrokerAdapter's synthetic generator doesn't
    need this but a real network call does.
    """
    return await asyncio.to_thread(_fetch_history_sync, symbol, start, end)


__all__ = ["BacktestDataError", "fetch_daily_bars"]
