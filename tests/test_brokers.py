"""Tests for broker-neutral adapters; all Schwab interactions use mocked transport."""

import json

import httpx
import pytest

from src.accounts.profiles import AccountProfile, BrokerName
from src.brokers.base import BrokerAPIOutageError, BrokerError, LiveTradingDisabledError
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
    if request.url.path == "/marketdata/v1/QQQ/quotes":
        return httpx.Response(
            200,
            json={
                "QQQ": {
                    "quote": {
                        "bidPrice": 270.11,
                        "askPrice": 270.37,
                        "lastPrice": 270.24,
                        "quoteTime": 1735689600000,  # ms since epoch
                    }
                }
            },
        )
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
async def test_paper_adapter_quote_is_stable_and_fresh() -> None:
    """
    The paper broker's synthetic quote must be deterministic (same symbol,
    same price, every call — so a preview and a later execute for the same
    order see consistent numbers) and always timestamped as fresh.
    """
    adapter = PaperBrokerAdapter()

    quote1 = await adapter.get_quote("QQQ")
    quote2 = await adapter.get_quote("QQQ")

    assert quote1["last"] == quote2["last"]
    assert quote1["bid"] < quote1["last"] < quote1["ask"]
    assert "quote_time" in quote1


@pytest.mark.asyncio
async def test_paper_adapter_market_order_preview_does_not_estimate_zero() -> None:
    """
    A MARKET order (no limitPrice) must get its estimated investment from a
    live quote, not silently report $0 regardless of size — the same bug
    class as the notional-limit check bypass this preview feeds into.
    """
    adapter = PaperBrokerAdapter()
    profile = AccountProfile(broker=BrokerName.PAPER)
    order = {"orderId": "decision-2", "quantity": 100, "symbol": "QQQ"}  # no limitPrice

    preview = await adapter.preview_order(profile, order)

    assert preview["estimatedTotalInvestment"] > 0


@pytest.mark.asyncio
async def test_paper_adapter_estimated_investment_is_rounded_to_cents() -> None:
    """
    A limit price with 3+ decimal places (as a strategy-sourced entry price
    can have) must not leave a binary-float artifact like 5189.120000000001
    in estimatedTotalInvestment — that fails OrderPreview.estimated_cost's
    Decimal(decimal_places=2) validation outright downstream.
    """
    adapter = PaperBrokerAdapter()
    profile = AccountProfile(broker=BrokerName.PAPER)
    order = {"orderId": "decision-3", "quantity": 10, "symbol": "QQQ", "limitPrice": "518.912"}

    preview = await adapter.preview_order(profile, order)

    from decimal import Decimal
    Decimal(str(preview["estimatedTotalInvestment"])).quantize(Decimal("0.01"))  # must not raise
    assert preview["estimatedTotalInvestment"] == 5189.12


@pytest.mark.asyncio
async def test_paper_adapter_option_preview_applies_the_100x_contract_multiplier() -> None:
    """2 contracts at a $3.50 premium is $700 of real risk, not $7 — the same multiplier RiskChecker applies."""
    adapter = PaperBrokerAdapter()
    profile = AccountProfile(broker=BrokerName.PAPER)
    order = {
        "orderId": "decision-option-1",
        "quantity": 2,
        "symbol": "QQQ   261011C00400000",
        "assetType": "OPTION",
        "limitPrice": "3.50",
    }

    preview = await adapter.preview_order(profile, order)

    assert preview["estimatedTotalInvestment"] == 700.0


