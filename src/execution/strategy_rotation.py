"""
Background loop that keeps an armed plan's strategy selection current
without the user re-arming — see src/execution/daily_plan.py's module
docstring for why this exists: arm once with a quantity, and the system
keeps picking the best-performing strategies on its own from then on,
the way an agent should, rather than asking for a fresh manual
confirmation every day.

Runs on a timer (default 24h) alongside the other background loops
(src/execution/autonomous_trader.py, strategy_scanner.py,
edge_tf_connector.py) started at FastAPI startup — see src/api/server.py.
Each tick: if there's an active plan, re-rank recent performance
(src/execution/strategy_ranking.py) using that plan's own
notional_per_trade_usd (so the ranking's simulated position sizing
matches what actually happens live) and rotate the plan's strategy list
to the new top picks. A no-op, cheap and network-free, whenever nothing
is currently armed — this loop always runs, it just has nothing to do
until something is.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Callable

from sqlmodel import Session

from src.config import Settings
from src.execution.daily_plan import DailyPlanService
from src.execution.strategy_ranking import DEFAULT_LOOKBACK_DAYS, DEFAULT_TOP_N, rank_strategies_by_recent_performance
from src.logging_config import get_logger

logger = get_logger(__name__)

# Once a day — matches the grain of the "recent performance" window
# itself (see strategy_ranking.py's DEFAULT_LOOKBACK_DAYS). Rotating
# more often wouldn't reflect any new information; the underlying
# ranking is a backtest over daily bars, which only advance once a day.
DEFAULT_ROTATION_INTERVAL_SEC = 24 * 60 * 60


async def rotate_once(session: Session, settings: Settings) -> bool:
    """Re-ranks and rotates the active plan's strategies to the current top picks, if a plan is active. Returns whether a rotation was actually attempted (not whether the picks changed — see DailyPlanService.rotate_strategies, which logs that distinction)."""
    plan = DailyPlanService(session).get_active_plan()
    if plan is None:
        return False

    try:
        ranking = await rank_strategies_by_recent_performance(
            settings.autonomous_watchlist,
            lookback_days=DEFAULT_LOOKBACK_DAYS,
            top_n=DEFAULT_TOP_N,
            notional_per_trade_usd=Decimal(plan.notional_per_trade_usd),
        )
    except Exception as exc:
        logger.error("strategy_rotation_ranking_failed", error=str(exc))
        return False

    if not ranking.top_picks:
        # Nothing fired in the window at all — safer to keep trading
        # whatever's currently armed than to rotate into an empty list,
        # which rotate_strategies() would refuse anyway.
        logger.warning("strategy_rotation_no_picks_kept_current_selection", current_strategy_ids=plan.strategy_ids)
        return False

    DailyPlanService(session).rotate_strategies(ranking.top_picks)
    return True


async def run_strategy_rotation_loop(
    session_factory: Callable[[], Session],
    get_settings_fn: Callable[[], Settings],
    stop_event: asyncio.Event,
    interval_sec: int = DEFAULT_ROTATION_INTERVAL_SEC,
) -> None:
    """Runs rotate_once on a timer until stop_event is set. Each iteration gets its own DB session, same pattern as run_autonomous_loop."""
    logger.info("strategy_rotation_loop_started")
    while not stop_event.is_set():
        settings = get_settings_fn()
        session = session_factory()
        try:
            await rotate_once(session, settings)
        except Exception as exc:
            logger.error("strategy_rotation_iteration_failed", error=str(exc))
        finally:
            session.close()

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=max(interval_sec, 60))
        except asyncio.TimeoutError:
            pass
    logger.info("strategy_rotation_loop_stopped")


__all__ = ["DEFAULT_ROTATION_INTERVAL_SEC", "rotate_once", "run_strategy_rotation_loop"]
