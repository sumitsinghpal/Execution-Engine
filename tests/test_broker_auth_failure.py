"""
Tests for broker-authentication-failure handling: a rejected/expired Schwab
refresh token must (a) surface as a specific, distinguishable error rather
than a generic broker failure, and (b) automatically halt trading via the
kill switch rather than leaving the system to keep accepting new requests
that will fail identically until a human re-authenticates.
"""

import httpx
import pytest

from src.brokers.base import BrokerAuthenticationError
from src.brokers.schwab.auth import SchwabOAuthClient
from src.execution.executor import Executor
from src.execution.kill_switch_state import KillSwitchService
from src.models.orders import AssetType, Instruction, OrderType, TradeProposal


def expired_refresh_token_transport(request: httpx.Request) -> httpx.Response:
    """Simulates Schwab rejecting a refresh token that has expired or been revoked."""
    if request.url.path == "/v1/oauth/token":
        return httpx.Response(401, json={"error": "invalid_grant", "error_description": "refresh token expired"})
    return httpx.Response(404, json={"error": "unexpected mock route"})


class FakeAuthFailingBroker:
    """Minimal broker stand-in whose every call raises BrokerAuthenticationError."""

    async def preview_order(self, profile, order_spec):
        raise BrokerAuthenticationError("refresh token expired (fake)")

    async def submit_order(self, profile, order_spec):
        raise BrokerAuthenticationError("refresh token expired (fake)")

    async def get_order_status(self, profile, order_id):
        raise BrokerAuthenticationError("refresh token expired (fake)")

    async def list_accounts(self):
        raise BrokerAuthenticationError("refresh token expired (fake)")

    async def get_positions(self, profile):
        raise BrokerAuthenticationError("refresh token expired (fake)")

    async def get_balances(self, profile):
        raise BrokerAuthenticationError("refresh token expired (fake)")


@pytest.mark.asyncio
async def test_oauth_client_raises_specific_error_on_expired_refresh_token():
    """
    A 401 from Schwab's token endpoint must raise BrokerAuthenticationError
    specifically, not a generic httpx.HTTPStatusError — callers need to be
    able to distinguish "credentials are dead, stop retrying" from a
    transient network/5xx failure worth retrying.
    """
    transport = httpx.MockTransport(expired_refresh_token_transport)
    oauth = SchwabOAuthClient(
        app_key="test-key",
        app_secret="test-secret",
        redirect_uri="https://127.0.0.1/callback",
        refresh_token="expired-refresh-token",
        transport=transport,
    )

    with pytest.raises(BrokerAuthenticationError):
        await oauth.get_access_token()


@pytest.mark.asyncio
async def test_preview_auto_engages_kill_switch_on_broker_auth_failure(test_db_engine_and_session):
    """
    When the broker rejects our credentials mid-preview, Executor must
    automatically trip the kill switch — not just let the exception surface
    and leave the system accepting the next request as if nothing failed.
    """
    _, session = test_db_engine_and_session
    executor = Executor(session=session, broker=FakeAuthFailingBroker())

    assert KillSwitchService(session).is_enabled() is False

    proposal = TradeProposal(
        decision_id="auth-failure-test-001",
        account="primary",
        symbol="QQQ",
        asset_type=AssetType.ETF,
        instruction=Instruction.BUY,
        quantity=10,
        order_type=OrderType.MARKET,
    )

    try:
        with pytest.raises(BrokerAuthenticationError):
            await executor.preview_order(proposal)

        assert KillSwitchService(session).is_enabled() is True, (
            "kill switch should auto-engage after a broker authentication failure"
        )
    finally:
        # The kill switch is a global singleton shared across the whole test
        # session (see test_position_reconciliation.py's module docstring
        # for why) — leave it clean for whatever test runs next.
        KillSwitchService(session).set_state(enabled=False, set_by="test_cleanup")