class TestPaperAdapterOptionQuotes:
    """PaperBrokerAdapter.get_quote() for an OCC option symbol — a distinct, plausible synthetic premium, not an equity-scale price."""

    @staticmethod
    def _occ(underlying="QQQ", days_out=45, right="C", strike="400"):
        from datetime import date, timedelta
        from decimal import Decimal

        from src.models.occ_symbol import format_occ_symbol

        return format_occ_symbol(underlying, date.today() + timedelta(days=days_out), right, Decimal(strike))

    @pytest.mark.asyncio
    async def test_option_quote_is_premium_scale_not_equity_scale(self):
        adapter = PaperBrokerAdapter()
        quote = await adapter.get_quote(self._occ())

        # Equity synthetic prices are ~$20-$500; a single option premium
        # should be nowhere near that (this repo's toy pricer caps out far
        # lower), so a value in the equity range would mean the OCC symbol
        # was hashed as if it were a plain ticker instead of parsed.
        assert 0 < quote["last"] < 200

    @pytest.mark.asyncio
    async def test_option_quote_has_a_wider_relative_spread_than_equities(self):
        adapter = PaperBrokerAdapter()
        equity_quote = await adapter.get_quote("QQQ")
        option_quote = await adapter.get_quote(self._occ())

        equity_spread_pct = (equity_quote["ask"] - equity_quote["bid"]) / equity_quote["last"]
        option_spread_pct = (option_quote["ask"] - option_quote["bid"]) / option_quote["last"]
        assert option_spread_pct > equity_spread_pct

    @pytest.mark.asyncio
    async def test_deeper_in_the_money_call_costs_more(self):
        adapter = PaperBrokerAdapter()
        # A very low strike call is deep ITM against whatever QQQ's
        # synthetic price is (~$20-$500 range); a very high strike call on
        # the same underlying/expiration is deep OTM.
        itm = await adapter.get_quote(self._occ(strike="1"))
        otm = await adapter.get_quote(self._occ(strike="5000"))

        assert itm["last"] > otm["last"]

    @pytest.mark.asyncio
    async def test_quote_is_deterministic_for_the_same_contract(self):
        adapter = PaperBrokerAdapter()
        first = await adapter.get_quote(self._occ())
        second = await adapter.get_quote(self._occ())
        assert first["last"] == second["last"]


class _FlakyThenOKTransport:
    """Fails with a 500 a fixed number of times, then succeeds — simulates a transient outage that clears."""

    def __init__(self, fail_times: int):
        self.fail_times = fail_times
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth/token":
            return httpx.Response(200, json={"access_token": "access-token", "expires_in": 1800})
        self.calls += 1
        if self.calls <= self.fail_times:
            return httpx.Response(500, json={"error": "internal server error"})
        return httpx.Response(200, json={"accounts": []})


class _AlwaysDownTransport:
    """Always returns a 503 — simulates a sustained outage that never clears within the retry budget."""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth/token":
            return httpx.Response(200, json={"access_token": "access-token", "expires_in": 1800})
        return httpx.Response(503, json={"error": "service unavailable"})


class _BadRequestTransport:
    """Always returns a 400 — a real client error that retrying can never fix."""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth/token":
            return httpx.Response(200, json={"access_token": "access-token", "expires_in": 1800})
        return httpx.Response(400, json={"error": "bad request"})


def _adapter_with_transport(transport_fn, retry_max_attempts=3, retry_backoff_sec=0.001) -> SchwabBrokerAdapter:
    transport = httpx.MockTransport(transport_fn)
    oauth = SchwabOAuthClient(
        app_key="test-app-key",
        app_secret="test-app-secret",
        redirect_uri="https://localhost/callback",
        refresh_token="test-refresh-token",
        transport=transport,
    )
    return SchwabBrokerAdapter(
        oauth,
        transport=transport,
        retry_max_attempts=retry_max_attempts,
        retry_backoff_sec=retry_backoff_sec,
    )


@pytest.mark.asyncio
async def test_transient_5xx_is_retried_and_recovers() -> None:
    """A 500 that clears within the retry budget succeeds transparently."""
    flaky = _FlakyThenOKTransport(fail_times=2)
    adapter = _adapter_with_transport(flaky, retry_max_attempts=3)

    result = await adapter.list_accounts()

    assert result == []
    assert flaky.calls == 3  # failed twice, succeeded on the 3rd


@pytest.mark.asyncio
async def test_sustained_outage_raises_after_exhausting_retries() -> None:
    """A 503 that never clears exhausts the retry budget and raises a distinct, catchable error."""
    adapter = _adapter_with_transport(_AlwaysDownTransport(), retry_max_attempts=3)

    with pytest.raises(BrokerAPIOutageError):
        await adapter.list_accounts()


