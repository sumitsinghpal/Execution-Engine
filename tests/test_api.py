"""Tests for API endpoints."""

from datetime import datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from src.api.server import app, get_db
from src.models.orders import (
    ApprovalArtifact,
    AssetType,
    ExecutionRequest,
    Instruction,
    OrderType,
    TradeProposal,
)


@pytest.fixture
def client(app_with_test_db):
    """Create FastAPI test client with test database."""
    return TestClient(app_with_test_db)


class TestPreviewEndpoint:
    """Test POST /v1/orders/preview."""
    
    def test_preview_valid_order(self, client, sample_trade_proposal):
        """Preview accepts valid order."""
        response = client.post(
            "/v1/orders/preview",
            json=sample_trade_proposal.model_dump(mode="json"),
        )
        
        assert response.status_code == 200
        data = response.json()
        assert "preview_id" in data
        assert data["decision_id"] == "edge-20260821-001"
        assert "payload_checksum" in data
        assert "expires_at" in data
    
    def test_preview_rejects_invalid_schema(self, client):
        """Preview rejects invalid schema."""
        response = client.post(
            "/v1/orders/preview",
            json={
                "decision_id": "edge-test",
                "account": "primary",
                "symbol": "QQQ",
                # Missing required fields
            },
        )
        
        assert response.status_code == 422  # Validation error
    
    def test_preview_rejects_unknown_fields(self, client):
        """Preview rejects unknown fields."""
        response = client.post(
            "/v1/orders/preview",
            json={
                "decision_id": "edge-test",
                "account": "primary",
                "symbol": "QQQ",
                "asset_type": "ETF",
                "instruction": "BUY",
                "quantity": 10,
                "order_type": "MARKET",
                "unknown_field": "should fail",
            },
        )
        
        assert response.status_code == 422


class TestHealthEndpoint:
    """Test GET /v1/health."""
    
    def test_health_check(self, client):
        """Health check returns status."""
        response = client.get("/v1/health")
        
        assert response.status_code == 200
        data = response.json()
        assert "status" in data
        assert "timestamp" in data
        assert "database" in data
        assert "broker_connectivity" in data


class TestKillSwitchEndpoints:
    """Test kill switch endpoints."""
    
    def test_kill_switch_on_requires_admin_key(self, client):
        """kill-switch/on requires admin key."""
        response = client.post("/v1/kill-switch/on")
        
        assert response.status_code == 403
    
    def test_kill_switch_on_with_valid_key(self, client):
        """kill-switch/on works with valid key."""
        response = client.post(
            "/v1/kill-switch/on",
            headers={"x-admin-key": "change-me-in-prod"},
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["enabled"] is True
    
    def test_kill_switch_off_with_valid_key(self, client):
        """kill-switch/off works with valid key."""
        # First turn it on
        client.post(
            "/v1/kill-switch/on",
            headers={"x-admin-key": "change-me-in-prod"},
        )
        
        # Then turn it off
        response = client.post(
            "/v1/kill-switch/off",
            headers={"x-admin-key": "change-me-in-prod"},
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["enabled"] is False
    
    def test_get_kill_switch_status(self, client):
        """Get current kill switch state."""
        response = client.get("/v1/kill-switch/status")
        
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data["enabled"], bool)


class TestMarketStatusEndpoint:
    """Test GET /v1/market-status."""
    
    def test_market_status(self, client):
        """Market status returns info."""
        response = client.get("/v1/market-status")
        
        assert response.status_code == 200
        data = response.json()
        assert "in_market_hours" in data
        assert "current_time_et" in data
        assert "market_open" in data
        assert "market_close" in data
