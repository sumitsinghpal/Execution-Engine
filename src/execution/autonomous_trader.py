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
3. _build_broker(settings, mandate) decides whether this loop can reach a
   real broker at all, and the answer is almost always no. For an
   AUTONOMOUS_PAPER mandate (the default), it behaves exactly as before:
   real Schwab market data when configured, wrapped in
   SchwabDataPaperBroker so every preview/submission still simulates;
   anything it can't safely identify as Schwab (a BrokerRouter, a raw
   RobinhoodHostBridgeAdapter, anything else) falls back to fully-synthetic
   PaperBrokerAdapter — fail closed, never the live object itself, even
   though that means losing real market data in that configuration. Only
   an AUTONOMOUS_LIVE mandate can reach a real broker, and only Robinhood,
   and only when EVERY ONE of these is independently true at once:
   mandate.mode == "AUTONOMOUS_LIVE", mandate.live_execution_authorized,
   mandate.mandate_hmac verifies against the server's own admin key (see
   SiloRuntime.active_mandate — a tampered or unverifiable mandate is
   treated as inactive, not an error), settings.
   autonomous_live_robinhood_enabled, settings.robinhood_live_trading_enabled,
   AND the account profile mandate.account_alias resolves to is itself
   live_enabled. Merely arming a Silo mandate, or configuring Robinhood
   credentials, or setting either deployment switch alone, changes
   nothing — every one of these is a separate, independent gate.
4. scan_for_entries() opens NOTHING unless a human has explicitly armed
   a Silo mandate (see src/execution/silo_runtime.py) — which strategies
   get to trade, and how much, are never silently inherited from a
   static default. manage_open_positions() is deliberately NOT gated by
   this: an already-open position keeps being checked against its
   standardized stop-loss/take-profit and can still be closed even after
   the Silo is disarmed or its mandate expires — an open position never
   goes unmonitored just because authorization to open NEW ones lapsed.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from decimal import Decimal
from typing import Callable

from sqlmodel import Session

from src.agentic.llm_narrator import narrate_entry, narrate_exit
from src.accounts.profiles import BrokerName
from src.brokers.base import BrokerAdapter, LiveTradingDisabledError
from src.brokers.factory import build_broker_adapter
from src.brokers.paper import PaperBrokerAdapter
from src.brokers.robinhood.adapter import RobinhoodHostBridgeAdapter
from src.brokers.router import BrokerRouter
from src.brokers.schwab.adapter import SchwabBrokerAdapter
from src.brokers.schwab_data_paper import SchwabDataPaperBroker
from src.config import Settings
from src.execution.autonomous_positions import AutonomousPositionService, AutonomousPositionStatus
from src.execution.silo_runtime import ProbabilityBands, SiloMandate, SiloRuntime
from src.execution.drawdown_guard import DrawdownGuard, _extract_equity
from src.execution.silo_evidence import compute_evidence, concentration_pct_of_equity
from src.execution.llm_evidence import CandidateContext, compute_llm_evidence
from src.execution.executor import Executor
from src.execution.risk_reward import compute_standardized_exit, size_position
from src.logging_config import get_logger
from src.models.orders import AssetType, Instruction, OrderType, TradeProposal
from src.notifications.webhook import notify
from src.strategy import engine as strategy_engine

logger = get_logger(__name__)


