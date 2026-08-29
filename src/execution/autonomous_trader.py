"""
Fully autonomous trading — the "auto trade, no human click" piece. Runs as
a background asyncio task started at FastAPI startup (see src/api/server.py),
same shape as src/execution/strategy_scanner.py, but where the scanner only
ever writes a row for a human to review, this loop actually calls
Executor.preview_order() and Executor.execute_order() itself.

Three things make that safe to ship:

1. Every buy/sell/size/exit decision is made by src/strategy/catalog.py's
   fixed technical rules (Golden Cross, Turtle 20-Day Breakout, RSI(2)
   Pullback by default — see settings.autonomous_strategy_ids) plus
   src/execution/risk_reward.py's standardized stop/target. There is no
   discretion, no LLM judgment call, nothing that could FOMO into a chase
   or freeze on an exit — src/agentic/llm_narrator.py is consulted only
   AFTER an order has already been submitted, purely to write the log
   entry explaining it.
2. The order still runs through the exact same preview -> risk checks ->
   execute gate as a human-submitted order — allowlists, notional caps,
   stale-quote protection, drawdown guard, all of it. The one thing
   removed is waiting for a human's approval click; approved_by is this
   agent's own id instead of an operator's. The kill switch (fleet-wide OR
   this agent's own scope — see settings.autonomous_agent_id) still halts
   it exactly like any other agent, including mid-position: a halt just
   stops new entries and stop/target management from firing, it doesn't
   touch what's already been submitted to the broker.
3. _build_broker() below NEVER lets this loop preview or submit an order
   against a real broker — that stays fixed regardless of settings. What
   IS configurable is where its market data (quotes, price history) comes
   from: real Schwab when settings.execution_mode == "SCHWAB" and
   credentials are present (build_broker_adapter(settings) resolves this
   exactly like every other component does), else the synthetic paper
   generator. Either way, the object actually used for preview_order()/
   submit_order() is SchwabDataPaperBroker or plain PaperBrokerAdapter —
   both simulate every fill; see src/brokers/schwab_data_paper.py's
   docstring for exactly which methods are real vs. simulated. Making
   ORDERS (not just data) real would mean changing what this function
   returns, a deliberate code change, not a config flip.
4. scan_for_entries() opens NOTHING unless a human has explicitly armed
   a strategy set and a per-trade quantity for today (see
   src/execution/daily_plan.py) — which strategies get to trade, and how
   much, are never silently inherited from a static default. The
   strategies offered for arming are themselves ranked from real recent
   performance (src/execution/strategy_ranking.py), not hand-picked once
   and left there; see that module's docstring for how the ranking
   itself is computed and why it's a starting point for a human decision,
   not an autonomous decision of its own. manage_open_positions() is NOT
   gated by this — a position already open still gets managed/exited
   normally even after the plan is disarmed or expires.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Callable

from sqlmodel import Session

from src.agentic.llm_narrator import narrate_entry, narrate_exit
from src.brokers.base import BrokerAdapter
from src.brokers.factory import build_broker_adapter
from src.brokers.schwab.adapter import SchwabBrokerAdapter
from src.brokers.schwab_data_paper import SchwabDataPaperBroker
from src.config import Settings
from src.execution.autonomous_positions import AutonomousPositionService, AutonomousPositionStatus
from src.execution.daily_plan import DailyPlanService
from src.execution.executor import Executor
from src.execution.risk_reward import compute_standardized_exit, size_position
from src.logging_config import get_logger
from src.models.orders import AssetType, Instruction, OrderType, TradeProposal
from src.notifications.webhook import notify
from src.strategy import engine as strategy_engine

logger = get_logger(__name__)


def _build_broker(settings: Settings) -> BrokerAdapter:
    """
    Real Schwab market data when configured, wrapped so every order
    preview/submission still simulates — see module docstring, point 3,
    and src/brokers/schwab_data_paper.py. build_broker_adapter(settings)
    already resolves whether Schwab is actually usable (execution_mode,
    credentials, an account profile that names it); if what it returns
    isn't a SchwabBrokerAdapter, Schwab isn't configured and plain
    PaperBrokerAdapter (fully synthetic, unchanged) is correct as-is.
    """
    broker = build_broker_adapter(settings)
    if isinstance(broker, SchwabBrokerAdapter):
        return SchwabDataPaperBroker(broker)
    return broker


async def manage_open_positions(session: Session, settings: Settings) -> int:
    """
    Checks every OPEN autonomous position's live quote against its
    standardized stop-loss/take-profit; closes (submits a MARKET SELL for)
    any that were hit. Returns how many were closed. A closing order that
    fails risk checks or the broker call is logged and left OPEN to retry
    next cycle, except a broker-call failure after risk-approval, which is
    closed defensively (CLOSED_ERROR) rather than left silently retrying
    forever against a broker that may keep rejecting it.
    """
    broker = _build_broker(settings)
    executor = Executor(session=session, broker=broker)
    service = AutonomousPositionService(session)
    closed = 0

    for position in service.list_open():
        try:
            quote = await broker.get_quote(position.symbol)
            last = float(quote["last"])
        except Exception as exc:
            logger.warning("autonomous_exit_quote_failed", symbol=position.symbol, error=str(exc))
            continue

        hit_target = last >= position.take_profit_price
        hit_stop = last <= position.stop_loss_price
        if not (hit_target or hit_stop):
            continue

        exit_reason = "take-profit" if hit_target else "stop-loss"
        status = AutonomousPositionStatus.CLOSED_TARGET if hit_target else AutonomousPositionStatus.CLOSED_STOP
        decision_id = f"auto-exit-{uuid.uuid4()}"

        try:
            proposal = TradeProposal(
                decision_id=decision_id,
                agent_id=settings.autonomous_agent_id,
                account=position.account,
                symbol=position.symbol,
                asset_type=AssetType.EQUITY,
                instruction=Instruction.SELL,
                quantity=position.quantity,
                order_type=OrderType.MARKET,
                strategy_id=f"autonomous:{position.strategy_id}:exit",
            )
            preview = await executor.preview_order(proposal)
            if preview.risk_verdict != "APPROVED":
                logger.error("autonomous_exit_rejected_by_risk_checks", symbol=position.symbol, details=preview.risk_details)
                continue

            await executor.execute_order(
                decision_id=decision_id,
                preview_id=preview.preview_id,
                approved_by=settings.autonomous_agent_id,
                approved_at=datetime.utcnow(),
                attestation=f"Autonomous {exit_reason} exit — standardized rule, no human review.",
                idempotency_key=f"{decision_id}:auto-exit",
            )
        except Exception as exc:
            logger.error("autonomous_exit_order_failed", symbol=position.symbol, error=str(exc))
            service.close_position(
                position,
                status=AutonomousPositionStatus.CLOSED_ERROR,
                exit_decision_id=decision_id,
                exit_price=None,
                exit_rationale=f"Exit order failed to submit: {exc}",
            )
            await notify(settings, f":warning: Autonomous exit FAILED for {position.symbol} ({position.strategy_id}): {exc}")
            closed += 1
            continue

        pnl = (last - position.entry_price) * position.quantity
        rationale = await narrate_exit(
            settings, symbol=position.symbol, exit_reason=exit_reason,
            entry_price=position.entry_price, exit_price=last, pnl_usd=pnl,
        )
        service.close_position(position, status=status, exit_decision_id=decision_id, exit_price=last, exit_rationale=rationale)
        pnl_emoji = ":chart_with_upwards_trend:" if pnl >= 0 else ":chart_with_downwards_trend:"
        await notify(
            settings,
            f"{pnl_emoji} Closed {position.symbol} ({position.strategy_id}) on {exit_reason}: "
            f"entry {position.entry_price:.2f} → exit {last:.2f}, P/L {pnl:+.2f}",
        )
        closed += 1

    return closed


async def scan_for_entries(session: Session, settings: Settings) -> int:
    """
    Runs every strategy in today's armed plan (see
    src/execution/daily_plan.py) against every symbol in
    settings.autonomous_watchlist; for each fresh entry signal (skipping
    any (symbol, strategy) pair already holding an open position — no
    pyramiding), sizes it using the plan's own notional_per_trade_usd,
    computes the standardized stop/target, and submits it through the
    normal preview -> execute gate. Returns how many positions were
    opened.

    Opens nothing at all — not an error, just 0 — when there is no
    active plan: this is the "ready to execute" gate the rest of the
    autonomous safety machinery (the master enable setting, the kill
    switch) sits on top of, not underneath. A human has to have reviewed
    a strategy ranking and explicitly armed a strategy set + quantity
    for today before this function does anything.
    """
    plan = DailyPlanService(session).get_active_plan()
    if plan is None:
        return 0

    broker = _build_broker(settings)
    executor = Executor(session=session, broker=broker)
    service = AutonomousPositionService(session)
    opened = 0
    notional_per_trade_usd = Decimal(plan.notional_per_trade_usd)

    for symbol in settings.autonomous_watchlist:
        for strategy_id in plan.strategy_ids:
            if service.has_open_position(symbol, strategy_id):
                continue
            try:
                detail = await strategy_engine.scan(broker, symbol, strategy_id)
            except Exception as exc:
                logger.warning("autonomous_scan_failed", strategy_id=strategy_id, symbol=symbol, error=str(exc))
                continue
            if detail is None:
                continue

            exit_levels = compute_standardized_exit(
                detail.entry_price, risk_pct=settings.autonomous_risk_pct, reward_risk_ratio=settings.autonomous_reward_risk_ratio
            )
            quantity = size_position(notional_per_trade_usd, detail.entry_price)
            if quantity < 1:
                logger.info("autonomous_entry_skipped_too_small", symbol=symbol, strategy_id=strategy_id, entry_price=detail.entry_price)
                continue

            decision_id = f"auto-entry-{uuid.uuid4()}"
            try:
                proposal = TradeProposal(
                    decision_id=decision_id,
                    agent_id=settings.autonomous_agent_id,
                    account=settings.autonomous_account,
                    symbol=symbol,
                    asset_type=AssetType.EQUITY,
                    instruction=Instruction.BUY,
                    quantity=quantity,
                    order_type=OrderType.MARKET,
                    strategy_id=f"autonomous:{strategy_id}",
                    strategy_stop_loss_price=Decimal(str(round(exit_levels.stop_loss_price, 2))),
                    strategy_take_profit_price=Decimal(str(round(exit_levels.take_profit_price, 2))),
                )
                preview = await executor.preview_order(proposal)
                if preview.risk_verdict != "APPROVED":
                    logger.info("autonomous_entry_rejected_by_risk_checks", symbol=symbol, strategy_id=strategy_id, details=preview.risk_details)
                    continue

                await executor.execute_order(
                    decision_id=decision_id,
                    preview_id=preview.preview_id,
                    approved_by=settings.autonomous_agent_id,
                    approved_at=datetime.utcnow(),
                    attestation=(
                        f"Autonomous rule-based entry: {strategy_id}, standardized "
                        f"1:{settings.autonomous_reward_risk_ratio} R:R, PAPER only — no human review."
                    ),
                    idempotency_key=f"{decision_id}:auto-entry",
                )
            except Exception as exc:
                logger.error("autonomous_entry_order_failed", symbol=symbol, strategy_id=strategy_id, error=str(exc))
                continue

            strategy_name = strategy_engine.get_strategy(strategy_id).name
            rationale = await narrate_entry(
                settings,
                strategy_name=strategy_name,
                symbol=symbol,
                side="BUY",
                entry_price=detail.entry_price,
                stop_loss=exit_levels.stop_loss_price,
                take_profit=exit_levels.take_profit_price,
                rule_rationale=detail.rationale,
                reward_risk_ratio=settings.autonomous_reward_risk_ratio,
            )
            service.open_position(
                symbol=symbol,
                strategy_id=strategy_id,
                account=settings.autonomous_account,
                agent_id=settings.autonomous_agent_id,
                entry_decision_id=decision_id,
                quantity=quantity,
                entry_price=detail.entry_price,
                stop_loss_price=exit_levels.stop_loss_price,
                take_profit_price=exit_levels.take_profit_price,
                entry_rationale=rationale,
            )
            await notify(
                settings,
                f":large_green_circle: Opened {symbol} ({strategy_id}): {quantity} @ {detail.entry_price:.2f}, "
                f"stop {exit_levels.stop_loss_price:.2f} / target {exit_levels.take_profit_price:.2f}",
            )
            opened += 1

    return opened


async def autonomous_cycle_once(session: Session, settings: Settings) -> dict:
    """One full pass: manage existing positions first (so a stop/target hit closes before this cycle might otherwise re-enter), then scan for new ones."""
    closed = await manage_open_positions(session, settings)
    opened = await scan_for_entries(session, settings)
    return {"positions_closed": closed, "positions_opened": opened}


async def run_autonomous_loop(
    session_factory: Callable[[], Session],
    get_settings_fn: Callable[[], Settings],
    stop_event: asyncio.Event,
) -> None:
    """Runs autonomous_cycle_once on a timer until stop_event is set. Each iteration gets its own DB session."""
    logger.info("autonomous_trader_started")
    while not stop_event.is_set():
        settings = get_settings_fn()
        interval = max(settings.autonomous_scan_interval_sec, 5)

        if settings.autonomous_trading_enabled:
            session = session_factory()
            try:
                result = await autonomous_cycle_once(session, settings)
                if result["positions_opened"] or result["positions_closed"]:
                    logger.info("autonomous_cycle_complete", **result)
            except Exception as exc:
                logger.error("autonomous_loop_iteration_failed", error=str(exc))
            finally:
                session.close()

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    logger.info("autonomous_trader_stopped")


__all__ = ["autonomous_cycle_once", "manage_open_positions", "run_autonomous_loop", "scan_for_entries"]
