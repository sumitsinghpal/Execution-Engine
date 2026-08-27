"""Tests for src/execution/external_signals.py and src/execution/edge_tf_connector.py."""

from datetime import datetime, timedelta, timezone

import pytest

from src.config import Settings
from src.execution.edge_tf_connector import claim_upstream, poll_once, report_upstream
from src.execution.external_signals import ExternalSignalService, ExternalSignalStatus
from src.integrations.edge_tf_client import EdgeTFClient, EdgeTFGatewayError
from src.models.orders import OrderStatus, OrderStatus_Model


def _instruction(trade_id="edge-trade-1", **overrides):
    payload = {
        "instruction_id": f"instr-{trade_id}",
        "trade_id": trade_id,
        "symbol": "QQQ",
        "side": "BUY",
        "quantity": 10.0,
        "order_type": "LIMIT",
        "limit_price": 450.0,
        "estimated_notional": 4500.0,
        "currency": "USD",
        "thesis_id": "thesis-1",
        "strategy_module": "quant_engine.iav_calculator",
        "rationale": "Institutional adoption velocity broke out.",
        "intent_hash": "hash123",
        "approved_fingerprint": "fp123",
        "approval_expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "idempotency_key": f"idem-{trade_id}",
    }
    payload.update(overrides)
    return payload