def _build_broker(settings: Settings, mandate: SiloMandate) -> BrokerAdapter:
    """
    Resolve the autonomous broker from the armed Silo mandate. See module
    docstring point 3 for the full independent-switch chain AUTONOMOUS_LIVE
    requires; every path through this function that isn't that fully-
    validated branch ends at plain PaperBrokerAdapter or a Schwab adapter
    wrapped in SchwabDataPaperBroker (real data, simulated fills) — never a
    bare live-capable object. Raises LiveTradingDisabledError (never
    silently falls back) when mode == "AUTONOMOUS_LIVE" but any condition
    is unmet, so a misconfigured live mandate fails loudly rather than
    quietly trading paper.
    """
    if mandate.mode == "AUTONOMOUS_LIVE":
        if mandate.broker != "robinhood":
            raise LiveTradingDisabledError("AUTONOMOUS_LIVE currently supports Robinhood only")
        if not settings.autonomous_live_robinhood_enabled:
            raise LiveTradingDisabledError("AUTONOMOUS_LIVE_ROBINHOOD_ENABLED is false")
        if not settings.robinhood_live_trading_enabled:
            raise LiveTradingDisabledError("ROBINHOOD_LIVE_TRADING_ENABLED is false")
        profile = settings.get_account_profile(mandate.account_alias)
        if profile.broker != BrokerName.ROBINHOOD or not profile.live_enabled:
            raise LiveTradingDisabledError("Armed Silo account alias is not a live-enabled Robinhood profile")
        return RobinhoodHostBridgeAdapter(settings.robinhood_bridge_url, settings.robinhood_bridge_token, live_enabled=True)

    # AUTONOMOUS_PAPER — identical fail-closed shape this function has
    # always used: real Schwab market data wrapped in SchwabDataPaperBroker
    # when safely extractable, plain PaperBrokerAdapter for everything else
    # this function doesn't explicitly know how to make safe (a
    # BrokerRouter with no Schwab inside, a bare RobinhoodHostBridgeAdapter,
    # anything else) — never the router or a live-capable adapter directly.
    broker = build_broker_adapter(settings)
    if isinstance(broker, PaperBrokerAdapter):
        return broker
    schwab = broker if isinstance(broker, SchwabBrokerAdapter) else None
    if schwab is None and isinstance(broker, BrokerRouter):
        candidate = broker.adapters.get(BrokerName.SCHWAB)
        if isinstance(candidate, SchwabBrokerAdapter):
            schwab = candidate
    if schwab is not None:
        return SchwabDataPaperBroker(schwab)
    return PaperBrokerAdapter()


