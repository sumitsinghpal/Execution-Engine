"""
Simulates the exact rules the live autonomous trader uses
(src/execution/autonomous_trader.py) over a historical bar series: the
same strategy.evaluate() functions from src/strategy/catalog.py decide
entries, and the same compute_standardized_exit()/size_position() from
src/execution/risk_reward.py decide the stop/target and position size.
Nothing here reimplements that logic separately — a backtest result
reflects what's actually deployed, not a parallel approximation of it that
could silently drift from it.

Deliberately conservative where a single day's bar could satisfy both the
stop and the target (a large gap or wide-range day): the stop is checked
first, so an ambiguous day is scored as a loss rather than a win. Real
intraday order would determine which actually happened first; daily bars
alone can't say, and assuming the loss is the standard conservative
backtesting convention.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional

from src.execution.risk_reward import compute_standardized_exit, size_position
from src.strategy.bars import Bar
from src.strategy.catalog import StrategyDefinition


@dataclass
class BacktestTrade:
    symbol: str
    strategy_id: str
    entry_date: str
    entry_price: float
    stop_loss_price: float
    take_profit_price: float
    quantity: int
    exit_date: Optional[str] = None
    exit_price: Optional[float] = None
    exit_reason: str = "OPEN_AT_END"  # "TARGET" | "STOP" | "OPEN_AT_END"
    pnl_usd: Optional[float] = None
    r_multiple: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "strategy_id": self.strategy_id,
            "entry_date": self.entry_date,
            "entry_price": self.entry_price,
            "stop_loss_price": self.stop_loss_price,
            "take_profit_price": self.take_profit_price,
            "quantity": self.quantity,
            "exit_date": self.exit_date,
            "exit_price": self.exit_price,
            "exit_reason": self.exit_reason,
            "pnl_usd": self.pnl_usd,
            "r_multiple": self.r_multiple,
        }


@dataclass
class BacktestResult:
    symbol: str
    strategy_id: str
    starting_capital: float
    ending_capital: float
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    profit_factor: Optional[float]
    max_drawdown_pct: float
    total_return_pct: float
    # Signals that fired but were skipped because notional_per_trade_usd
    # doesn't cover even 1 share at that entry price (e.g. $100 against a
    # $700 QQQ) — distinct from the strategy simply never firing. Without
    # this, a too-small notional against an expensive symbol reads as "the
    # strategy doesn't work here" when the real story is "the budget
    # doesn't reach this symbol at all."
    signals_too_small_for_notional: int = 0
    trades: list[BacktestTrade] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "strategy_id": self.strategy_id,
            "starting_capital": self.starting_capital,
            "ending_capital": self.ending_capital,
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": self.win_rate,
            "profit_factor": self.profit_factor,
            "max_drawdown_pct": self.max_drawdown_pct,
            "total_return_pct": self.total_return_pct,
            "signals_too_small_for_notional": self.signals_too_small_for_notional,
            "trades": [t.to_dict() for t in self.trades],
        }


def run_backtest(
    bars: list[Bar],
    symbol: str,
    strategy: StrategyDefinition,
    *,
    risk_pct: Decimal,
    reward_risk_ratio: Decimal,
    notional_per_trade_usd: Decimal,
    starting_capital: float = 100_000.0,
) -> BacktestResult:
    """
    Walks the bar series forward once. `strategy.evaluate()` is called on
    every expanding prefix bars[:i+1] — each strategy's own evaluate()
    already returns None until it has enough history, so this doesn't
    duplicate that "how much history does this strategy need" knowledge.
    One position at a time (no pyramiding — matches
    autonomous_trader.scan_for_entries()); a signal firing while already
    in a trade is ignored until the current one closes.
    """
    open_trade: Optional[BacktestTrade] = None
    trades: list[BacktestTrade] = []
    signals_too_small_for_notional = 0

    for i, bar in enumerate(bars):
        if open_trade is not None:
            hit_stop = bar.low <= open_trade.stop_loss_price
            hit_target = bar.high >= open_trade.take_profit_price
            if hit_stop or hit_target:
                exit_price = open_trade.stop_loss_price if hit_stop else open_trade.take_profit_price
                exit_reason = "STOP" if hit_stop else "TARGET"  # stop checked first — see module docstring
                risk_distance = open_trade.entry_price - open_trade.stop_loss_price
                open_trade.exit_date = bar.timestamp
                open_trade.exit_price = exit_price
                open_trade.exit_reason = exit_reason
                open_trade.pnl_usd = (exit_price - open_trade.entry_price) * open_trade.quantity
                open_trade.r_multiple = (exit_price - open_trade.entry_price) / risk_distance if risk_distance else None
                trades.append(open_trade)
                open_trade = None
            continue

        detail = strategy.evaluate(bars[: i + 1])
        if detail is None:
            continue

        exit_levels = compute_standardized_exit(
            detail.entry_price, risk_pct=risk_pct, reward_risk_ratio=reward_risk_ratio
        )
        quantity = size_position(notional_per_trade_usd, detail.entry_price)
        if quantity < 1:
            signals_too_small_for_notional += 1  # too little capital allocated for even one share — matches live behavior, but tracked so it's not confused with "the strategy never fires"
            continue

        open_trade = BacktestTrade(
            symbol=symbol,
            strategy_id=strategy.id,
            entry_date=bar.timestamp,
            entry_price=detail.entry_price,
            stop_loss_price=exit_levels.stop_loss_price,
            take_profit_price=exit_levels.take_profit_price,
            quantity=quantity,
        )

    if open_trade is not None:
        # Still open when the data ran out — mark-to-market at the last
        # close so it's visible, rather than silently dropped from results.
        last_close = bars[-1].close
        risk_distance = open_trade.entry_price - open_trade.stop_loss_price
        open_trade.exit_date = bars[-1].timestamp
        open_trade.exit_price = last_close
        open_trade.exit_reason = "OPEN_AT_END"
        open_trade.pnl_usd = (last_close - open_trade.entry_price) * open_trade.quantity
        open_trade.r_multiple = (last_close - open_trade.entry_price) / risk_distance if risk_distance else None
        trades.append(open_trade)

    return _summarize(symbol, strategy.id, trades, starting_capital, signals_too_small_for_notional)


def _summarize(
    symbol: str, strategy_id: str, trades: list[BacktestTrade], starting_capital: float, signals_too_small_for_notional: int = 0
) -> BacktestResult:
    equity = starting_capital
    peak = starting_capital
    max_drawdown_pct = 0.0
    gross_profit = 0.0
    gross_loss = 0.0
    wins = 0
    losses = 0

    for trade in trades:
        pnl = trade.pnl_usd or 0.0
        equity += pnl
        peak = max(peak, equity)
        if peak > 0:
            drawdown_pct = (peak - equity) / peak * 100
            max_drawdown_pct = max(max_drawdown_pct, drawdown_pct)
        if pnl > 0:
            wins += 1
            gross_profit += pnl
        elif pnl < 0:
            losses += 1
            gross_loss += -pnl

    total = len(trades)
    win_rate = wins / total if total else 0.0
    # None (not float("inf")) whenever there are no losses to divide by —
    # a perfect win streak is a real, if small-sample, outcome, but
    # "infinity" isn't valid JSON (stdlib json.dumps raises on it), and
    # this dict crosses that boundary via POST /v1/backtest/run.
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None
    total_return_pct = (equity - starting_capital) / starting_capital * 100 if starting_capital else 0.0

    return BacktestResult(
        symbol=symbol,
        strategy_id=strategy_id,
        starting_capital=starting_capital,
        ending_capital=equity,
        total_trades=total,
        wins=wins,
        losses=losses,
        win_rate=win_rate,
        profit_factor=profit_factor,
        max_drawdown_pct=max_drawdown_pct,
        total_return_pct=total_return_pct,
        signals_too_small_for_notional=signals_too_small_for_notional,
        trades=trades,
    )


__all__ = ["BacktestResult", "BacktestTrade", "run_backtest"]
