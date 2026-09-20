"""
Fetch quotes for many symbols the cheapest way the broker supports.

A broker with a batch call (SchwabBrokerAdapter.get_quotes: one HTTP request for
up to 50 symbols) uses it; any other broker falls back to one get_quote() per
symbol, gathered concurrently — exactly what every caller did before. Callers get
the same shape either way: {symbol: quote}, where a symbol that could not be
quoted maps to {"error": "..."} rather than failing the rest. That per-symbol
isolation is this codebase's standard for batch operations.
"""

from __future__ import annotations

import asyncio
from typing import Any, Iterable

from src.logging_config import get_logger

logger = get_logger(__name__)


async def fetch_quotes(broker: Any, symbols: Iterable[str]) -> dict[str, dict[str, Any]]:
    unique = list(dict.fromkeys(symbol for symbol in symbols if symbol))
    if not unique:
        return {}

    batch = getattr(broker, "get_quotes", None)
    if batch is not None:
        try:
            return await batch(unique)
        except Exception as exc:
            # A broker-level failure (outage, rate limit) hits every symbol equally.
            # Deliberately NOT retried symbol-by-symbol: that would multiply the very
            # calls that just failed.
            logger.warning("batch_quotes_failed", symbols=len(unique), error=str(exc))
            return {symbol: {"error": str(exc)} for symbol in unique}

    async def one(symbol: str) -> tuple[str, dict[str, Any]]:
        try:
            return symbol, await broker.get_quote(symbol)
        except Exception as exc:
            return symbol, {"error": str(exc)}

    return dict(await asyncio.gather(*[one(symbol) for symbol in unique]))