@pytest.mark.asyncio
async def test_client_error_is_not_retried() -> None:
    """A 400 is a real request error, not an outage — it must fail immediately, not after 3 retries."""
    transport = _BadRequestTransport()
    original_call = transport.__call__
    call_count = {"n": 0}

    def counting_call(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/v1/oauth/token":
            call_count["n"] += 1
        return original_call(request)

    adapter = _adapter_with_transport(counting_call, retry_max_attempts=3)

    with pytest.raises(BrokerError):
        await adapter.list_accounts()

    assert call_count["n"] == 1, "a 4xx client error must not be retried"


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
async def test_schwab_get_quote_parses_marketdata_response(
    schwab_adapter: SchwabBrokerAdapter,
) -> None:
    """
    get_quote() hits the separate market-data API base path (not the
    trader/account one) and unwraps Schwab's per-symbol nested response
    shape into the flat dict RiskChecker expects.
    """
    quote = await schwab_adapter.get_quote("QQQ")

    assert quote["symbol"] == "QQQ"
    assert quote["bid"] == 270.11
    assert quote["ask"] == 270.37
    assert quote["last"] == 270.24
    assert "quote_time" in quote


@pytest.mark.asyncio
async def test_schwab_live_submission_is_hard_blocked(
    schwab_adapter: SchwabBrokerAdapter, schwab_profile: AccountProfile
) -> None:
    """No code path may submit a live Schwab order in this release."""
    with pytest.raises(LiveTradingDisabledError):
        await schwab_adapter.submit_order(schwab_profile, {"orderId": "decision-1"})


class TestSchwabAccountHashAutoResolution:
    """
    An operator only configures a plain account number (SCHWAB_ACCOUNT_NUMBER);
    the adapter resolves it into the hash Schwab's Trader API actually
    requires, lazily and once, so nothing has to look that hash up by hand.
    """

    @pytest.fixture
    def unresolved_profile(self) -> AccountProfile:
        """A profile with no pre-resolved account_hash — the auto-resolve path is what this exercises."""
        return AccountProfile(broker=BrokerName.SCHWAB, credential_profile="schwab_main", live_enabled=False)

    def _adapter(self, account_number: str = "1234") -> SchwabBrokerAdapter:
        transport = httpx.MockTransport(schwab_transport)
        oauth = SchwabOAuthClient(
            app_key="test-app-key",
            app_secret="test-app-secret",
            redirect_uri="https://localhost/callback",
            refresh_token="test-refresh-token",
            transport=transport,
        )
        return SchwabBrokerAdapter(oauth, transport=transport, account_number=account_number)

    @pytest.mark.asyncio
    async def test_get_balances_resolves_hash_from_configured_account_number(self, unresolved_profile) -> None:
        adapter = self._adapter()
        balances = await adapter.get_balances(unresolved_profile)
        assert balances["cashAvailableForTrading"] == 1000

    @pytest.mark.asyncio
    async def test_resolved_hash_is_cached_after_first_use(self, unresolved_profile) -> None:
        """A second call must not re-hit the /accounts endpoint to re-resolve the same hash."""
        adapter = self._adapter()
        await adapter.get_balances(unresolved_profile)
        assert adapter._resolved_account_hash == "account-hash"

        call_count = {"accounts": 0}

        def counting_transport(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/trader/v1/accounts":
                call_count["accounts"] += 1
            return schwab_transport(request)

        adapter.transport = httpx.MockTransport(counting_transport)
        await adapter.get_positions(unresolved_profile)

        assert call_count["accounts"] == 0, "a cached resolved hash must not re-query /accounts"

    @pytest.mark.asyncio
    async def test_profiles_own_account_hash_always_wins_over_resolution(self, schwab_profile) -> None:
        """A profile that already has account_hash set must never trigger a resolve call at all."""
        adapter = self._adapter(account_number="some-other-number-that-would-not-match")
        balances = await adapter.get_balances(schwab_profile)
        assert balances["cashAvailableForTrading"] == 1000

    @pytest.mark.asyncio
    async def test_no_account_hash_and_no_account_number_fails_closed(self, unresolved_profile) -> None:
        adapter = self._adapter(account_number=None)
        with pytest.raises(BrokerError, match="resolved account hash"):
            await adapter.get_balances(unresolved_profile)


def test_schwab_oauth_authorization_url_is_explicit() -> None:
    """The initial authorization-code bootstrap has an inspectable redirect URL."""
    oauth = SchwabOAuthClient("app-key", "secret", "https://localhost/callback")

    assert oauth.authorization_url("csrf-state") == (
        "https://api.schwabapi.com/v1/oauth/authorize?client_id=app-key&"
        "redirect_uri=https%3A%2F%2Flocalhost%2Fcallback&state=csrf-state"
    )