class TestExternalSignalService:
    def test_record_if_new_persists_a_signal(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        service = ExternalSignalService(session)

        record = service.record_if_new("edge-tf", _instruction("TRADE-A"))

        assert record is not None
        assert record.status == ExternalSignalStatus.PENDING
        assert record.symbol == "QQQ"
        assert record.source == "edge-tf"

    def test_duplicate_trade_id_is_not_recorded_twice(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        service = ExternalSignalService(session)

        first = service.record_if_new("edge-tf", _instruction("TRADE-DUP"))
        second = service.record_if_new("edge-tf", _instruction("TRADE-DUP"))

        assert first is not None
        assert second is None

    def test_list_signals_filters_by_status(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        service = ExternalSignalService(session)
        record = service.record_if_new("edge-tf", _instruction("TRADE-LIST"))

        pending = service.list_signals(status=ExternalSignalStatus.PENDING)
        assert any(r.id == record.id for r in pending)

        service.dismiss(record.id)
        pending_after = service.list_signals(status=ExternalSignalStatus.PENDING)
        assert not any(r.id == record.id for r in pending_after)

        dismissed = service.list_signals(status=ExternalSignalStatus.DISMISSED)
        assert any(r.id == record.id for r in dismissed)

    def test_dismiss_unknown_id_raises(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        with pytest.raises(ValueError):
            ExternalSignalService(session).dismiss(999_999)

    def test_get_by_trade_id_round_trips(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        service = ExternalSignalService(session)
        service.record_if_new("edge-tf", _instruction("TRADE-GET"))

        found = service.get_by_trade_id("TRADE-GET")
        assert found is not None
        assert found.external_trade_id == "TRADE-GET"
        assert service.get_by_trade_id("NOPE") is None

    def test_mark_status_updates_and_returns_none_for_unknown(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        service = ExternalSignalService(session)
        service.record_if_new("edge-tf", _instruction("TRADE-MARK"))

        updated = service.mark_status("TRADE-MARK", ExternalSignalStatus.CLAIMED)
        assert updated.status == ExternalSignalStatus.CLAIMED
        assert service.mark_status("NOPE", ExternalSignalStatus.CLAIMED) is None

    def test_to_trade_proposal_dict_uses_trade_id_as_decision_id(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        service = ExternalSignalService(session)
        record = service.record_if_new("edge-tf", _instruction("TRADE-PROPOSAL", quantity=12.6))

        proposal = record.to_trade_proposal_dict(account="primary", agent_id="edge-tf-agent")

        assert proposal["decision_id"] == "TRADE-PROPOSAL"
        assert proposal["account"] == "primary"
        assert proposal["agent_id"] == "edge-tf-agent"
        assert proposal["symbol"] == "QQQ"
        assert proposal["instruction"] == "BUY"
        assert proposal["quantity"] == 13  # rounded from 12.6, never below 1 whole share
        assert proposal["order_type"] == "LIMIT"
        assert proposal["limit_price"] == "450.0"
        assert proposal["strategy_id"] == "edge-tf:quant_engine.iav_calculator"

    def test_to_trade_proposal_dict_omits_limit_price_for_market_orders(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        service = ExternalSignalService(session)
        record = service.record_if_new(
            "edge-tf", _instruction("TRADE-MKT", order_type="MARKET", limit_price=None)
        )

        proposal = record.to_trade_proposal_dict(account="primary")
        assert "limit_price" not in proposal


class _FakeEdgeTFClient:
    """Records what was called instead of making real HTTP requests."""

    def __init__(self, instructions=None, claim_error=None):
        self._instructions = instructions or []
        self._claim_error = claim_error
        self.claimed = []
        self.reported = []

    async def list_orders(self):
        return self._instructions

    async def claim(self, trade_id, *, executor_id):
        if self._claim_error is not None:
            raise self._claim_error
        self.claimed.append((trade_id, executor_id))
        return {"trade_id": trade_id}

    async def report(self, trade_id, report):
        self.reported.append((trade_id, report))
        return {"recorded": True}


class TestPollOnce:
    @pytest.mark.asyncio
    async def test_poll_once_returns_zero_when_connector_not_configured(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        settings = Settings(_env_file=None, env="test", edge_tf_gateway_url=None, edge_tf_gateway_token=None)

        new_count = await poll_once(session, settings)
        assert new_count == 0

    @pytest.mark.asyncio
    async def test_poll_once_records_new_instructions(self, test_db_engine_and_session, monkeypatch):
        _, session = test_db_engine_and_session
        settings = Settings(
            _env_file=None, env="test",
            edge_tf_gateway_url="https://edge-tf.example:8601",
            edge_tf_gateway_token="secret-token",
        )
        fake = _FakeEdgeTFClient(instructions=[_instruction("POLL-1"), _instruction("POLL-2")])
        monkeypatch.setattr("src.execution.edge_tf_connector._client", lambda s: fake)

        new_count = await poll_once(session, settings)

        assert new_count == 2
        pending = ExternalSignalService(session).list_signals(status=ExternalSignalStatus.PENDING)
        assert {r.external_trade_id for r in pending} >= {"POLL-1", "POLL-2"}

    @pytest.mark.asyncio
    async def test_poll_once_does_not_duplicate_already_known_trades(self, test_db_engine_and_session, monkeypatch):
        _, session = test_db_engine_and_session
        settings = Settings(
            _env_file=None, env="test",
            edge_tf_gateway_url="https://edge-tf.example:8601",
            edge_tf_gateway_token="secret-token",
        )
        fake = _FakeEdgeTFClient(instructions=[_instruction("POLL-DUP")])
        monkeypatch.setattr("src.execution.edge_tf_connector._client", lambda s: fake)

        first = await poll_once(session, settings)
        second = await poll_once(session, settings)

        assert first == 1
        assert second == 0

    @pytest.mark.asyncio
    async def test_poll_once_survives_a_gateway_error(self, test_db_engine_and_session, monkeypatch):
        _, session = test_db_engine_and_session
        settings = Settings(
            _env_file=None, env="test",
            edge_tf_gateway_url="https://edge-tf.example:8601",
            edge_tf_gateway_token="secret-token",
        )

        class _Failing:
            async def list_orders(self):
                raise EdgeTFGatewayError(503, "UNAVAILABLE", "gateway down")

        monkeypatch.setattr("src.execution.edge_tf_connector._client", lambda s: _Failing())

        new_count = await poll_once(session, settings)
        assert new_count == 0


class TestClaimAndReportUpstream:
    @pytest.mark.asyncio
    async def test_claim_upstream_succeeds(self, test_db_engine_and_session, monkeypatch):
        _, session = test_db_engine_and_session
        settings = Settings(
            _env_file=None, env="test",
            edge_tf_gateway_url="https://edge-tf.example:8601",
            edge_tf_gateway_token="secret-token",
        )
        service = ExternalSignalService(session)
        record = service.record_if_new("edge-tf", _instruction("CLAIM-OK"))
        fake = _FakeEdgeTFClient()
        monkeypatch.setattr("src.execution.edge_tf_connector._client", lambda s: fake)

        await claim_upstream(record, settings, executor_id="execution-engine")

        assert fake.claimed == [("CLAIM-OK", "execution-engine")]

    @pytest.mark.asyncio
    async def test_claim_upstream_propagates_gateway_refusal(self, test_db_engine_and_session, monkeypatch):
        """A caller MUST see this — proceeding to execute locally after a refused claim risks a double execution."""
        _, session = test_db_engine_and_session
        settings = Settings(
            _env_file=None, env="test",
            edge_tf_gateway_url="https://edge-tf.example:8601",
            edge_tf_gateway_token="secret-token",
        )
        service = ExternalSignalService(session)
        record = service.record_if_new("edge-tf", _instruction("CLAIM-CONFLICT"))
        fake = _FakeEdgeTFClient(claim_error=EdgeTFGatewayError(409, "CLAIM_CONFLICT", "already claimed"))
        monkeypatch.setattr("src.execution.edge_tf_connector._client", lambda s: fake)

        with pytest.raises(EdgeTFGatewayError):
            await claim_upstream(record, settings, executor_id="execution-engine")

    @pytest.mark.asyncio
    async def test_claim_upstream_raises_when_not_configured(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        settings = Settings(_env_file=None, env="test", edge_tf_gateway_url=None, edge_tf_gateway_token=None)
        record = ExternalSignalService(session).record_if_new("edge-tf", _instruction("CLAIM-NOCFG"))

        with pytest.raises(EdgeTFGatewayError):
            await claim_upstream(record, settings, executor_id="execution-engine")

    @pytest.mark.asyncio
    async def test_report_upstream_maps_filled_status_and_calls_report(self, test_db_engine_and_session, monkeypatch):
        _, session = test_db_engine_and_session
        settings = Settings(
            _env_file=None, env="test",
            edge_tf_gateway_url="https://edge-tf.example:8601",
            edge_tf_gateway_token="secret-token",
        )
        record = ExternalSignalService(session).record_if_new("edge-tf", _instruction("REPORT-1"))
        fake = _FakeEdgeTFClient()
        monkeypatch.setattr("src.execution.edge_tf_connector._client", lambda s: fake)

        order_status = OrderStatus_Model(
            decision_id="REPORT-1",
            execution_id="broker-order-1",
            status=OrderStatus.FILLED,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            filled_quantity=10,
            average_fill_price=451.20,
            broker_status="FILLED",
            broker_message=None,
        )

        await report_upstream(record, settings, order_status)

        assert len(fake.reported) == 1
        trade_id, report = fake.reported[0]
        assert trade_id == "REPORT-1"
        assert report["status"] == "FILLED"
        assert report["filled_quantity"] == 10.0

    @pytest.mark.asyncio
    async def test_report_upstream_swallows_errors(self, test_db_engine_and_session, monkeypatch):
        """The local trade already happened by the time this runs — a downstream reporting failure must never surface to the execute caller."""
        _, session = test_db_engine_and_session
        settings = Settings(
            _env_file=None, env="test",
            edge_tf_gateway_url="https://edge-tf.example:8601",
            edge_tf_gateway_token="secret-token",
        )
        record = ExternalSignalService(session).record_if_new("edge-tf", _instruction("REPORT-FAIL"))

        class _Failing:
            async def report(self, trade_id, report):
                raise RuntimeError("network blip")

        monkeypatch.setattr("src.execution.edge_tf_connector._client", lambda s: _Failing())

        order_status = OrderStatus_Model(
            decision_id="REPORT-FAIL",
            execution_id="broker-order-2",
            status=OrderStatus.REJECTED,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            filled_quantity=0,
            average_fill_price=None,
            broker_status="REJECTED",
            broker_message="insufficient buying power",
        )

        # Must not raise.
        await report_upstream(record, settings, order_status)


class TestEdgeTFClient:
    def test_requires_base_url_and_token(self):
        with pytest.raises(ValueError):
            EdgeTFClient("", "token")
        with pytest.raises(ValueError):
            EdgeTFClient("https://example.com", "")

    def test_strips_trailing_slash(self):
        client = EdgeTFClient("https://edge-tf.example:8601/", "token")
        assert client._base_url == "https://edge-tf.example:8601"
