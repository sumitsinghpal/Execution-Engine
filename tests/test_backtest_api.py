"""
Tests for POST /v1/backtest/run at the FastAPI boundary — mocks
src.api.server.run_backtest_suite so these never hit real yfinance
network. Complements tests/test_backtest_engine.py's pure-logic tests: the
profit_factor=None regression specifically needs to be checked at this
JSON-response boundary, since that's where the real bug (float("inf")
crashing json.dumps) actually surfaced — a pure-Python dataclass check
alone wouldn't have caught it.
"""

from datetime import date

import pytest
from fastapi.testclient import TestClient

from src.backtest.engine import BacktestResult, BacktestTrade


@pytest.fixture
def client(app_with_test_db):
    return TestClient(app_with_test_db)


def _all_wins_result() -> BacktestResult:
    trade = BacktestTrade(
        symbol="QQQ", strategy_id="golden_cross", entry_date="2024-01-01", entry_price=100.0,
        stop_loss_price=99.0, take_profit_price=102.0, quantity=10,
        exit_date="2024-01-05", exit_price=102.0, exit_reason="TARGET", pnl_usd=20.0, r_multiple=2.0,
    )
    return BacktestResult(
        symbol="QQQ", strategy_id="golden_cross", starting_capital=100_000.0, ending_capital=100_020.0,
        total_trades=1, wins=1, losses=0, win_rate=1.0, profit_factor=None,
        max_drawdown_pct=0.0, total_return_pct=0.02, trades=[trade],
    )


class TestBacktestEndpoint:
    def test_all_wins_pair_serializes_cleanly(self, client, monkeypatch):
        """The exact bug this regression-guards: an all-wins pair used to produce profit_factor=inf, and FastAPI's default JSONResponse 500s on inf."""
        import src.api.server as server

        async def fake_suite(*a, **k):
            return [_all_wins_result()], []

        monkeypatch.setattr(server, "run_backtest_suite", fake_suite)

        response = client.post(
            "/v1/backtest/run",
            json={"symbols": ["QQQ"], "strategy_ids": ["golden_cross"], "start_date": "2024-01-01", "end_date": "2024-06-01"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["results"][0]["profit_factor"] is None
        assert body["summary"]["wins"] == 1

    def test_unknown_strategy_id_is_rejected(self, client):
        response = client.post(
            "/v1/backtest/run",
            json={"strategy_ids": ["not-a-real-strategy"], "start_date": "2024-01-01", "end_date": "2024-06-01"},
        )
        assert response.status_code == 400

    def test_end_date_before_start_date_is_rejected(self, client):
        response = client.post(
            "/v1/backtest/run",
            json={"start_date": "2024-06-01", "end_date": "2024-01-01"},
        )
        assert response.status_code == 422

    def test_too_many_symbols_is_rejected(self, client):
        response = client.post(
            "/v1/backtest/run",
            json={"symbols": [f"SYM{i}" for i in range(11)], "start_date": "2024-01-01", "end_date": "2024-06-01"},
        )
        assert response.status_code == 422
