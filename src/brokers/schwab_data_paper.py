"""
Real Schwab market data, simulated (paper) order execution — the broker
used by src/execution/autonomous_trader.py when Schwab is configured.

Subclasses PaperBrokerAdapter and overrides ONLY get_quote() and
get_price_history() to pull real data from a SchwabBrokerAdapter instance.
Everything else — preview_order(), submit_order(), get_order_status(),
get_positions(), get_balances() — is inherited from PaperBrokerAdapter
completely unchanged: no order this broker ever previews or submits
reaches Schwab. PaperBrokerAdapter.preview_order() itself calls
self.get_quote(...) for a MARKET order's price estimate (every order the
autonomous trader places is MARKET), so that override alone is enough to
make the simulated fill price consistent with the real quote that
triggered the trade — not a second, unrelated synthetic number.

Falls back to the inherited synthetic implementation if Schwab is
unreachable, unauthenticated, or returns no data for a symbol (market
closed with no cached quote, a bad ticker, etc.) — a market-data hiccup
must never stop the autonomous loop from evaluating on some price, since
degrading to "no signals this cycle" everywhere is safer than raising.
"""

from __future__ import annotations

from typing import Any

from src.brokers.paper import PaperBrokerAdapter
from src.brokers.schwab.adapter import SchwabBrokerAdapter
from src.logging_config import get_logger

logger = get_logger(__name__)


class SchwabDataPaperBroker(PaperBrokerAdapter):
    def __init__(self, schwab: SchwabBrokerAdapter):
        self._schwab = schwab

    async def get_quote(self, symbol: str) -> dict[str, Any]:
        try:
            quote = await self._schwab.get_quote(symbol)
            if quote.get("last") is not None:
                return quote
            logger.warning("schwab_data_paper_quote_missing_last", symbol=symbol)
        except Exception as exc:
            logger.warning("schwab_data_paper_quote_failed_falling_back_to_synthetic", symbol=symbol, error=str(exc))
        return await super().get_quote(symbol)

    async def get_quotes(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        """
        One batched Schwab call for real quotes; any symbol Schwab did not return (or
        the whole batch, if Schwab is down or rate-limiting) falls back to the
        synthetic quote, exactly as get_quote() does — same "a market-data hiccup must
        never stop the loop" rule, at one call instead of one per symbol.
        """
        unique = list(dict.fromkeys(symbols))
        try:
            real = await self._schwab.get_quotes(unique)
        except Exception as exc:
            logger.warning("schwab_data_paper_batch_quotes_failed_falling_back_to_synthetic", error=str(exc))
            real = {}
        result: dict[str, dict[str, Any]] = {}
        for symbol in unique:
            quote = real.get(symbol)
            if quote and "error" not in quote and quote.get("last") is not None:
                result[symbol] = quote
            else:
                result[symbol] = await super().get_quote(symbol)
        return result

    async def get_price_history(self, symbol: str, bar_interval: str, lookback_days: int) -> list[dict[str, Any]]:
        try:
            bars = await self._schwab.get_price_history(symbol, bar_interval, lookback_days)
            if bars:
                return bars
            logger.warning("schwab_data_paper_history_empty_falling_back_to_synthetic", symbol=symbol)
        except Exception as exc:
            logger.warning("schwab_data_paper_history_failed_falling_back_to_synthetic", symbol=symbol, error=str(exc))
        return await super().get_price_history(symbol, bar_interval, lookback_days)


__all__ = ["SchwabDataPaperBroker"]
