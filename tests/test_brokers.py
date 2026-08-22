"""Tests for broker-neutral adapters; all Schwab interactions use mocked transport."""

import json

import httpx
import pytest

from src.accounts.profiles import AccountProfile, BrokerName
from src.brokers.base import LiveTradingDisabledError
from src.brokers.paper import PaperBrokerAdapter
from src.brokers.schwab.adapter import SchwabBrokerAdapter
from src.brokers.schwab.auth import SchwabOAuthClient


def schwab_transport(request: httpx.Request) -> httpx.Response:
    """Return deterministic Schwab responses without a network connection."""
    if request.url.path == "/v1/oauth/token":
        return httpx.Response(200, json={"access_token": "access-token", "expires_in": 1800})
    if request.url.path == "/trader/v1/accounts":
        return httpx.Response(
            200,
            json=[{"securitiesAccount": {"accountNumber": "1234", "hashValue": "account-hash"}}],
        )
    if request.url.path == "/trader/v1/accounts/account-hash/previewOrder":
        return httpx.Response(200, json={"status": "OK", "estimatedCommission": 0, "estimatedTotalInvestment": 10})
    if request.url.path == "/trader/v1/accounts/account-hash":
        fields = request.url.params.get("fields")
        if fields == "positions":
            return httpx.Response(200, json={"securitiesAccount": {"positions": [{"symbol": "QQQ"}]}})
        return httpx.Response(200, json={"securitiesAccount": {"currentBalances": {"cashAvailableForTrading": 1000}}})
    if request.url.path == "/trader/v1/accounts/account-hash/orders/42":
        return httpx.Response(200, json={"orderId": "42", "status": "WORKING"})
    return httpx.Response(404, json={"error": "unexpected mock route"})


@pytest.fixture
def schwab_adapter() -> SchwabBrokerAdapter:
    """Create the adapter with its entire transport mocked."""
    transport = httpx.MockTransport(schwab_transport)
    oauth = SchwabOAuthClient(
        app_key="test-app-key",
        app_secret="test-app-secret",
        redirect_uri="https://localhost/callback",
        refresh_token="test-refresh-token",
        transport=transport,
    )
    return SchwabBrokerAdapter(oauth, transport=transport)


@pytest.fixture
def schwab_profile() -> AccountProfile:
    """Return a resolved, non-live Schwab account profile."""
    return AccountProfile(
        broker=BrokerName.SCHWAB,
        credential_profile="schwab_main",
        account_hash="account-hash",
        live_enabled=False,
    )


@pytest.mark.asyncio
async def test_paper_adapter_simulates_preview_and_submission() -> None:
    """Paper mode supports the full safe execution workflow without I/O."""
    adapter = PaperBrokerAdapter()
    profile = AccountProfile(broker=BrokerName.PAPER)
    order = {"orderId": "decision-1", "quantity": 2, "limitPrice": "10", "symbol": "QQQ"}

    preview = await adapter.preview_order(profile, order)
    receipt = await adapter.submit_order(profile, order)

    assert preview["mode"] == "PAPER"
    assert receipt["orderId"] == "paper-order-decision-1"


@pytest.mark.asyncio
async def test_schwab_read_only_calls_and_preview_use_account_hash(
    schwab_adapter: SchwabBrokerAdapter, schwab_profile: AccountProfile
) -> None:
    """Schwab requests use the documented Trader API account-hash paths."""
    assert await schwab_adapter.resolve_account_hash("1234") == "account-hash"
    preview = await schwab_adapter.preview_order(schwab_profile, {"orderId": "decision-1"})
    positions = await schwab_adapter.get_positions(schwab_profile)
    balances = await schwab_adapter.get_balances(schwab_profile)
    status = await schwab_adapter.get_order_status(schwab_profile, "42")

    assert preview["status"] == "OK"
    assert positions == [{"symbol": "QQQ"}]
    assert balances["cashAvailableForTrading"] == 1000
    assert status["status"] == "WORKING"


@pytest.mark.asyncio
async def test_schwab_live_submission_is_hard_blocked(
    schwab_adapter: SchwabBrokerAdapter, schwab_profile: AccountProfile
) -> None:
    """No code path may submit a live Schwab order in this release."""
    with pytest.raises(LiveTradingDisabledError):
        await schwab_adapter.submit_order(schwab_profile, {"orderId": "decision-1"})


def test_schwab_oauth_authorization_url_is_explicit() -> None:
    """The initial authorization-code bootstrap has an inspectable redirect URL."""
    oauth = SchwabOAuthClient("app-key", "secret", "https://localhost/callback")

    assert oauth.authorization_url("csrf-state") == (
        "https://api.schwabapi.com/v1/oauth/authorize?client_id=app-key&"
        "redirect_uri=https%3A%2F%2Flocalhost%2Fcallback&state=csrf-state"
    )