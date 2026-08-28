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
    slippage_bps: Decimal = Decimal("5"),
    commission_per_order_usd: Decimal = Decimal("0"),
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
        slippage_bps=slippage_bps,
        commission_per_order_usd=commission_per_order_usd,
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
    slippage_bps: Decimal = Decimal("5"),
    commission_per_order_usd: Decimal = Decimal("0"),
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
                    slippage_bps=slippage_bps, commission_per_order_usd=commission_per_order_usd,
                )
            )

    return results, errors


def combine_equity_curves(results: list[BacktestResult], starting_capital: float = 100_000.0) -> dict:
    """
    Merges every (symbol, strategy) pair's closed trades into ONE
    chronological equity curve sharing a single starting_capital — what
    running the whole suite together, out of one account, would actually
    have looked like. Each BacktestResult's own equity_curve is computed
    against ITS OWN independent starting_capital (see BacktestResult's
    field docs) and isn't summable as-is — the per-pair curves are 6
    separate $100k accounts, not $600k of combined exposure. This instead
    walks the underlying trades (sorted by exit_date, so entries and exits
    from every pair interleave in the order they actually closed) and
    accumulates their real pnl_usd against one shared base.

    A crude "portfolio" in the sense that every trade still gets its own
    full notional_per_trade_usd regardless of how many other positions are
    open elsewhere in the combined curve (no cross-strategy capital
    rationing) — but it's the first honest combined view: one capital
    base, one combined drawdown, one number for "did running everything
    together actually make money."
    """
    all_trades = [t for r in results for t in r.trades if t.exit_date is not None]
    all_trades.sort(key=lambda t: t.exit_date)

    first_dates = [r.equity_curve[0].date for r in results if r.equity_curve]
    first_date = min(first_dates) if first_dates else None

    equity = starting_capital
    peak = starting_capital
    max_drawdown_pct = 0.0
    curve = [{"date": first_date, "equity": equity}] if first_date else []

    for t in all_trades:
        equity += t.pnl_usd or 0.0
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown_pct = max(max_drawdown_pct, (peak - equity) / peak * 100)
        curve.append({"date": t.exit_date, "equity": equity})

    total_return_pct = (equity - starting_capital) / starting_capital * 100 if starting_capital else 0.0

    return {
        "starting_capital": starting_capital,
        "ending_capital": equity,
        "total_return_pct": total_return_pct,
        "max_drawdown_pct": max_drawdown_pct,
        "equity_curve": curve,
    }


def summarize_suite(results: list[BacktestResult], *, combined_starting_capital: float = 100_000.0) -> dict:
    """Combined stats across every (symbol, strategy) pair in a suite — the headline numbers for a backtest report."""
    total_trades = sum(r.total_trades for r in results)
    wins = sum(r.wins for r in results)
    losses = sum(r.losses for r in results)
    total_pnl = sum(r.ending_capital - r.starting_capital for r in results)
    signals_too_small = sum(r.signals_too_small_for_notional for r in results)
    # One benchmark_return_pct per symbol (every strategy on the same
    # symbol over the same window has the identical buy-and-hold figure —
    # averaging the raw per-pair list would over-weight whichever symbol
    # happened to have the most strategies fire on it).
    benchmarks_by_symbol = {r.symbol: r.benchmark_return_pct for r in results}
    avg_benchmark_return_pct = (sum(benchmarks_by_symbol.values()) / len(benchmarks_by_symbol)) if benchmarks_by_symbol else 0.0
    return {
        "pairs_run": len(results),
        "total_trades": total_trades,
        "wins": wins,
        "losses": losses,
        "win_rate": (wins / total_trades) if total_trades else 0.0,
        "total_pnl_usd": total_pnl,
        "avg_benchmark_return_pct": avg_benchmark_return_pct,
        # Signals that fired but notional_per_trade_usd didn't cover even
        # 1 share — a high count here (relative to total_trades) means the
        # notional is mismatched to these symbols' prices, not that the
        # strategies aren't working. See BacktestResult's own field doc.
        "signals_too_small_for_notional": signals_too_small,
        "best_pair": max(results, key=lambda r: r.total_return_pct).to_dict() if results else None,
        "worst_pair": min(results, key=lambda r: r.total_return_pct).to_dict() if results else None,
        # One combined account across every pair in the suite — see
        # combine_equity_curves() for why this isn't just summing the
        # per-pair curves.
        "combined_portfolio": combine_equity_curves(results, starting_capital=combined_starting_capital),
    }


__all__ = [
    "MIN_BARS_REQUIRED",
    "combine_equity_curves",
    "run_backtest_for_symbol",
    "run_backtest_suite",
    "summarize_suite",
]
