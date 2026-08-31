"""Tests for src/execution/hedge_engine_adapter.py and its ingestion endpoint."""

import uuid

import pytest
from fastapi.testclient import TestClient

from src.execution.hedge_engine_adapter import record_batch, record_decision


def _decision(ticker="SSO", viability_pass=True, ev_net=0.03, decision_id=None, leverage=3.0, **overrides):
    payload = {
        "decision_id": decision_id or uuid.uuid4().hex,
        "llm_output": {
            "p_success": 0.7,
            "p_confidence": 0.82,
            "horizon_days": 5,
            "expected_delta": {"fav": 0.08, "neutral": 0.0, "unfav": -0.05},
            "suggested_instrument": {"type": "LETF", "ticker": ticker, "leverage": leverage},
            "rationale": "Synthetic demo: tactical LETF hedge for short horizon",
            "evidence": [],
            "flags": {"requires_human_review": False},
        },
        "quant_checks": {
            "ev_gross": ev_net + 0.005,
            "letf_decay": 0.002,
            "ev_net": ev_net,
            "viability_pass": viability_pass,
            "p_confidence": 0.82,
            "safety_margin": 0.01,
            "notes": "" if viability_pass else "Net EV below safety margin.",
        },
        "prompt_hash": "demo_prompt_hash_0001",
        "model_version": "demo-model-v1",
        "timestamp_utc": "2026-08-30T00:00:00+00:00",
        "audit_hash": "deadbeef",
    }
    payload.update(overrides)
    return payload


class TestRecordDecision:
    def test_viable_decision_becomes_a_buy_signal(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        record = record_decision(session, _decision(ticker="SSO", viability_pass=True))

        assert record is not None
        assert record.side == "BUY"
        assert record.symbol == "SSO"
        assert record.source == "hedge-engine"
        assert record.quantity is None  # not sized by the source
        assert "ev_net" in record.rationale

    def test_non_viable_decision_is_skipped(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        record = record_decision(session, _decision(viability_pass=False))

        assert record is None

    def test_identical_decision_id_is_not_recorded_twice(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        decision_id = uuid.uuid4().hex
        first = record_decision(session, _decision(ticker="TQQQ", decision_id=decision_id))
        second = record_decision(session, _decision(ticker="TQQQ", decision_id=decision_id))

        assert first is not None
        assert second is None

    def test_a_new_decision_id_for_the_same_ticker_is_new(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        first = record_decision(session, _decision(ticker="REVISE-HEDGE"))
        second = record_decision(session, _decision(ticker="REVISE-HEDGE"))

        assert first is not None
        assert second is not None
        assert first.external_trade_id != second.external_trade_id

    def test_rejects_missing_required_fields(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        broken = _decision()
        del broken["quant_checks"]
        with pytest.raises(Exception):
            record_decision(session, broken)

    def test_thesis_id_is_the_hedge_engine_decision_id(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        decision_id = uuid.uuid4().hex
        record = record_decision(session, _decision(decision_id=decision_id))

        assert record.thesis_id == decision_id
        assert record.strategy_module == "hedge-engine:letf-decay"


class TestRecordBatch:
    def test_counts_only_the_genuinely_new_viable_decisions(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        batch = [
            _decision(ticker="AAA", viability_pass=True),
            _decision(ticker="BBB", viability_pass=False),
            _decision(ticker="CCC", viability_pass=True),
        ]

        new_count = record_batch(session, batch)
        assert new_count == 2


class TestToTradeProposalDictRequiresQuantity:
    def test_raises_without_a_quantity_override(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        record = record_decision(session, _decision(ticker="QTY-HEDGE-1"))

        with pytest.raises(ValueError):
            record.to_trade_proposal_dict(account="primary")

    def test_succeeds_with_a_quantity_override(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        record = record_decision(session, _decision(ticker="QTY-HEDGE-2"))

        proposal = record.to_trade_proposal_dict(account="primary", quantity=4)
        assert proposal["quantity"] == 4
        assert proposal["order_type"] == "MARKET"
        assert "limit_price" not in proposal


@pytest.fixture
def client(app_with_test_db):
    return TestClient(app_with_test_db, headers={"x-admin-key": "change-me-in-prod"})


class TestIngestEndpoint:
    def test_ingest_records_only_viable_decisions(self, client):
        response = client.post(
            "/v1/external-signals/ingest",
            json={
                "source": "hedge-engine",
                "candidates": [
                    _decision(ticker="ING-HEDGE-A", viability_pass=True),
                    _decision(ticker="ING-HEDGE-B", viability_pass=False),
                ],
            },
        )

        assert response.status_code == 200
        assert response.json()["new_signals"] == 1

        listed = client.get("/v1/external-signals", params={"status": "PENDING"})
        symbols = {s["symbol"] for s in listed.json()["signals"]}
        assert "ING-HEDGE-A" in symbols
        assert "ING-HEDGE-B" not in symbols

    def test_ep_edge_and_hedge_ingest_coexist_on_the_same_endpoint(self, client):
        """The shared /v1/external-signals/ingest endpoint dispatches by `source` — confirms adding hedge-engine didn't break ep-edge-earnings."""
        ep_response = client.post(
            "/v1/external-signals/ingest",
            json={
                "source": "ep-edge-earnings",
                "candidates": [
                    {
                        "ticker": "COEXIST-EP",
                        "thesis": "test",
                        "direction": "bullish",
                        "instrument": "equity",
                        "expected_move": 0.05,
                        "implied_move": 0.03,
                        "probability_positive": 0.8,
                        "expected_value": 0.05,
                        "confidence": 0.8,
                        "market_awareness": 0.2,
                    }
                ],
            },
        )
        hedge_response = client.post(
            "/v1/external-signals/ingest",
            json={"source": "hedge-engine", "candidates": [_decision(ticker="COEXIST-HEDGE")]},
        )

        assert ep_response.status_code == 200 and ep_response.json()["new_signals"] == 1
        assert hedge_response.status_code == 200 and hedge_response.json()["new_signals"] == 1

        listed = client.get("/v1/external-signals", params={"status": "PENDING"}).json()["signals"]
        sources_by_symbol = {s["symbol"]: s for s in listed}
        assert "COEXIST-EP" in sources_by_symbol
        assert "COEXIST-HEDGE" in sources_by_symbol

    def test_load_without_quantity_is_rejected(self, client):
        client.post(
            "/v1/external-signals/ingest",
            json={"source": "hedge-engine", "candidates": [_decision(ticker="LOAD-HEDGE-NOQTY")]},
        )
        listed = client.get("/v1/external-signals", params={"status": "PENDING"}).json()["signals"]
        trade_id = next(s["external_trade_id"] for s in listed if s["symbol"] == "LOAD-HEDGE-NOQTY")

        response = client.post(f"/v1/external-signals/{trade_id}/load", params={"account": "primary"})
        assert response.status_code == 422

    def test_load_with_quantity_succeeds(self, client):
        client.post(
            "/v1/external-signals/ingest",
            json={"source": "hedge-engine", "candidates": [_decision(ticker="LOAD-HEDGE-QTY")]},
        )
        listed = client.get("/v1/external-signals", params={"status": "PENDING"}).json()["signals"]
        trade_id = next(s["external_trade_id"] for s in listed if s["symbol"] == "LOAD-HEDGE-QTY")

        response = client.post(
            f"/v1/external-signals/{trade_id}/load", params={"account": "primary", "quantity": 2}
        )
        assert response.status_code == 200
        assert response.json()["proposal"]["quantity"] == 2
        assert response.json()["proposal"]["decision_id"] == trade_id
