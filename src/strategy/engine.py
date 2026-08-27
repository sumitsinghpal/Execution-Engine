"""
Orchestrates fetching price history from a broker adapter and running one
strategy's evaluate() against it. Pure evaluation only — this module never
places, previews, or persists an order; see src/execution/strategy_signals.py
for what happens with a fired signal (stored for a human to review) and
src/api/server.py for how a reviewed signal turns into an actual
TradeProposal through the normal, unmodified preview/approve/execute gate.
"""

from typing import Optional

from src.brokers.base import BrokerAdapter
from src.strategy.bars import Bar
from src.strategy.catalog import STRATEGIES, SignalDetail, StrategyDefinition


class UnknownStrategyError(ValueError):
    pass


def get_strategy(strategy_id: str) -> StrategyDefinition:
    strategy = STRATEGIES.get(strategy_id)
    if strategy is None:
        raise UnknownStrategyError(f"Unknown strategy_id: {strategy_id}")
    return strategy


async def fetch_bars(broker: BrokerAdapter, symbol: str, strategy: StrategyDefinition) -> list[Bar]:
    raw = await broker.get_price_history(symbol, strategy.bar_interval, strategy.lookback_days)
    return [
        Bar(
            timestamp=b["timestamp"],
            open=float(b["open"]),
            high=float(b["high"]),
            low=float(b["low"]),
            close=float(b["close"]),
            volume=float(b.get("volume") or 0),
        )
        for b in raw
        if b.get("close") is not None
    ]


async def scan(broker: BrokerAdapter, symbol: str, strategy_id: str) -> Optional[SignalDetail]:
    """Fetches history and runs the strategy's entry rule. Returns None if there's no signal right now."""
    strategy = get_strategy(strategy_id)
    bars = await fetch_bars(broker, symbol, strategy)
    return strategy.evaluate(bars)


__all__ = ["UnknownStrategyError", "get_strategy", "fetch_bars", "scan"]
