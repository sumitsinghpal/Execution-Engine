"""Fetches real historical data and runs it through src/backtest/engine.py for one or many (symbol, strategy) pairs."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from src.backtest.data_source import BacktestDataError, fetch_daily_bars
from src.backtest.engine import BacktestResult, run_backtest
from src.logging_config import get_logger
from src.strategy.bars import Bar
from src.strategy.engine import UnknownStrategyError, get_strategy

logger = get_logger(__name__)

MIN_BARS_REQUIRED = 30  # below this, no strategy in the catalog could ever fire (the longest lookback needs 260)


async def run_backtest_for_symbol(
    symbol: str,
    strategy_id: str,
    start: date,
    end: date,
    *,
    risk_pct: Decimal,
    reward_risk_ratio: Decimal,
    notional_per_trade_usd: Decimal,
    starting_capital: float = 100_000.0,
) -> BacktestResult:
    strategy = get_strategy(strategy_id)  # raises UnknownStrategyError

    raw_bars = await fetch_daily_bars(symbol, start, end)
    if len(raw_bars) < MIN_BARS_REQUIRED:
        raise BacktestDataError(
            f"Only {len(raw_bars)} bars available for {symbol} between {start} and {end}; "
            f"need at least {MIN_BARS_REQUIRED} for any strategy to evaluate at all."
        )

    bars = [
        Bar(timestamp=b["timestamp"], open=b["open"], high=b["high"], low=b["low"], close=b["close"], volume=b["volume"])
        for b in raw_bars
    ]

    return run_backtest(
        bars,
        symbol,
        strategy,
        risk_pct=risk_pct,
        reward_risk_ratio=reward_risk_ratio,
        notional_per_trade_usd=notional_per_trade_usd,
        starting_capital=starting_capital,
    )


async def run_backtest_suite(
    symbols: list[str],
    strategy_ids: list[str],
    start: date,
    end: date,
    *,
    risk_pct: Decimal,
    reward_risk_ratio: Decimal,
    notional_per_trade_usd: Decimal,
    starting_capital: float = 100_000.0,
) -> tuple[list[BacktestResult], list[dict]]:
    """
    Runs every (symbol, strategy_id) pair, fetching each symbol's history
    from yfinance exactly ONCE and reusing it across every strategy_id for
    that symbol — not once per pair. This isn't just an efficiency
    optimization: hitting Yahoo's unofficial endpoint for the same ticker
    N times in a row (once this ran 6+ strategy_ids against 3 symbols,
    18 fetches for what should be 3) was observed live to make later
    fetches for an already-fetched symbol come back thin enough that a
    strategy needing 200+ days of history stopped firing on data that, in
    isolation, has plenty — a real, reproduced degradation, not a
    one-off. One symbol's data failing (or a strategy_id being unknown) is
    recorded as an error and skipped rather than aborting the whole suite —
    matches autonomous_trader.scan_for_entries()'s "one failure doesn't
    stop the rest of the scan" behavior. Returns (results, errors).
    """
    results: list[BacktestResult] = []
    errors: list[dict] = []

    strategies = []
    for strategy_id in strategy_ids:
        try:
            strategies.append(get_strategy(strategy_id))
        except UnknownStrategyError as exc:
            logger.warning("backtest_unknown_strategy", strategy_id=strategy_id, error=str(exc))
            errors.append({"symbol": None, "strategy_id": strategy_id, "error": str(exc)})

    for symbol in symbols:
        try:
            raw_bars = await fetch_daily_bars(symbol, start, end)
            if len(raw_bars) < MIN_BARS_REQUIRED:
                raise BacktestDataError(
                    f"Only {len(raw_bars)} bars available for {symbol} between {start} and {end}; "
                    f"need at least {MIN_BARS_REQUIRED} for any strategy to evaluate at all."
                )
            bars = [
                Bar(timestamp=b["timestamp"], open=b["open"], high=b["high"], low=b["low"], close=b["close"], volume=b["volume"])
                for b in raw_bars
            ]
        except BacktestDataError as exc:
            logger.warning("backtest_symbol_data_failed", symbol=symbol, error=str(exc))
            for strategy in strategies:
                errors.append({"symbol": symbol, "strategy_id": strategy.id, "error": str(exc)})
            continue

        for strategy in strategies:
            results.append(
                run_backtest(
                    bars, symbol, strategy,
                    risk_pct=risk_pct, reward_risk_ratio=reward_risk_ratio,
                    notional_per_trade_usd=notional_per_trade_usd, starting_capital=starting_capital,
                )
            )

    return results, errors


def summarize_suite(results: list[BacktestResult]) -> dict:
    """Combined stats across every (symbol, strategy) pair in a suite — the headline numbers for a backtest report."""
    total_trades = sum(r.total_trades for r in results)
    wins = sum(r.wins for r in results)
    losses = sum(r.losses for r in results)
    total_pnl = sum(r.ending_capital - r.starting_capital for r in results)
    signals_too_small = sum(r.signals_too_small_for_notional for r in results)
    return {
        "pairs_run": len(results),
        "total_trades": total_trades,
        "wins": wins,
        "losses": losses,
        "win_rate": (wins / total_trades) if total_trades else 0.0,
        "total_pnl_usd": total_pnl,
        # Signals that fired but notional_per_trade_usd didn't cover even
        # 1 share — a high count here (relative to total_trades) means the
        # notional is mismatched to these symbols' prices, not that the
        # strategies aren't working. See BacktestResult's own field doc.
        "signals_too_small_for_notional": signals_too_small,
        "best_pair": max(results, key=lambda r: r.total_return_pct).to_dict() if results else None,
        "worst_pair": min(results, key=lambda r: r.total_return_pct).to_dict() if results else None,
    }


__all__ = ["MIN_BARS_REQUIRED", "run_backtest_for_symbol", "run_backtest_suite", "summarize_suite"]
