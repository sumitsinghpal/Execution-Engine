"""
Confirms the actual auth boundary described in README.md's security note —
account data, orders, and every action endpoint require the admin key;
only pure infrastructure/catalog reads (health, strategy catalog,
market status) stay open. Uses a client with NO default auth header
(unlike every other test file's `client` fixture) specifically to probe
this boundary from the outside.
"""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def anon_client(app_with_test_db):
    """No default auth header — the point of this file is testing what an unauthenticated caller can and can't reach."""
    return TestClient(app_with_test_db)


PROTECTED_GET_ROUTES = [
    "/v1/orders",
    "/v1/account/primary/balances",
    "/v1/account/primary/positions",
    "/v1/orders/some-decision-id",
    "/v1/orders/some-decision-id/slices",
    "/v1/risk/symbol-exposure",
    "/v1/strategies/signals",
    "/v1/external-signals",
    "/v1/autonomous/status",
    "/v1/autonomous/positions",
    "/v1/autonomous/plan",
    "/v1/quotes?symbols=QQQ",
    "/v1/watchlists",
    "/v1/alerts",
    "/v1/orders/bracket",
]

PROTECTED_POST_ROUTES = [
    "/v1/orders/preview",
    "/v1/orders/execute",
    "/v1/reconciliation/positions",
    "/v1/risk/drawdown-check",
    "/v1/risk/agent-exposure-check",
    "/v1/strategies/scan-all",
    "/v1/strategies/some-id/scan",
    "/v1/strategies/signals/1/dismiss",
    "/v1/external-signals/poll",
    "/v1/external-signals/some-id/load",
    "/v1/external-signals/ingest",
    "/v1/external-signals/some-id/dismiss",
    "/v1/backtest/run",
    "/v1/autonomous/run-once",
    "/v1/orders/multi-leg/preview",
    "/v1/orders/multi-leg/execute",
    "/v1/autonomous/rank-strategies",
    "/v1/autonomous/start",
    "/v1/autonomous/arm",
    "/v1/autonomous/disarm",
    "/v1/autonomous/rotate-now",
    "/v1/watchlists/Tech/items",
    "/v1/alerts",
    "/v1/alerts/check-now",
    "/v1/orders/bracket/attach",
    "/v1/orders/bracket/1/cancel",
    "/v1/orders/bracket/check-now",
]

PROTECTED_DELETE_ROUTES = [
    "/v1/watchlists/Tech/items/QQQ",
    "/v1/watchlists/Tech",
    "/v1/alerts/1",
]

PUBLIC_ROUTES = ["/v1/health", "/v1/strategies", "/v1/market-status"]


class TestProtectedRoutesRejectUnauthenticated:
    @pytest.mark.parametrize("path", PROTECTED_GET_ROUTES)
    def test_get_requires_admin_key(self, anon_client, path):
        response = anon_client.get(path)
        assert response.status_code == 403, f"{path} should require the admin key"

    @pytest.mark.parametrize("path", PROTECTED_POST_ROUTES)
    def test_post_requires_admin_key(self, anon_client, path):
        response = anon_client.post(path, json={})
        assert response.status_code == 403, f"{path} should require the admin key"

    @pytest.mark.parametrize("path", PROTECTED_DELETE_ROUTES)
    def test_delete_requires_admin_key(self, anon_client, path):
        response = anon_client.delete(path)
        assert response.status_code == 403, f"{path} should require the admin key"


class TestPublicRoutesStayOpen:
    @pytest.mark.parametrize("path", PUBLIC_ROUTES)
    def test_public_route_does_not_require_a_key(self, anon_client, path):
        response = anon_client.get(path)
        assert response.status_code != 403, f"{path} is meant to stay public but rejected an unauthenticated request"
