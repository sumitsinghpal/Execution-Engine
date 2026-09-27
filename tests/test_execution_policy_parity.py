"""
Mode 1 (research_test) parity test — see docs/plan for the "Scope modes 1
& 2" work. The `simulated` field on OrderPreview/ExecutionReceipt/
OrderStatus_Model (src/models/orders.py) is meant to be the ONLY thing
that changes when the exact same order lifecycle runs against a paper
broker vs. a real-broker-shaped one — switching modes changes broker
authority, not the response contract. This runs the same preview -> approve
-> execute -> status lifecycle twice, once per broker, and asserts:
  1. `simulated` is True for the real PaperBrokerAdapter and False for a
     broker double shaped like a live adapter (not a PaperBrokerAdapter
     subclass) — see is_simulated_broker() in src/brokers/base.py.
  2. Every other field NAME on each response model is identical across
     both runs (the contract itself never changes shape based on mode).
"""

from datetime import UTC, datetime

import pytest

from src.brokers.paper import PaperBrokerAdapter
from src.config import Settings
from src.execution.executor import Executor
import src.execution.executor as executor_module
import src.risk.limits as risk_limits_module
from src.models.orders import AssetType, Instruction, OrderType, TradeProposal


def _settings(monkeypatch, **overrides):
    defaults = dict(_env_file=None, env="test", api_key_admin="change-me-in-prod")
    defaults.update(overrides)
    settings = Settings(**defaults)
    monkeypatch.setattr(executor_module, "get_settings", lambda: settings)
    monkeypatch.setattr(risk_limits_module, "get_settings", lambda: settings)
    return settings


class _FakeLiveShapedBroker:
    """
    A broker double that is NOT a PaperBrokerAdapter subclass — standing in
    for a real (Schwab/Robinhood-shaped) adapter. Reports "SUBMITTED"
    rather than paper's instant "FILLED", same as a real broker would
    before a fill is later discovered via reconciliation.
    """

    def __init__(self, price: float = 100.0):
        self.price = price
        self.submitted_specs = []

    async def get_quote(self, symbol):
        return {"symbol": symbol, "bid": self.price, "ask": self.price, "last": self.price,
                "quote_time": datetime.now(UTC).isoformat(), "mode": "TEST"}

    async def preview_order(self, profile, order_spec):
        return {"estimatedCommission": 0, "estimatedTotalInvestment": self.price * order_spec.get("quantity", 0), "status": "OK"}

    async def submit_order(self, profile, order_spec):
        self.submitted_specs.append(order_spec)
        return {"orderId": f"live-{len(self.submitted_specs)}", "status": "SUBMITTED"}

    async def get_order_status(self, profile, order_id): raise NotImplementedError
    async def list_accounts(self): raise NotImplementedError
    async def get_positions(self, profile): raise NotImplementedError
    async def get_balances(self, profile): raise NotImplementedError
    async def get_price_history(self, symbol, bar_interval, lookback_days): raise NotImplementedError


def _proposal(decision_id):
    return TradeProposal(
        decision_id=decision_id, agent_id="default", account="primary", symbol="SPY",
        asset_type=AssetType.EQUITY, instruction=Instruction.BUY, quantity=1,
        order_type=OrderType.MARKET,
    )


async def _run_lifecycle(session, broker, decision_id):
    """Preview -> approve -> execute -> status, returning all three responses."""
    executor = Executor(session=session, broker=broker)
    preview = await executor.preview_order(_proposal(decision_id))
    assert preview.risk_verdict == "APPROVED"
    receipt = await executor.execute_order(
        decision_id=decision_id,
        preview_id=preview.preview_id,
        approved_by="test-op",
        approved_at=datetime.now(UTC),
        attestation="parity test",
        idempotency_key=f"{decision_id}-idem",
    )
    status = await executor.get_order_status(decision_id)
    return preview, receipt, status


class TestExecutionPolicyParity:
    @pytest.mark.asyncio
    async def test_paper_broker_reports_simulated_true_throughout(self, test_db_engine_and_session, monkeypatch):
        _, session = test_db_engine_and_session
        _settings(monkeypatch)
        preview, receipt, status = await _run_lifecycle(session, PaperBrokerAdapter(), "policy-parity-paper")
        assert preview.simulated is True
        assert receipt.simulated is True
        assert status.simulated is True

    @pytest.mark.asyncio
    async def test_live_shaped_broker_reports_simulated_false_throughout(self, test_db_engine_and_session, monkeypatch):
        _, session = test_db_engine_and_session
        _settings(monkeypatch)
        preview, receipt, status = await _run_lifecycle(session, _FakeLiveShapedBroker(), "policy-parity-live")
        assert preview.simulated is False
        assert receipt.simulated is False
        assert status.simulated is False

    @pytest.mark.asyncio
    async def test_response_contract_field_names_are_identical_across_both_modes(self, test_db_engine_and_session, monkeypatch):
        """
        The whole point of `simulated` as a derived flag rather than a
        differently-shaped response per mode: nothing about the contract
        itself should differ just because the underlying broker does.
        """
        _, session = test_db_engine_and_session
        _settings(monkeypatch)
        paper_preview, paper_receipt, paper_status = await _run_lifecycle(session, PaperBrokerAdapter(), "policy-parity-contract-paper")
        live_preview, live_receipt, live_status = await _run_lifecycle(session, _FakeLiveShapedBroker(), "policy-parity-contract-live")

        assert set(paper_preview.model_fields) == set(live_preview.model_fields)
        assert set(paper_receipt.model_fields) == set(live_receipt.model_fields)
        assert set(paper_status.model_fields) == set(live_status.model_fields)
        assert "simulated" in paper_preview.model_fields
        assert "simulated" in paper_receipt.model_fields
        assert "simulated" in paper_status.model_fields
