"""
Tests for src/execution/external_signals.py itself — the shared
schema/service every source's adapter (edge_tf_connector,
ep_edge_earnings_adapter, hedge_engine_adapter) builds on. Per-source
behavior is covered in each adapter's own test file; this covers the
service directly, including the confidence field's default for a
source that carries no such concept.
"""

from src.execution.external_signals import ExternalSignalService


def _instruction(trade_id="test-trade-1", **overrides):
    payload = {
        "trade_id": trade_id,
        "symbol": "QQQ",
        "side": "BUY",
        "order_type": "MARKET",
        "thesis_id": "thesis-1",
        "strategy_module": "test.module",
    }
    payload.update(overrides)
    return payload


class TestConfidenceField:
    def test_defaults_to_none_when_the_source_has_no_confidence_concept(self, test_db_engine_and_session):
        """edge-tf's instructions never carry a `confidence` key — it's a fully-approved order, not a probabilistic thesis (see edge_tf_connector.py)."""
        _, session = test_db_engine_and_session
        record = ExternalSignalService(session).record_if_new("edge-tf", _instruction(trade_id="edge-tf-no-conf"))

        assert record is not None
        assert record.confidence is None
        assert record.to_dict()["confidence"] is None

    def test_is_carried_through_when_provided(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        record = ExternalSignalService(session).record_if_new(
            "ep-edge-earnings", _instruction(trade_id="with-conf", confidence=0.73)
        )

        assert record is not None
        assert record.confidence == 0.73
        assert record.to_dict()["confidence"] == 0.73
