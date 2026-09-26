"""
Actual Trade-TF HTTP client -> Engine ASGI app -> paper broker contract.

Uses Trade-TF's real, already-safety-audited ExecutionEngineClient (see
Trade-TF's own commit "Make ExecutionEngineClient speak Execution-Engine's
real protocol, and make autonomous execution paper-only by construction" —
26 unit tests plus a prior real end-to-end paper run) rather than the
differently-shaped client that shipped in Francois's 2026.09.25-rc1 zip.
That zip's version has no test coverage of its own and nothing in Trade-TF
actually calls it — see docs/reconciliation-002-007.md's own integration
notes for why it was left out of this repo's copy of Trade-TF. This test
proves the SAME thing either shape would (real HTTP wire compatibility
between the two real, running services) using the client that's actually
shipping.
"""
import sys
from pathlib import Path
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4
import httpx
import pytest

# The release archive places both projects side by side.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'Trade-TF'))
from trader_tf.adapters.execution.execution_engine_client import ApprovalMode, ExecutionEngineClient
from trader_tf.domain.order_intent import OrderIntent
from trader_tf.strategy_registry import StrategyContract, StrategyRegistry

@pytest.mark.asyncio
async def test_trade_tf_http_paper_contract(app_with_test_db, monkeypatch, test_settings):
    from src.execution import executor
    from src.risk import limits
    monkeypatch.setattr(executor, 'get_settings', lambda: test_settings)
    monkeypatch.setattr(limits, 'get_settings', lambda: test_settings)
    real_client = httpx.AsyncClient
    def local_client(*args, **kwargs):
        kwargs['transport'] = httpx.ASGITransport(app=app_with_test_db)
        return real_client(*args, **kwargs)
    monkeypatch.setattr(httpx, 'AsyncClient', local_client)
    key = str(uuid4())
    intent = OrderIntent(intent_id=key, strategy_id='contract-test', sleeve_element_id='equity',
        eligibility_id='approved-research', instrument='QQQ', side='BUY', quantity=Decimal('1'),
        idempotency_key=key, created_at=datetime.now(UTC), expires_at=datetime.now(UTC) + timedelta(minutes=10))

    # HUMAN mode (the default) previews only, against the real engine.
    human = ExecutionEngineClient('http://test', test_settings.api_key_admin, account='primary', asset_type='EQUITY')
    previewed = await human.submit_intent(intent)
    assert previewed.status == 'PREVIEWED' and previewed.accepted and previewed.preview_id

    # AUTONOMOUS_PAPER_ONLY against the SAME real engine, in real paper mode:
    # proves the engine's own health check truly reports PAPER end to end,
    # not a mocked/assumed value, and that the client only ever auto-executes
    # because of that real proof.
    auto_key = str(uuid4())
    auto_intent = OrderIntent(intent_id=auto_key, strategy_id='contract-test', sleeve_element_id='equity',
        eligibility_id='approved-research', instrument='QQQ', side='BUY', quantity=Decimal('1'),
        idempotency_key=auto_key, created_at=datetime.now(UTC), expires_at=datetime.now(UTC) + timedelta(minutes=10))
    client = ExecutionEngineClient('http://test', test_settings.api_key_admin, account='primary',
        asset_type='EQUITY', approval_mode=ApprovalMode.AUTONOMOUS_PAPER_ONLY)
    receipt = await client.submit_intent(auto_intent)
    repeated = await client.submit_intent(auto_intent)
    assert receipt == repeated and receipt.accepted and receipt.status == 'EXECUTED'
    [fill] = await client.get_fills(auto_key)
    assert fill.quantity == Decimal('1') and fill.instrument == 'QQQ'


def test_registry_survives_restart_and_rejects_conflicting_version(tmp_path):
    path = str(tmp_path / 'registry.db')
    registry = StrategyRegistry(path)
    contract = StrategyContract(strategy_id='dip2', version='3', decision_policy={'rule': 'twoSessionReturn'})
    digest = registry.register(contract)
    restarted = StrategyRegistry(path)
    assert restarted.get('dip2', '3').contract_hash == digest
    with pytest.raises(ValueError, match='version conflict'):
        restarted.register(contract.model_copy(update={'decision_policy': {'rule': 'changed'}}))