def _synthetic_paper_mandate() -> SiloMandate:
    """
    A minimal, always-active AUTONOMOUS_PAPER mandate used ONLY to resolve
    a broker for manage_open_positions() when no real Silo mandate is
    currently armed — see that function's docstring for why exit
    management must never depend on an active mandate the way entries do.
    Never persisted, never armed, never seen by evaluate_candidate().
    """
    return SiloMandate(
        silo_id="synthetic-exit-management",
        armed_by="system",
        trades_per_minute=1,
        max_per_trade_usd=Decimal("1"),
        max_concentration_pct=Decimal("100"),
        decision_bands=ProbabilityBands(execute_above=1.0, resize_above=1.0),
    )


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
    runtime = SiloRuntime()
    mandate = runtime.active_mandate(admin_key=settings.api_key_admin) or _synthetic_paper_mandate()
    broker = _build_broker(settings, mandate)
    risk_mode = "standard" if mandate.mode == "AUTONOMOUS_LIVE" else "silo_paper"
    executor = Executor(session=session, broker=broker, risk_mode=risk_mode)
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
        # Deterministic, tied to the specific position being closed rather
        # than a fresh uuid4() every cycle: a crash between this order
        # actually submitting and close_position() recording it locally
        # means the position is STILL OPEN on the next cycle, and this
        # code re-runs for it — with the same decision_id, that retry hits
        # Executor's existing idempotent-duplicate handling (cached preview
        # / cached receipt) and catches up local state, instead of a fresh
        # random key bypassing submission_guard.py entirely and risking a
        # second real sell order.
        decision_id = f"auto-exit-{position.entry_decision_id}"

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
    Runs every strategy in the armed Silo mandate's authorized_strategy_ids
    against every symbol in its authorized_symbols (see
    src/execution/silo_runtime.py); for each fresh entry signal (skipping
    any (symbol, strategy) pair already holding an open position — no
    pyramiding), sizes it using the mandate's own max_per_trade_usd,
    computes the standardized stop/target, and submits it through the
    normal preview -> execute gate. Returns how many positions were
    opened.

    Opens nothing at all — not an error, just 0 — when there is no
    active mandate: this is the "ready to execute" gate the rest of the
    autonomous safety machinery (the kill switch, RiskChecker for
    AUTONOMOUS_LIVE) sits on top of, not underneath. A human has to have
    explicitly armed a Silo mandate before this function does anything.
    """
    runtime = SiloRuntime()
    mandate = runtime.active_mandate(admin_key=settings.api_key_admin)
    if mandate is None:
        return 0

    broker = _build_broker(settings, mandate)
    risk_mode = "standard" if mandate.mode == "AUTONOMOUS_LIVE" else "silo_paper"
    executor = Executor(session=session, broker=broker, risk_mode=risk_mode)
    service = AutonomousPositionService(session)
    opened = 0
    # Silo owns sizing policy. Using the mandate max as the paper sizing budget
    # removes the legacy hard-coded per-trade notional from autonomous mode.
    notional_per_trade_usd = mandate.max_per_trade_usd

    # Concentration and drawdown are both percentages of *account equity*,
    # captured once per scan pass rather than once per candidate -- equity
    # does not meaningfully change between candidates evaluated
    # microseconds apart, and DrawdownGuard already treats "once per day"
    # as the right baseline granularity. If equity can't be read at all,
    # the Silo mandate's own concentration/drawdown authority can't be
    # verified right now, so this whole pass defers rather than silently
    # trading without checking them (see silo_evidence.py).
    try:
        profile = settings.get_account_profile(settings.autonomous_account)
        balances = await broker.get_balances(profile)
        account_equity = _extract_equity(balances)
        drawdown_report = await DrawdownGuard(session, broker).check_drawdown(settings.autonomous_account)
        current_drawdown_pct = Decimal(str(drawdown_report.drawdown_pct * 100))
    except Exception as exc:
        logger.warning("autonomous_silo_concentration_drawdown_unavailable", error=str(exc))
        return 0
    if account_equity is None:
        logger.warning("autonomous_silo_concentration_unavailable_no_equity")
        return 0

    for symbol in mandate.authorized_symbols:
        for strategy_id in mandate.authorized_strategy_ids:
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

            estimated_notional = Decimal(str(detail.entry_price)) * Decimal(quantity)
            candidate_concentration_pct = concentration_pct_of_equity(estimated_notional, account_equity)
            try:
                quote = await broker.get_quote(symbol)
                evidence = compute_evidence(quote, settings.max_quote_age_seconds)
            except Exception as exc:
                logger.warning("autonomous_silo_evidence_quote_failed", symbol=symbol, strategy_id=strategy_id, error=str(exc))
                evidence = {}
            try:
                llm_evidence = await compute_llm_evidence(
                    CandidateContext(
                        symbol=symbol, strategy_id=strategy_id, entry_price=detail.entry_price,
                        stop_loss_price=exit_levels.stop_loss_price, take_profit_price=exit_levels.take_profit_price,
                        rationale=detail.rationale,
                    ),
                    settings,
                )
                evidence.update(llm_evidence)
            except Exception as exc:
                logger.warning("autonomous_silo_llm_evidence_failed", symbol=symbol, strategy_id=strategy_id, error=str(exc))
            silo_decision = runtime.evaluate_candidate(
                symbol=symbol,
                strategy_id=strategy_id,
                estimated_notional_usd=estimated_notional,
                concentration_pct=candidate_concentration_pct,
                drawdown_pct=current_drawdown_pct,
                evidence=evidence,
                admin_key=settings.api_key_admin,
            )
            if not silo_decision.allowed:
                logger.info(
                    "autonomous_entry_deferred_by_silo",
                    symbol=symbol,
                    strategy_id=strategy_id,
                    action=silo_decision.action,
                    probability_score=silo_decision.probability_score,
                    reason=silo_decision.reason,
                )
                continue

            # Deterministic, not a fresh uuid4() every cycle: one attempt
            # per (symbol, strategy_id) per calendar day. A crash between
            # this order actually submitting and open_position() recording
            # it locally means has_open_position() above still says False
            # on the next cycle, and this candidate is re-evaluated fresh —
            # with the same decision_id, that retry hits Executor's
            # existing idempotent-duplicate handling (cached preview /
            # cached receipt) if terms match, or is cleanly refused if they
            # don't, instead of a fresh random key bypassing
            # submission_guard.py entirely and risking a second real order.
            decision_id = f"auto-entry-{symbol}-{strategy_id}-{date.today().isoformat()}"
            try:
                # AUTONOMOUS_LIVE routes to the mandate's OWN authorized
                # account_alias (its live Robinhood account), never the
                # static paper default -- a mismatch here would mean
                # _build_broker() correctly resolved a live adapter while
                # the order itself still targeted the paper account, or
                # vice versa.
                account = mandate.account_alias if mandate.mode == "AUTONOMOUS_LIVE" else settings.autonomous_account
                mode_label = "LIVE Robinhood order" if mandate.mode == "AUTONOMOUS_LIVE" else "PAPER only"
                proposal = TradeProposal(
                    decision_id=decision_id,
                    agent_id=settings.autonomous_agent_id,
                    account=account,
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
                        f"1:{settings.autonomous_reward_risk_ratio} R:R, {mode_label} — no human review, "
                        f"Silo mandate {mandate.silo_id}."
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
            runtime.record_trade()
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
