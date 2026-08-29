"""
Ranks every daily-bar strategy in the catalog by recent, real performance
— the input to the "which 3 strategies should trade my money today"
decision (see src/execution/daily_plan.py for what happens once picks
are actually armed).

Reuses src/backtest's real simulation (same strategy.evaluate(),
compute_standardized_exit(), size_position() the live autonomous trader
runs — see src/backtest/engine.py's own docstring) over a SHORT rolling
window, not the 3-year window the dashboard's separate "Live updates"
card uses. Recency is the whole point of this ranking; a 3-year backtest
would tell you what worked over 3 years, not what's been working lately.
90 days by default — long enough that every daily-bar strategy in the
catalog (even Golden Cross's 200-day moving average, the longest
lookback) still has a real signal-generation window left over after its
own indicator warms up, short enough that "recent" still means something.

Intraday strategies (ORB, VWAP Reversion) are excluded — this only ever
fetches daily bars, same reasoning as the Live Updates card.

score = win_rate * total_trades, aggregated per strategy across every
watchlist symbol. Deliberately simple and inspectable rather than a
fancier composite: a strategy that's fired 10 times at a 60% win rate
scores 6.0; one lucky 1-for-1 trade scores 1.0. Rewards a strategy that
both wins often AND fires often, without a human having to eyeball two
separate numbers to compare them — but this is a heuristic over a small
sample, not a guarantee; see this module's docstring in daily_plan.py
for why picks still go through a human confirmation step before any
money moves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Optional

from src.backtest.runner import run_backtest_suite
from src.logging_config import get_logger
from src.strategy.catalog import STRATEGIES

logger = get_logger(__name__)

DEFAULT_LOOKBACK_DAYS = 90
DEFAULT_TOP_N = 3


@dataclass
class StrategyRankingEntry:
    strategy_id: str
    strategy_name: str
    total_trades: int
    wins: int
    win_rate: float
    total_pnl_usd: float
    score: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "strategy_name": self.strategy_name,
            "total_trades": self.total_trades,
            "wins": self.wins,
            "win_rate": self.win_rate,
            "total_pnl_usd": self.total_pnl_usd,
            "score": self.score,
        }


@dataclass
class StrategyRanking:
    lookback_days: int
    symbols: list[str]
    computed_for_date: str  # ISO date this ranking covers up to (today, UTC)
    rankings: list[StrategyRankingEntry] = field(default_factory=list)
    top_picks: list[str] = field(default_factory=list)  # strategy_ids, best first
    errors: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "lookback_days": self.lookback_days,
            "symbols": self.symbols,
            "computed_for_date": self.computed_for_date,
            "rankings": [r.to_dict() for r in self.rankings],
            "top_picks": self.top_picks,
            "errors": self.errors,
        }


async def rank_strategies_by_recent_performance(
    symbols: list[str],
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    top_n: int = DEFAULT_TOP_N,
    notional_per_trade_usd: Decimal = Decimal("1000"),
    risk_pct: Decimal = Decimal("0.01"),
    reward_risk_ratio: Decimal = Decimal("2"),
) -> StrategyRanking:
    """
    Runs every daily-bar strategy in the catalog against `symbols` over
    the last `lookback_days`, aggregates each strategy's trades across
    every symbol it traded, scores and ranks them, and returns the top
    `top_n` strategy_ids — ready to hand to DailyPlanService.arm() once a
    human has looked at the numbers behind them.
    """
    daily_strategy_ids = [s.id for s in STRATEGIES.values() if s.bar_interval == "daily"]
    end = date.today()
    start = end - timedelta(days=lookback_days)

    results, errors = await run_backtest_suite(
        symbols,
        daily_strategy_ids,
        start,
        end,
        risk_pct=risk_pct,
        reward_risk_ratio=reward_risk_ratio,
        notional_per_trade_usd=notional_per_trade_usd,
    )

    by_strategy: dict[str, dict] = {}
    for r in results:
        agg = by_strategy.setdefault(r.strategy_id, {"trades": 0, "wins": 0, "pnl": 0.0})
        agg["trades"] += r.total_trades
        agg["wins"] += r.wins
        agg["pnl"] += r.ending_capital - r.starting_capital

    entries: list[StrategyRankingEntry] = []
    for strategy_id, agg in by_strategy.items():
        trades = agg["trades"]
        win_rate = (agg["wins"] / trades) if trades else 0.0
        score = win_rate * trades
        strategy_def = STRATEGIES.get(strategy_id)
        entries.append(StrategyRankingEntry(
            strategy_id=strategy_id,
            strategy_name=strategy_def.name if strategy_def else strategy_id,
            total_trades=trades,
            wins=agg["wins"],
            win_rate=win_rate,
            total_pnl_usd=agg["pnl"],
            score=score,
        ))

    # Ties broken by trade count, then win rate — a strategy that traded
    # more to earn the same score is the one with the larger sample
    # behind it, worth preferring when the headline score is identical.
    entries.sort(key=lambda e: (e.score, e.total_trades, e.win_rate), reverse=True)

    top_picks = [e.strategy_id for e in entries[:top_n] if e.total_trades > 0]

    return StrategyRanking(
        lookback_days=lookback_days,
        symbols=symbols,
        computed_for_date=end.isoformat(),
        rankings=entries,
        top_picks=top_picks,
        errors=errors,
    )


__all__ = [
    "DEFAULT_LOOKBACK_DAYS",
    "DEFAULT_TOP_N",
    "StrategyRanking",
    "StrategyRankingEntry",
    "rank_strategies_by_recent_performance",
]
