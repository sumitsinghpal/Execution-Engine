from datetime import datetime
from uuid import uuid4
from unittest.mock import AsyncMock
import pytest
from sqlmodel import Session, SQLModel, create_engine
from src.accounts.profiles import AccountProfile
from src.brokers.paper import PaperBrokerAdapter
from src.brokers.router import BrokerRouter
from src.brokers.robinhood.adapter import RobinhoodHostBridgeAdapter
from src.config import Settings
from src.execution.executor import Executor
from src.execution.submission_guard import SubmissionClaim
from src.models.orders import TradeProposal, ExecutionRequest

@pytest.fixture
def setup(monkeypatch):
    settings = Settings(_env_file=None, kill_switch_enabled=False)
    monkeypatch.setattr('src.execution.executor.get_settings', lambda: settings)
    monkeypatch.setattr('src.risk.limits.get_settings', lambda: settings)
    engine = create_engine('sqlite://')
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        executor = Executor(db, broker=PaperBrokerAdapter())
        proposal = TradeProposal(decision_id=str(uuid4()), account='primary', symbol='QQQ',
            asset_type='ETF', instruction='BUY', quantity=1, order_type='MARKET')
        yield executor, proposal, settings

async def execute(executor, preview, key='approval-1'):
    return await executor.execute_order(preview.decision_id, preview.preview_id, 'human',
        datetime.utcnow(), 'Approve exactly this preview', key)

@pytest.mark.asyncio
async def test_paper_roundtrip_and_retry(setup):
    executor, proposal, _ = setup
    executor.broker.submit_order = AsyncMock(wraps=executor.broker.submit_order)
    preview = await executor.preview_order(proposal)
    first = await execute(executor, preview)
    again = await execute(executor, preview)
    assert first == again
    assert first.status.value == 'FILLED'
    assert executor.broker.submit_order.call_count == 1
    with pytest.raises(ValueError, match='idempotency key conflict'):
        await execute(executor, preview, 'other-key')

@pytest.mark.asyncio
async def test_changed_preview_terms_rejected(setup):
    executor, proposal, _ = setup
    await executor.preview_order(proposal)
    with pytest.raises(ValueError, match='different order terms'):
        await executor.preview_order(proposal.model_copy(update={'quantity': 2}))

@pytest.mark.asyncio
async def test_changed_route_needs_new_approval(setup):
    executor, proposal, settings = setup
    preview = await executor.preview_order(proposal)
    settings.execution_mode = 'LIVE'
    with pytest.raises(ValueError, match='routing changed'):
        await execute(executor, preview)

@pytest.mark.asyncio
async def test_unknown_submission_survives_new_executor(setup):
    executor, proposal, _ = setup
    preview = await executor.preview_order(proposal)
    executor.broker.submit_order = AsyncMock(side_effect=TimeoutError())
    with pytest.raises(TimeoutError):
        await execute(executor, preview)
    restarted = Executor(executor.session, broker=executor.broker)
    with pytest.raises(ValueError, match='already entered execution'):
        await execute(restarted, preview)
    assert executor.broker.submit_order.call_count == 1
    assert executor.session.get(SubmissionClaim, proposal.decision_id)

@pytest.mark.asyncio
async def test_missing_broker_id_never_succeeds(setup):
    executor, proposal, _ = setup
    preview = await executor.preview_order(proposal)
    executor.broker.submit_order = AsyncMock(return_value={'status': 'WORKING'})
    with pytest.raises(ValueError, match='no order ID'):
        await execute(executor, preview)

@pytest.mark.asyncio
async def test_rejected_preview_cannot_execute(setup):
    executor, proposal, settings = setup
    settings.kill_switch_enabled = True
    preview = await executor.preview_order(proposal)
    settings.kill_switch_enabled = False
    with pytest.raises(ValueError, match='REJECTED'):
        await execute(executor, preview)

@pytest.mark.asyncio
async def test_router_isolates_accounts():
    paper, schwab = PaperBrokerAdapter(), PaperBrokerAdapter()
    paper.submit_order, schwab.submit_order = AsyncMock(), AsyncMock()
    router = BrokerRouter({'paper': paper, 'schwab': schwab}, 'schwab')
    await router.submit_order(AccountProfile(broker='schwab'), {})
    assert schwab.submit_order.call_count == 1
    assert paper.submit_order.call_count == 0
    with pytest.raises(Exception, match='No adapter'):
        await router.submit_order(AccountProfile(broker='robinhood'), {})

@pytest.mark.asyncio
async def test_robinhood_requires_capability_and_account():
    broker = RobinhoodHostBridgeAdapter('https://bridge.example', 'test', True)
    broker._call = AsyncMock(return_value={'SUBMIT': False})
    profile = AccountProfile(broker='robinhood', credential_profile='rh-main', live_enabled=True)
    with pytest.raises(Exception, match='lacks SUBMIT'):
        await broker.submit_order(profile, {})
    assert broker._call.call_count == 1

def test_mismatched_approval_preview_rejected():
    with pytest.raises(ValueError, match='must match'):
        ExecutionRequest(decision_id='a', preview_id='one', approval={
            'preview_id': 'two', 'approved_by': 'human', 'approved_at': datetime.utcnow(),
            'attestation': 'yes', 'idempotency_key': 'key'})

@pytest.mark.asyncio
async def test_schwab_submission_transport_never_retries_ambiguous_post():
    import httpx
    from src.brokers.schwab.adapter import SchwabBrokerAdapter
    oauth = AsyncMock()
    oauth.get_access_token.return_value = 'test-access-token'
    attempts = []
    def transport(request):
        attempts.append(request)
        raise httpx.ReadTimeout('response lost', request=request)
    broker = SchwabBrokerAdapter(oauth, transport=httpx.MockTransport(transport), retry_max_attempts=3)
    with pytest.raises(Exception):
        await broker._request('POST', '/accounts/test/orders', retry_allowed=False, json={})
    assert len(attempts) == 1

@pytest.mark.asyncio
async def test_schwab_cancel_reports_request_not_fill():
    from src.brokers.schwab.adapter import SchwabBrokerAdapter
    broker = SchwabBrokerAdapter(AsyncMock())
    broker._resolve_account_hash = AsyncMock(return_value='test-hash')
    broker._request = AsyncMock(return_value={})
    result = await broker.cancel_order(AccountProfile(broker='schwab', account_hash='test-hash'), '123')
    assert result == {'orderId': '123', 'cancellationRequested': True}
    broker._request.assert_awaited_once_with('DELETE', '/accounts/test-hash/orders/123', retry_allowed=False)
