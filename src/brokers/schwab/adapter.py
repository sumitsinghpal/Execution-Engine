"""Account-scoped Schwab Trader API adapter.

This adapter intentionally provides only read-only calls and order preview. Live
order submission is blocked until a separately reviewed release enables it.
"""

import asyncio
from datetime import UTC, datetime
from typing import Any, Optional

import httpx

from src.accounts.profiles import AccountProfile
from src.brokers.base import BrokerAdapter, BrokerAPIOutageError, BrokerError, LiveTradingDisabledError
from src.brokers.schwab.auth import SchwabOAuthClient
from src.logging_config import get_logger

logger = get_logger(__name__)


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
        timeout_sec: float = 30.0,
        retry_max_attempts: int = 3,
        retry_backoff_sec: float = 1.0,
        account_number: Optional[str] = None,
    ) -> None:
        self.oauth = oauth
        self.transport = transport
        self.timeout_sec = timeout_sec
        self.retry_max_attempts = max(retry_max_attempts, 1)
        self.retry_backoff_sec = retry_backoff_sec
        # The raw Schwab account number to auto-resolve into a hash on first
        # use, for a profile that was registered (e.g. via
        # Settings.schwab_account_number) without a pre-resolved
        # account_hash. Not the account_hash itself — Schwab account hashes
        # are looked up, never configured directly, so operators only ever
        # need to supply the plain account number they already know.
        self.account_number = account_number
        self._resolved_account_hash: Optional[str] = None

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
        account_hash = await self._resolve_account_hash(profile)
        return await self._request("POST", f"/accounts/{account_hash}/previewOrder", json=order_spec)

    async def submit_order(self, profile: AccountProfile, order_spec: dict[str, Any]) -> dict[str, Any]:
        raise LiveTradingDisabledError(
            "Live Schwab order submission is intentionally disabled; use PAPER mode until separately enabled"
        )

    async def get_order_status(self, profile: AccountProfile, order_id: str) -> dict[str, Any]:
        account_hash = await self._resolve_account_hash(profile)
        return await self._request("GET", f"/accounts/{account_hash}/orders/{order_id}")

    async def get_positions(self, profile: AccountProfile) -> list[dict[str, Any]]:
        account_hash = await self._resolve_account_hash(profile)
        account = await self._request("GET", f"/accounts/{account_hash}", params={"fields": "positions"})
        return account.get("securitiesAccount", account).get("positions", [])

    async def get_balances(self, profile: AccountProfile) -> dict[str, Any]:
        account_hash = await self._resolve_account_hash(profile)
        account = await self._request("GET", f"/accounts/{account_hash}")
        balances = account.get("securitiesAccount", account).get("currentBalances", {})
        # Normalize a broker-neutral "net_liquidation_value" key alongside
        # Schwab's own field names, so DrawdownGuard doesn't need to know
        # per-broker balance schemas.
        if "net_liquidation_value" not in balances and "liquidationValue" in balances:
            balances = {**balances, "net_liquidation_value": balances["liquidationValue"]}
        return balances

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

    async def get_price_history(self, symbol: str, bar_interval: str, lookback_days: int) -> list[dict[str, Any]]:
        """
        Real historical OHLCV candles from Schwab's price-history endpoint
        — feeds src/strategy's indicator calculations with genuine market
        data rather than a synthetic series once Schwab is configured.
        """
        if bar_interval == "5min":
            params = {"symbol": symbol, "periodType": "day", "period": "1", "frequencyType": "minute", "frequency": "5"}
        else:
            # Schwab's yearly period buckets are coarse (1/2/3/5/10/15/20);
            # round up so a strategy asking for e.g. 260 days of daily bars
            # (Golden Cross needs 200+) still gets enough history.
            years = max(1, -(-lookback_days // 365))
            params = {
                "symbol": symbol,
                "periodType": "year",
                "period": str(years),
                "frequencyType": "daily",
                "frequency": "1",
            }

        # Unlike get_quote() (path-based, /{symbol}/quotes), Schwab's price
        # history endpoint takes the symbol as a query parameter.
        response = await self._request(
            "GET", "/pricehistory", base_url=self.MARKET_DATA_BASE_URL, params=params
        )
        candles = response.get("candles", []) if isinstance(response, dict) else []

        bars = [
            {
                "timestamp": datetime.fromtimestamp(c["datetime"] / 1000, tz=UTC).isoformat(),
                "open": c.get("open"),
                "high": c.get("high"),
                "low": c.get("low"),
                "close": c.get("close"),
                "volume": c.get("volume", 0),
            }
            for c in candles
            if "datetime" in c
        ]
        if bar_interval != "5min" and len(bars) > lookback_days:
            bars = bars[-lookback_days:]
        return bars

    async def _resolve_account_hash(self, profile: AccountProfile) -> str:
        """
        A pre-resolved account_hash on the profile always wins. Otherwise,
        if this adapter was constructed with a plain account_number (see
        Settings.schwab_account_number), resolve it via the Schwab accounts
        endpoint on first use and cache the result for the lifetime of this
        adapter instance — so an operator only ever has to configure the
        account number they already know, never a hash they'd have to look
        up by hand.
        """
        if profile.account_hash:
            return profile.account_hash
        if self._resolved_account_hash:
            return self._resolved_account_hash
        if not self.account_number:
            raise BrokerError(
                "Schwab account profile requires a resolved account hash — configure either "
                "AccountProfile.account_hash directly, or SCHWAB_ACCOUNT_NUMBER so it can be "
                "resolved automatically"
            )
        self._resolved_account_hash = await self.resolve_account_hash(self.account_number)
        return self._resolved_account_hash

    async def _request(
        self, method: str, path: str, base_url: Optional[str] = None, **kwargs: Any
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """
        Issues one Schwab API call with an explicit timeout and retry-with-
        backoff on transient failures (connection errors, timeouts, and 5xx
        responses). A 4xx response is never retried — it means the request
        itself was wrong, not that Schwab is having a bad moment — and
        raises immediately. All retries exhausted raises
        BrokerAPIOutageError so callers (Executor) can distinguish "Schwab
        is down" from a normal request-shaped error.
        """
        token = await self.oauth.get_access_token()
        url = f"{base_url or self.BASE_URL}{path}"
        last_exc: Optional[Exception] = None

        for attempt in range(1, self.retry_max_attempts + 1):
            try:
                async with httpx.AsyncClient(transport=self.transport, timeout=self.timeout_sec) as client:
                    response = await client.request(
                        method,
                        url,
                        headers={"Authorization": f"Bearer {token}"},
                        **kwargs,
                    )

                if response.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"Schwab returned {response.status_code}", request=response.request, response=response
                    )
                response.raise_for_status()  # 4xx raises here, not retried below

                if not response.content:
                    return {}
                return response.json()

            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                is_client_error = isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500
                if is_client_error:
                    raise BrokerError(f"Schwab rejected the request ({method} {path}): {exc}") from exc

                last_exc = exc
                if attempt < self.retry_max_attempts:
                    backoff = self.retry_backoff_sec * (2 ** (attempt - 1))
                    logger.warning(
                        "schwab_request_retrying",
                        method=method,
                        path=path,
                        attempt=attempt,
                        max_attempts=self.retry_max_attempts,
                        backoff_sec=backoff,
                        error=str(exc),
                    )
                    await asyncio.sleep(backoff)

        logger.critical(
            "schwab_api_outage",
            method=method,
            path=path,
            attempts=self.retry_max_attempts,
            error=str(last_exc),
        )
        raise BrokerAPIOutageError(
            f"Schwab API unreachable after {self.retry_max_attempts} attempts "
            f"({method} {path}): {last_exc}"
        )