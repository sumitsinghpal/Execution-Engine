"""OAuth authorization-code and refresh-token support for Schwab."""

from datetime import UTC, datetime, timedelta
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

from src.brokers.base import BrokerAuthenticationError, BrokerError


class SchwabOAuthClient:
    """Manage Schwab OAuth authorization-code bootstrap and access token refresh."""

    AUTHORIZE_URL = "https://api.schwabapi.com/v1/oauth/authorize"
    TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"

    def __init__(
        self,
        app_key: str,
        app_secret: str,
        redirect_uri: str,
        refresh_token: Optional[str] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        timeout_sec: float = 30.0,
    ) -> None:
        if not app_key or not app_secret or not redirect_uri:
            raise BrokerError("Schwab OAuth requires app key, app secret, and redirect URI")
        self.app_key = app_key
        self.app_secret = app_secret
        self.redirect_uri = redirect_uri
        self.refresh_token = refresh_token
        self.transport = transport
        self.timeout_sec = timeout_sec
        self.access_token: Optional[str] = None
        self.access_token_expires_at: Optional[datetime] = None

    def authorization_url(self, state: str) -> str:
        """Return the initial user authorization URL for the OAuth bootstrap flow."""
        return f"{self.AUTHORIZE_URL}?{urlencode({'client_id': self.app_key, 'redirect_uri': self.redirect_uri, 'state': state})}"

    async def exchange_authorization_code(self, authorization_code: str) -> dict[str, Any]:
        """Exchange an interactive authorization code and retain returned refresh token."""
        data = await self._request_token({
            "grant_type": "authorization_code",
            "code": authorization_code,
            "redirect_uri": self.redirect_uri,
        })
        self._store_token_response(data)
        return data

    async def get_access_token(self) -> str:
        """Return a valid access token, refreshing it when necessary."""
        if self.access_token and self.access_token_expires_at and datetime.now(UTC) < self.access_token_expires_at:
            return self.access_token
        if not self.refresh_token:
            raise BrokerAuthenticationError("Schwab refresh token is required for authenticated API calls")
        data = await self._request_token({"grant_type": "refresh_token", "refresh_token": self.refresh_token})
        self._store_token_response(data)
        return self.access_token or ""

    async def _request_token(self, payload: dict[str, str]) -> dict[str, Any]:
        async with httpx.AsyncClient(transport=self.transport, timeout=self.timeout_sec) as client:
            response = await client.post(self.TOKEN_URL, auth=(self.app_key, self.app_secret), data=payload)
            if response.status_code in (400, 401, 403):
                # Schwab refresh tokens are valid for 7 days and cannot be
                # renewed automatically — this status means ours is expired,
                # revoked, or otherwise rejected, and every subsequent call
                # will fail identically until a human re-authenticates
                # in-browser. Surface that distinctly from a transient
                # network/5xx failure, which is worth retrying.
                raise BrokerAuthenticationError(
                    f"Schwab rejected the OAuth token request (HTTP {response.status_code}); "
                    f"the refresh token is likely expired or revoked and requires interactive "
                    f"re-authentication: {response.text}"
                )
            response.raise_for_status()
            return response.json()

    def _store_token_response(self, data: dict[str, Any]) -> None:
        access_token = data.get("access_token")
        if not access_token:
            raise BrokerError("Schwab token response did not include access_token")
        self.access_token = access_token
        self.refresh_token = data.get("refresh_token", self.refresh_token)
        expires_in = int(data.get("expires_in", 1800))
        self.access_token_expires_at = datetime.now(UTC) + timedelta(seconds=max(expires_in - 60, 0))