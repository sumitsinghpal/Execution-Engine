"""
Tests for cross-agent symbol coordination: several agents can each be
individually within their own limits and still, combined, over-concentrate
an account in one symbol — this is the check that catches that.

Note on account/symbol choice: like other files in this shared-DB test
suite, each test uses a symbol unlikely to collide with committed orders
from other test files (this repo's default allowlist doesn't include the
made-up symbols used here, but SymbolCoordinationGuard doesn't consult the
allowlist — it only sums OrderRecord history — so that's fine for a
directly-constructed OrderRecord in a unit test).
"""

from datetime import datetime

import pytest

from src.execution.executor import Executor, OrderRecord
from src.execution.symbol_coordination import SymbolCoordinationGuard
from src.models.orders import AssetType, Instruction, OrderStatus, OrderType, TradeProposal


def _make_order(session, agent_id, account, symbol, notional, status=OrderStatus.SUBMITTED.value):
    order = OrderRecord(
        decision_id=f"coord-test-{agent_id}-{symbol}-{datetime.utcnow().timestamp()}",
        agent_id=agent_id,
        account=account,
        symbol=symbol,
        quantity=1,
        instruction="BUY",
        status=status,
        payload_checksum="test-checksum",
        estimated_notional_usd=str(notional),
    )
    session.add(order)
    session.commit()
    return order


def test_uncapped_symbol_is_never_breached(test_db_engine_and_session):
    """With no combined cap configured (the default), no combination of orders breaches it."""
    _, session = test_db_engine_and_session
    _make_order(session, "agent-a", "primary", "SCOORD1", 999_999)

    guard = SymbolCoordinationGuard(session=session)
    report = guard.check("primary", "SCOORD1", proposed_notional_usd=1)

    assert report.cap_usd is None
    assert report.breached is False


def test_sums_committed_notional_across_multiple_agents(test_db_engine_and_session):
    """The whole point: this sums ALL agents' committed orders for the symbol, not just one."""
    _, session = test_db_engine_and_session
    _make_order(session, "agent-a", "primary", "SCOORD2", 4_000)
    _make_order(session, "agent-b", "primary", "SCOORD2", 3_000)

    guard = SymbolCoordinationGuard(session=session)
    report = guard.check("primary", "SCOORD2", proposed_notional_usd=0)

    assert report.committed_notional_usd == 7_000


def test_ignores_orders_for_a_different_account_or_symbol(test_db_engine_and_session):
    _, session = test_db_engine_and_session
    _make_order(session, "agent-a", "primary", "SCOORD3", 4_000)
    _make_order(session, "agent-a", "retirement", "SCOORD3", 5_000)  # different account
    _make_order(session, "agent-a", "primary", "SCOORD3B", 6_000)  # different symbol

    guard = SymbolCoordinationGuard(session=session)
    report = guard.check("primary", "SCOORD3", proposed_notional_usd=0)

    assert report.committed_notional_usd == 4_000


def test_ignores_orders_that_never_reached_the_broker(test_db_engine_and_session):
    _, session = test_db_engine_and_session
    _make_order(session, "agent-a", "primary", "SCOORD4", 5_000, status=OrderStatus.PREVIEWED.value)
    _make_order(session, "agent-a", "primary", "SCOORD4", 2_000, status=OrderStatus.REJECTED.value)
    _make_order(session, "agent-a", "primary", "SCOORD4", 1_000, status=OrderStatus.SUBMITTED.value)

    guard = SymbolCoordinationGuard(session=session)
    report = guard.check("primary", "SCOORD4", proposed_notional_usd=0)

    assert report.committed_notional_usd == 1_000


def test_combined_total_over_cap_is_breached(test_db_engine_and_session):
    """Each agent is individually well under a hypothetical per-agent limit; combined, they breach the fleet cap."""
    _, session = test_db_engine_and_session
    _make_order(session, "agent-a", "primary", "SCOORD5", 6_000)
    _make_order(session, "agent-b", "primary", "SCOORD5", 6_000)

    guard = SymbolCoordinationGuard(session=session)
    guard.settings.max_combined_symbol_notional_usd = 10_000

    report = guard.check("primary", "SCOORD5", proposed_notional_usd=0)

    assert report.combined_notional_usd == 12_000
    assert report.breached is True


def test_proposed_notional_is_included_in_the_combined_total(test_db_engine_and_session):
    """The check is forward-looking: it must include the order being proposed, not just what's already committed."""
    _, session = test_db_engine_and_session
    _make_order(session, "agent-a", "primary", "SCOORD6", 8_000)

    guard = SymbolCoordinationGuard(session=session)
    guard.settings.max_combined_symbol_notional_usd = 10_000

    under_report = guard.check("primary", "SCOORD6", proposed_notional_usd=1_000)
    assert under_report.breached is False

    over_report = guard.check("primary", "SCOORD6", proposed_notional_usd=3_000)
    assert over_report.breached is True


@pytest.mark.asyncio
async def test_executor_preview_rejects_when_combined_symbol_exposure_would_breach_cap(test_db_engine_and_session):
    """
    End-to-end through Executor.preview_order(), not just the guard directly:
    two different agents, each individually well within every per-agent and
    fleet-wide limit, still get the second one rejected once their combined
    committed capital in the same symbol would exceed the coordination cap.
    """
    _, session = test_db_engine_and_session

    # Agent A: a real order, previewed AND executed — only executed orders
    # count as "committed" capital (see COMMITTED_ORDER_STATUSES).
    executor_a = Executor(session=session)
    proposal_a = TradeProposal(
        decision_id="coord-exec-agent-a-001",
        agent_id="coord-exec-agent-a",
        account="primary",
        symbol="QQQ",
        asset_type=AssetType.ETF,
        instruction=Instruction.BUY,
        quantity=1,
        order_type=OrderType.MARKET,
    )
    preview_a = await executor_a.preview_order(proposal_a)
    assert preview_a.risk_verdict == "APPROVED"
    await executor_a.execute_order(
        decision_id=proposal_a.decision_id,
        preview_id=preview_a.preview_id,
        approved_by="test",
        approved_at=datetime.utcnow(),
        attestation="ok",
        idempotency_key="coord-exec-agent-a-idem",
    )

    # Agent B: proposes an equal-sized order for the SAME symbol/account.
    # A combined cap set to exactly agent A's already-committed notional
    # means any further order for this symbol must breach it.
    executor_b = Executor(session=session)
    executor_b.settings.max_combined_symbol_notional_usd = preview_a.estimated_cost

    proposal_b = TradeProposal(
        decision_id="coord-exec-agent-b-001",
        agent_id="coord-exec-agent-b",
        account="primary",
        symbol="QQQ",
        asset_type=AssetType.ETF,
        instruction=Instruction.BUY,
        quantity=1,
        order_type=OrderType.MARKET,
    )
    preview_b = await executor_b.preview_order(proposal_b)

    assert preview_b.risk_verdict == "REJECTED"
    assert preview_b.risk_details["checks"]["combined_symbol_exposure_ok"] is False
