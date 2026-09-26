"""Actual Trade-TF HTTP client -> Engine ASGI app -> paper broker contract."""
import sys
from pathlib import Path
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4
import httpx
import pytest

# The release archive places both projects side by side.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'Trade-TF'))
from trader_tf.adapters.execution.execution_engine_client import ExecutionEngineClient
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
        idempotency_key=key, created_at=datetime.now(UTC), account_alias='primary',
        asset_type='ETF', order_type='MARKET')
    client = ExecutionEngineClient('http://test', test_settings.api_key_admin)
    with pytest.raises(ValueError, match='Explicit approval'):
        await client.submit_intent(intent)
    preview = await client.preview_intent(intent)
    approval = dict(preview_id=preview['preview_id'], approved_by='test-human',
        approved_at=datetime.now(UTC).isoformat(), attestation='Approve this paper preview', idempotency_key=key)
    receipt = await client.submit_intent(intent, approval)
    repeated = await client.submit_intent(intent, approval)
    assert receipt == repeated and receipt.accepted
    status = await client.get_status(intent.intent_id)
    assert status['status'] == 'FILLED' and status['filled_quantity'] == 1
    assert status['execution_id'] == receipt.broker_order_id


def test_registry_survives_restart_and_rejects_conflicting_version(tmp_path):
    path = str(tmp_path / 'registry.db')
    registry = StrategyRegistry(path)
    contract = StrategyContract(strategy_id='dip2', version='3', decision_policy={'rule': 'twoSessionReturn'})
    digest = registry.register(contract)
    restarted = StrategyRegistry(path)
    assert restarted.get('dip2', '3').contract_hash == digest
    with pytest.raises(ValueError, match='version conflict'):
        restarted.register(contract.model_copy(update={'decision_policy': {'rule': 'changed'}}))
