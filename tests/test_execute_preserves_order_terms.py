"""
Regression test for a real bug: Executor.execute_order() used to rebuild
the broker order spec with hardcoded order_type="MARKET" and
asset_type="ETF", discarding whatever was actually previewed and
risk-approved. A LIMIT order's price protection — the entire reason a
human approved it — was silently dropped at the one moment it reached the
broker. Same for STOP orders. See OrderRecord.order_type/limit_price/
stop_price (src/execution/executor.py), populated at preview time and now
actually used at execute time.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from src.execution.executor import Executor
from src.models.orders import AssetType, Instruction, OrderType, TradeProposal


class RecordingBroker:
    """A broker double that records the exact order_spec each call receives, for asserting on it."""

    def __init__(self, quote_price: str):
        self.quote_price = quote_price
        self.submitted_specs = []
        self.previewed_specs = []

    async def get_quote(self, symbol):
        return {
            "symbol": symbol,
            "bid": self.quote_price,
            "ask": self.quote_price,
            "last": self.quote_price,
            "quote_time": datetime.now(UTC).isoformat(),
            "mode": "TEST",
        }

    async def preview_order(self, profile, order_spec):
        self.previewed_specs.append(order_spec)
        return {"estimatedCommission": 0, "estimatedTotalInvestment": 100, "status": "OK"}

    async def submit_order(self, profile, order_spec):
        self.submitted_specs.append(order_spec)
        return {"orderId": "recorded-exec-1", "status": "ACCEPTED"}

    async def get_order_status(self, profile, order_id): raise NotImplementedError
    async def list_accounts(self): raise NotImplementedError
    async def get_positions(self, profile): raise NotImplementedError
    async def get_balances(self, profile): raise NotImplementedError
    async def get_price_history(self, symbol, bar_interval, lookback_days): raise NotImplementedError


async def _preview_and_execute(session, broker, proposal: TradeProposal):
    executor = Executor(session=session, broker=broker)
    preview = await executor.preview_order(proposal)
    assert preview.risk_verdict == "APPROVED", preview.risk_details

    receipt = await executor.execute_order(
        decision_id=proposal.decision_id,
        preview_id=preview.preview_id,
        approved_by="test-approver",
        approved_at=datetime.utcnow(),
        attestation="reviewed",
        idempotency_key=f"idem-{proposal.decision_id}",
    )
    return preview, receipt


@pytest.mark.asyncio
async def test_limit_order_reaches_the_broker_as_limit_with_its_price(test_db_engine_and_session):
    """The exact bug: a previewed LIMIT order must not become a MARKET order at execute time."""
    _, session = test_db_engine_and_session
    broker = RecordingBroker(quote_price="123.45")
    proposal = TradeProposal(
        decision_id="order-terms-limit-001",
        account="primary",
        symbol="QQQ",
        asset_type=AssetType.ETF,
        instruction=Instruction.BUY,
        quantity=10,
        order_type=OrderType.LIMIT,
        limit_price=Decimal("123.45"),
    )

    await _preview_and_execute(session, broker, proposal)

    assert len(broker.submitted_specs) == 1
    submitted = broker.submitted_specs[0]
    assert submitted["orderType"] == "LIMIT", "the broker must see LIMIT, not a silently-downgraded MARKET order"
    assert submitted["limitPrice"] == "123.45"


@pytest.mark.asyncio
async def test_stop_order_reaches_the_broker_as_stop_with_its_price(test_db_engine_and_session):
    _, session = test_db_engine_and_session
    broker = RecordingBroker(quote_price="50.00")
    proposal = TradeProposal(
        decision_id="order-terms-stop-001",
        account="primary",
        symbol="IWM",
        asset_type=AssetType.ETF,
        instruction=Instruction.BUY,
        quantity=5,
        order_type=OrderType.STOP,
        stop_price=Decimal("48.00"),
    )

    await _preview_and_execute(session, broker, proposal)

    submitted = broker.submitted_specs[0]
    assert submitted["orderType"] == "STOP"
    assert submitted["stopPrice"] == "48.00"
    assert "limitPrice" not in submitted


@pytest.mark.asyncio
async def test_market_order_still_reaches_the_broker_as_market(test_db_engine_and_session):
    """The common case must keep working unchanged."""
    _, session = test_db_engine_and_session
    broker = RecordingBroker(quote_price="200.00")
    proposal = TradeProposal(
        decision_id="order-terms-market-001",
        account="primary",
        symbol="SPY",
        asset_type=AssetType.ETF,
        instruction=Instruction.SELL,
        quantity=3,
        order_type=OrderType.MARKET,
    )

    await _preview_and_execute(session, broker, proposal)

    submitted = broker.submitted_specs[0]
    assert submitted["orderType"] == "MARKET"
    assert "limitPrice" not in submitted
    assert "stopPrice" not in submitted


@pytest.mark.asyncio
async def test_asset_type_is_preserved_not_hardcoded_to_etf(test_db_engine_and_session):
    _, session = test_db_engine_and_session
    broker = RecordingBroker(quote_price="99.00")
    proposal = TradeProposal(
        decision_id="order-terms-equity-001",
        account="primary",
        symbol="SPY",
        asset_type=AssetType.EQUITY,
        instruction=Instruction.BUY,
        quantity=1,
        order_type=OrderType.LIMIT,
        limit_price=Decimal("99.00"),
    )

    await _preview_and_execute(session, broker, proposal)

    assert broker.submitted_specs[0]["assetType"] == "EQUITY"
