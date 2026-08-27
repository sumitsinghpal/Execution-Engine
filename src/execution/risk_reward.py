"""
Standardized stop-loss/take-profit sizing for autonomous trades.

The strategy catalog (src/strategy/catalog.py) intentionally uses each
strategy's own historically-taught stop/target convention (Turtle's literal
2N, O'Neil's 7-8%/20%, etc.) — that's correct for a human-reviewed signal,
where the point is showing the real, citable rule. Autonomous trading asked
for the opposite: one standardized risk:reward ratio applied uniformly
across every strategy, so exits are mechanical and identical regardless of
which rule triggered the entry — no strategy-flavored discretion creeping
into "when do I get out." src/execution/autonomous_trader.py uses this
instead of a fired signal's own stop_loss_price/take_profit_price.
"""

from __future__ import annotations

from decimal import Decimal
from typing import NamedTuple


class StandardizedExit(NamedTuple):
    stop_loss_price: float
    take_profit_price: float
    risk_distance: float


def compute_standardized_exit(
    entry_price: float,
    *,
    risk_pct: Decimal,
    reward_risk_ratio: Decimal,
) -> StandardizedExit:
    """
    Long-only (matches the strategy catalog — see its own docstring on why
    this system doesn't support shorting). risk_pct is the stop distance as
    a fraction of entry price (e.g. Decimal("0.01") = 1% below entry);
    reward_risk_ratio multiplies that same distance for the target (e.g.
    Decimal("2") = take-profit at 2x the risk distance above entry, a 1:2
    risk:reward trade).
    """
    if entry_price <= 0:
        raise ValueError(f"entry_price must be positive, got {entry_price}")
    if risk_pct <= 0:
        raise ValueError(f"risk_pct must be positive, got {risk_pct}")
    if reward_risk_ratio <= 0:
        raise ValueError(f"reward_risk_ratio must be positive, got {reward_risk_ratio}")

    risk_distance = entry_price * float(risk_pct)
    stop_loss_price = entry_price - risk_distance
    take_profit_price = entry_price + risk_distance * float(reward_risk_ratio)
    return StandardizedExit(stop_loss_price, take_profit_price, risk_distance)


def size_position(notional_usd: Decimal, entry_price: float) -> int:
    """
    Fixed-notional position sizing: how many whole shares fit in
    notional_usd at entry_price. Deliberately simple (not volatility- or
    Kelly-sized) — a fixed dollar amount per autonomous trade is easy to
    reason about and verify, matching "pure strategy" over anything
    cleverer. Returns 0 (skip the trade, never round up into an oversized
    position) if even one share doesn't fit.
    """
    if entry_price <= 0:
        return 0
    return int(float(notional_usd) // entry_price)


__all__ = ["StandardizedExit", "compute_standardized_exit", "size_position"]
