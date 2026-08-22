"""
Schwab API client with OAuth token management.
Supports mocked mode for testing without real credentials.
"""

from datetime import datetime, timedelta
from typing import Any, Optional

import httpx
from src.config import get_settings
from src.logging_config import get_logger

logger = get_logger(__name__)


class SchwabOAuthClient:
    """Handle Schwab OAuth token lifecycle."""
    
    TOKEN_ENDPOINT = "https://api.schwabapi.com/v1/oauth/token"
    
    def __init__(self, app_key: str, app_secret: str, refresh_token: str):
        self.app_key = app_key
        self.app_secret = app_secret
        self.refresh_token = refresh_token
        self.access_token: Optional[str] = None
        self.token_expires_at: Optional[datetime] = None
    
    async def get_access_token(self) -> str:
        """Get or refresh access token."""
        # Check if current token is still valid
        if self.access_token and self.token_expires_at and datetime.utcnow() < self.token_expires_at:
            return self.access_token
        
        # Refresh token
        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.TOKEN_ENDPOINT,
                auth=(self.app_key, self.app_secret),
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self.refresh_token,
                },
            )
            response.raise_for_status()
            data = response.json()
        
        self.access_token = data["access_token"]
        expires_in = data.get("expires_in", 1800)  # Default 30 min
        self.token_expires_at = datetime.utcnow() + timedelta(seconds=expires_in - 60)  # Refresh 60s before expiry
        
        logger.info("oauth_token_refreshed", expires_in=expires_in)
        return self.access_token


class SchwabClient:
    """High-level Schwab API client."""
    
    BASE_URL = "https://api.schwabapi.com"
    
    def __init__(self, mock: bool = False):
        self.mock = mock
        self.settings = get_settings()
        
        if not mock:
            self.oauth = SchwabOAuthClient(
                self.settings.schwab_app_key,
                self.settings.schwab_app_secret,
                self.settings.schwab_refresh_token,
            )
    
    async def preview_order(self, order_spec: dict) -> dict:
        """
        Preview an order with Schwab.
        Returns estimated commission, cost, and execution details.
        """
        if self.mock:
            return self._mock_preview_response(order_spec)
        
        token = await self.oauth.get_access_token()
        
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.BASE_URL}/v1/accounts/preview/orders",
                headers={"Authorization": f"Bearer {token}"},
                json=order_spec,
                timeout=self.settings.schwab_api_timeout_sec,
            )
            response.raise_for_status()
        
        logger.info("schwab_preview_success", decision_id=order_spec.get("order_id"))
        return response.json()
    
    async def submit_order(self, order_spec: dict) -> dict:
        """Submit an order to Schwab."""
        if self.mock:
            return self._mock_submit_response(order_spec)
        
        token = await self.oauth.get_access_token()
        
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.BASE_URL}/v1/accounts/orders",
                headers={"Authorization": f"Bearer {token}"},
                json=order_spec,
                timeout=self.settings.schwab_api_timeout_sec,
            )
            response.raise_for_status()
        
        logger.info("schwab_submit_success", broker_order_id=response.json().get("orderId"))
        return response.json()
    
    async def get_order_status(self, account_id: str, order_id: str) -> dict:
        """Query order status from Schwab."""
        if self.mock:
            return self._mock_status_response(order_id)
        
        token = await self.oauth.get_access_token()
        
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.BASE_URL}/v1/accounts/{account_id}/orders/{order_id}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=self.settings.schwab_api_timeout_sec,
            )
            response.raise_for_status()
        
        return response.json()
    
    @staticmethod
    def _mock_preview_response(order_spec: dict) -> dict:
        """Mock Schwab preview response for testing."""
        return {
            "orderId": "mock-preview-123",
            "estimatedCommission": 0.0,
            "estimatedTotalInvestment": 7215.00,
            "status": "OK",
            "symbol": order_spec.get("symbol", "QQQ"),
            "quantity": order_spec.get("quantity", 10),
        }
    
    @staticmethod
    def _mock_submit_response(order_spec: dict) -> dict:
        """Mock Schwab submit response for testing."""
        return {
            "orderId": "mock-order-456",
            "status": "ACCEPTED",
            "symbol": order_spec.get("symbol", "QQQ"),
            "quantity": order_spec.get("quantity", 10),
            "enteredTime": datetime.utcnow().isoformat(),
        }
    
    @staticmethod
    def _mock_status_response(order_id: str) -> dict:
        """Mock Schwab status response for testing."""
        return {
            "orderId": order_id,
            "status": "FILLED",
            "filledQuantity": 10,
            "averageFillPrice": 721.50,
        }
