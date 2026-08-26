"""Account-scoped Schwab Trader API adapter.

This adapter intentionally provides only read-only calls and order preview. Live
order submission is blocked until a separately reviewed release enables it.
"""

from datetime import UTC, datetime
from typing import Any, Optional

import httpx

from src.accounts.profiles import AccountProfile
from src.brokers.base import BrokerAdapter, BrokerError, LiveTradingDisabledError
from src.brokers.schwab.auth import SchwabOAuthClient


class SchwabBrokerAdapter(BrokerAdapter):
    """Schwab Trader API implementation using account hashes, never raw account numbers."""

    BASE_URL = "https://api.schwabapi.com/trader/v1"
    # Schwab's real-time quotes live under a separate market-data API
    # product from the trader/account endpoints above — a distinct base
    # path, and in practice a distinct subscription/entitlement on the
    # developer app. A 401/403 from get_quote() most likely means that
    # product hasn't been enabled for this app registration specifically,
    # not that the access token itself is bad.
    MARKET_DATA_BASE_URL = "https://api.schwabapi.com/marketdata/v1"

    def __init__(
        self,
        oauth: SchwabOAuthClient,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.oauth = oauth
        self.transport = transport

    async def list_accounts(self) -> list[dict[str, Any]]:
        response = await self._request("GET", "/accounts")
        return response if isinstance(response, list) else response.get("accounts", [])

    async def resolve_account_hash(self, account_number: str) -> str:
        """Resolve an account number through the Schwab accounts endpoint."""
        for account in await self.list_accounts():
            securities_account = account.get("securitiesAccount", account)
            if securities_account.get("accountNumber") == account_number:
                account_hash = securities_account.get("hashValue") or securities_account.get("accountHash")
                if account_hash:
                    return account_hash
        raise BrokerError("No accessible Schwab account matches the configured account number")

    async def preview_order(self, profile: AccountProfile, order_spec: dict[str, Any]) -> dict[str, Any]:
        account_hash = self._account_hash(profile)
        return await self._request("POST", f"/accounts/{account_hash}/previewOrder", json=order_spec)

    async def submit_order(self, profile: AccountProfile, order_spec: dict[str, Any]) -> dict[str, Any]:
        raise LiveTradingDisabledError(
            "Live Schwab order submission is intentionally disabled; use PAPER mode until separately enabled"
        )

    async def get_order_status(self, profile: AccountProfile, order_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/accounts/{self._account_hash(profile)}/orders/{order_id}")

    async def get_positions(self, profile: AccountProfile) -> list[dict[str, Any]]:
        account = await self._request("GET", f"/accounts/{self._account_hash(profile)}", params={"fields": "positions"})
        return account.get("securitiesAccount", account).get("positions", [])

    async def get_balances(self, profile: AccountProfile) -> dict[str, Any]:
        account = await self._request("GET", f"/accounts/{self._account_hash(profile)}")
        return account.get("securitiesAccount", account).get("currentBalances", {})

    async def get_quote(self, symbol: str) -> dict[str, Any]:
        response = await self._request(
            "GET", f"/{symbol}/quotes", base_url=self.MARKET_DATA_BASE_URL
        )
        # Schwab nests the quote under the symbol key; unwrap defensively
        # since sandbox/live payload shapes have been known to drift.
        payload = response.get(symbol, response) if isinstance(response, dict) else {}
        quote_data = payload.get("quote", payload)

        quote_time_ms = quote_data.get("quoteTime") or quote_data.get("tradeTime")
        quote_time = (
            datetime.fromtimestamp(quote_time_ms / 1000, tz=UTC).isoformat()
            if quote_time_ms
            else datetime.now(UTC).isoformat()
        )

        return {
            "symbol": symbol,
            "bid": quote_data.get("bidPrice"),
            "ask": quote_data.get("askPrice"),
            "last": quote_data.get("lastPrice"),
            "quote_time": quote_time,
            "mode": "LIVE",
        }

    @staticmethod
    def _account_hash(profile: AccountProfile) -> str:
        if not profile.account_hash:
            raise BrokerError("Schwab account profile requires a resolved account hash")
        return profile.account_hash

    async def _request(
        self, method: str, path: str, base_url: Optional[str] = None, **kwargs: Any
    ) -> dict[str, Any] | list[dict[str, Any]]:
        token = await self.oauth.get_access_token()
        async with httpx.AsyncClient(transport=self.transport) as client:
            response = await client.request(
                method,
                f"{base_url or self.BASE_URL}{path}",
                headers={"Authorization": f"Bearer {token}"},
                **kwargs,
            )
            response.raise_for_status()
            if not response.content:
                return {}
            return response.json()