"""Tests for src/execution/ep_edge_earnings_adapter.py and its ingestion endpoint."""

import pytest
from fastapi.testclient import TestClient

from src.execution.ep_edge_earnings_adapter import record_batch, record_candidate
from src.execution.external_signals import ExternalSignalService


def _candidate(ticker="MU", direction="bullish", expected_value=0.05, **overrides):
    payload = {
        "ticker": ticker,
        "thesis": "Residual earnings information implies a bullish revision.",
        "direction": direction,
        "instrument": "equity",
        "expected_move": 0.05,
        "implied_move": 0.03,
        "probability_positive": 0.8,
        "expected_value": expected_value,
        "confidence": 0.8,
        "market_awareness": 0.2,
        "invalidation_conditions": ("Target guidance contradicts the propagated driver.",),
    }
    payload.update(overrides)
    return payload


class TestRecordCandidate:
    def test_bullish_candidate_becomes_a_buy_signal(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        record = record_candidate(session, _candidate(direction="bullish"))

        assert record is not None
        assert record.side == "BUY"
        assert record.symbol == "MU"
        assert record.source == "ep-edge-earnings"
        assert record.quantity is None  # not sized by the source

    def test_bearish_candidate_becomes_a_sell_signal(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        record = record_candidate(session, _candidate(direction="bearish"))

        assert record is not None
        assert record.side == "SELL"

    def test_neutral_candidate_is_skipped(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        record = record_candidate(session, _candidate(direction="neutral"))

        assert record is None

    def test_identical_candidate_is_not_recorded_twice(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        first = record_candidate(session, _candidate(ticker="NVDA"))
        second = record_candidate(session, _candidate(ticker="NVDA"))

        assert first is not None
        assert second is None

    def test_a_genuinely_different_candidate_for_the_same_ticker_is_new(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        first = record_candidate(session, _candidate(ticker="REVISE-1", expected_value=0.05))
        second = record_candidate(session, _candidate(ticker="REVISE-1", expected_value=0.09))

        assert first is not None
        assert second is not None
        assert first.external_trade_id != second.external_trade_id

    def test_rejects_unknown_fields(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        with pytest.raises(Exception):
            record_candidate(session, _candidate(unexpected_field="nope"))


class TestRecordBatch:
    def test_counts_only_the_genuinely_new_tradeable_candidates(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        batch = [
            _candidate(ticker="AAA", direction="bullish"),
            _candidate(ticker="BBB", direction="neutral"),
            _candidate(ticker="CCC", direction="bearish"),
        ]

        new_count = record_batch(session, batch)
        assert new_count == 2


class TestToTradeProposalDictRequiresQuantity:
    def test_raises_without_a_quantity_override(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        record = record_candidate(session, _candidate(ticker="QTY-1"))

        with pytest.raises(ValueError):
            record.to_trade_proposal_dict(account="primary")

    def test_succeeds_with_a_quantity_override(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        record = record_candidate(session, _candidate(ticker="QTY-2"))

        proposal = record.to_trade_proposal_dict(account="primary", quantity=7)
        assert proposal["quantity"] == 7
        assert proposal["order_type"] == "MARKET"
        assert "limit_price" not in proposal


@pytest.fixture
def client(app_with_test_db):
    return TestClient(app_with_test_db, headers={"x-admin-key": "change-me-in-prod"})


class TestIngestEndpoint:
    def test_ingest_records_only_tradeable_candidates(self, client):
        response = client.post(
            "/v1/external-signals/ingest",
            json={
                "source": "ep-edge-earnings",
                "candidates": [
                    _candidate(ticker="ING-A", direction="bullish"),
                    _candidate(ticker="ING-B", direction="neutral"),
                ],
            },
        )

        assert response.status_code == 200
        assert response.json()["new_signals"] == 1

        listed = client.get("/v1/external-signals", params={"status": "PENDING"})
        symbols = {s["symbol"] for s in listed.json()["signals"]}
        assert "ING-A" in symbols
        assert "ING-B" not in symbols

    def test_ingest_rejects_unknown_source(self, client):
        response = client.post(
            "/v1/external-signals/ingest",
            json={"source": "not-a-real-source", "candidates": []},
        )
        assert response.status_code == 400

    def test_load_without_quantity_is_rejected(self, client):
        client.post(
            "/v1/external-signals/ingest",
            json={"source": "ep-edge-earnings", "candidates": [_candidate(ticker="LOAD-NOQTY")]},
        )
        listed = client.get("/v1/external-signals", params={"status": "PENDING"}).json()["signals"]
        trade_id = next(s["external_trade_id"] for s in listed if s["symbol"] == "LOAD-NOQTY")

        response = client.post(f"/v1/external-signals/{trade_id}/load", params={"account": "primary"})
        assert response.status_code == 422

    def test_load_with_quantity_succeeds(self, client):
        client.post(
            "/v1/external-signals/ingest",
            json={"source": "ep-edge-earnings", "candidates": [_candidate(ticker="LOAD-QTY")]},
        )
        listed = client.get("/v1/external-signals", params={"status": "PENDING"}).json()["signals"]
        trade_id = next(s["external_trade_id"] for s in listed if s["symbol"] == "LOAD-QTY")

        response = client.post(
            f"/v1/external-signals/{trade_id}/load", params={"account": "primary", "quantity": 3}
        )
        assert response.status_code == 200
        assert response.json()["proposal"]["quantity"] == 3
        assert response.json()["proposal"]["decision_id"] == trade_id
