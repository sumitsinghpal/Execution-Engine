"""OAuth authorization-code and refresh-token support for Schwab."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

from src.brokers.base import BrokerAuthenticationError, BrokerError
from src.logging_config import get_logger

logger = get_logger(__name__)


class SchwabOAuthClient:
    """Manage Schwab OAuth authorization-code bootstrap and access token refresh."""

    AUTHORIZE_URL = "https://api.schwabapi.com/v1/oauth/authorize"
    TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"

    # Schwab's refresh token lapses after ~7 days of not being used to mint an
    # access token (see scripts/schwab_oauth_bootstrap.py's docstring) — normal
    # use of a running server keeps it alive, so this is meant to catch a
    # server that's been down or idle for most of a week, not normal operation.
    REFRESH_TOKEN_LIFETIME = timedelta(days=7)
    REFRESH_TOKEN_WARNING_WINDOW = timedelta(hours=24)

    def __init__(
        self,
        app_key: str,
        app_secret: str,
        redirect_uri: str,
        refresh_token: Optional[str] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        timeout_sec: float = 30.0,
        token_file: Optional[str] = None,
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
        self.refresh_token_issued_at: Optional[datetime] = None
        self._refresh_lock: Optional[asyncio.Lock] = None
        # A refresh token rotated by a previous process run — see
        # _persist_refresh_token — takes priority over the `refresh_token`
        # argument above, which only reflects whatever was true at the last
        # manual OAuth bootstrap (or Settings/.env). Without this, a container
        # restart after Schwab rotates the token would authenticate with a
        # stale, already-superseded value and fail.
        self.token_file = token_file
        if self.token_file:
            self._load_persisted_refresh_token()

    def _load_persisted_refresh_token(self) -> None:
        try:
            data = json.loads(Path(self.token_file).read_text())
        except (OSError, ValueError):
            return  # no file yet, or unreadable — fall back to the constructor argument
        stored = data.get("refresh_token")
        if stored:
            self.refresh_token = stored
        issued_at = data.get("issued_at")
        if issued_at:
            try:
                self.refresh_token_issued_at = datetime.fromisoformat(issued_at)
            except ValueError:
                pass

    def _persist_refresh_token(self) -> None:
        """Best-effort: a failure here must never block authentication — it only
        means the NEXT restart falls back to the (possibly stale) configured
        value, same as before this feature existed."""
        try:
            path = Path(self.token_file)
            path.write_text(json.dumps({
                "refresh_token": self.refresh_token,
                "issued_at": self.refresh_token_issued_at.isoformat() if self.refresh_token_issued_at else None,
            }))
            try:
                path.chmod(0o600)  # owner-read/write only; no-op on platforms without POSIX permissions
            except (OSError, NotImplementedError):
                pass
        except OSError as exc:
            logger.error("schwab_token_persist_failed", token_file=self.token_file, error=str(exc))

    def is_refresh_token_expiring_soon(self) -> bool:
        """
        True once less than 24 hours remain in the refresh token's ~7-day
        lifetime since it was last successfully used. Logs a critical warning
        as a side effect, so a background loop can just poll this rather than
        every caller needing its own logging.
        """
        if self.refresh_token_issued_at is None:
            return False  # never successfully used in this process yet — nothing to warn about
        remaining = self.REFRESH_TOKEN_LIFETIME - (datetime.now(UTC) - self.refresh_token_issued_at)
        expiring_soon = remaining < self.REFRESH_TOKEN_WARNING_WINDOW
        if expiring_soon:
            logger.critical(
                "schwab_refresh_token_expiring_soon",
                remaining_seconds=max(remaining.total_seconds(), 0),
            )
        return expiring_soon

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

    def _access_token_is_valid(self) -> bool:
        return bool(
            self.access_token and self.access_token_expires_at and datetime.now(UTC) < self.access_token_expires_at
        )

    async def get_access_token(self) -> str:
        """
        Return a valid access token, refreshing it when necessary. Serialized: this
        client is now shared process-wide (see factory.py), so several tasks can find
        the token expired at the same moment — without the lock each would call the
        token endpoint. The first refreshes; the rest wait and reuse its result.
        """
        if self._access_token_is_valid():
            return self.access_token or ""
        if self._refresh_lock is None:
            self._refresh_lock = asyncio.Lock()
        async with self._refresh_lock:
            if self._access_token_is_valid():  # someone else refreshed while we waited
                return self.access_token or ""
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
        # A successful exchange — whether or not Schwab rotated the refresh
        # token itself — resets its ~7-day inactivity clock (see
        # REFRESH_TOKEN_LIFETIME above).
        self.refresh_token_issued_at = datetime.now(UTC)
        expires_in = int(data.get("expires_in", 1800))
        self.access_token_expires_at = datetime.now(UTC) + timedelta(seconds=max(expires_in - 60, 0))
        if self.token_file:
            self._persist_refresh_token()