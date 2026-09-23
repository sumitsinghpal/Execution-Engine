"""
Tests for SchwabOAuthClient's refresh-token persistence and expiry warning.
All Schwab interactions use mocked transport — see tests/test_brokers.py.
"""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from src.brokers.schwab.auth import SchwabOAuthClient


def token_transport(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/v1/oauth/token":
        return httpx.Response(
            200, json={"access_token": "access-token", "refresh_token": "rotated-refresh-token", "expires_in": 1800}
        )
    return httpx.Response(404, json={"error": "unexpected mock route"})


def _client(**overrides) -> SchwabOAuthClient:
    defaults = dict(
        app_key="test-app-key",
        app_secret="test-app-secret",
        redirect_uri="https://localhost/callback",
        refresh_token="initial-refresh-token",
        transport=httpx.MockTransport(token_transport),
    )
    defaults.update(overrides)
    return SchwabOAuthClient(**defaults)


class TestNoPersistenceByDefault:
    def test_no_token_file_never_touches_disk(self, tmp_path, monkeypatch) -> None:
        """The default (token_file=None) must behave exactly as before this feature existed."""
        monkeypatch.chdir(tmp_path)
        client = _client()
        assert client.token_file is None
        # No stray file should appear anywhere from just constructing the client.
        assert list(tmp_path.iterdir()) == []


class TestPersistence:
    @pytest.mark.asyncio
    async def test_successful_exchange_persists_refresh_token(self, tmp_path) -> None:
        token_file = tmp_path / "token.json"
        client = _client(token_file=str(token_file))

        await client.get_access_token()

        assert token_file.exists()
        data = json.loads(token_file.read_text())
        assert data["refresh_token"] == "rotated-refresh-token"
        assert data["issued_at"] is not None

    def test_a_new_client_picks_up_the_persisted_token_over_the_configured_one(self, tmp_path) -> None:
        token_file = tmp_path / "token.json"
        token_file.write_text(json.dumps({"refresh_token": "persisted-token", "issued_at": None}))

        client = _client(refresh_token="stale-configured-token", token_file=str(token_file))

        assert client.refresh_token == "persisted-token"

    def test_missing_token_file_falls_back_to_the_configured_refresh_token(self, tmp_path) -> None:
        token_file = tmp_path / "does-not-exist.json"
        client = _client(refresh_token="configured-token", token_file=str(token_file))
        assert client.refresh_token == "configured-token"

    def test_corrupt_token_file_does_not_crash_construction(self, tmp_path) -> None:
        token_file = tmp_path / "token.json"
        token_file.write_text("not valid json {{{")
        client = _client(refresh_token="configured-token", token_file=str(token_file))
        assert client.refresh_token == "configured-token"


class TestExpiryWarning:
    def test_never_used_this_process_is_not_flagged(self) -> None:
        client = _client()
        assert client.refresh_token_issued_at is None
        assert client.is_refresh_token_expiring_soon() is False

    def test_freshly_issued_is_not_flagged(self) -> None:
        client = _client()
        client.refresh_token_issued_at = datetime.now(UTC)
        assert client.is_refresh_token_expiring_soon() is False

    def test_within_24h_of_the_7_day_window_is_flagged(self) -> None:
        client = _client()
        client.refresh_token_issued_at = datetime.now(UTC) - (
            SchwabOAuthClient.REFRESH_TOKEN_LIFETIME - timedelta(hours=1)
        )
        assert client.is_refresh_token_expiring_soon() is True

    def test_already_past_the_7_day_window_is_flagged(self) -> None:
        client = _client()
        client.refresh_token_issued_at = datetime.now(UTC) - timedelta(days=8)
        assert client.is_refresh_token_expiring_soon() is True

    @pytest.mark.asyncio
    async def test_a_successful_exchange_resets_the_warning_clock(self) -> None:
        client = _client()
        client.refresh_token_issued_at = datetime.now(UTC) - timedelta(days=8)
        assert client.is_refresh_token_expiring_soon() is True

        await client.get_access_token()

        assert client.is_refresh_token_expiring_soon() is False
