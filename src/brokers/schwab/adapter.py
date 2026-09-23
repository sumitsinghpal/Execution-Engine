"""Account-scoped Schwab Trader API adapter.

submit_order() places a REAL order against a REAL Schwab account — real money,
not a simulation — enabled deliberately after LiveTradingDisabledError was
removed following an explicit decision to turn this on (see the git history
for src/brokers/schwab/adapter.py around that change). Everything else in this
adapter (quotes, positions, balances, preview) was already live-data-capable
before that; submission was the one call intentionally held back.
"""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Optional
from urllib.parse import quote as url_quote

import httpx

from src.accounts.profiles import AccountProfile
from src.brokers.base import (
    BrokerAdapter,
    BrokerAPIOutageError,
    BrokerError,
    BrokerRateLimitError,
    LiveTradingDisabledError,
)
from src.brokers.schwab.auth import SchwabOAuthClient
from src.brokers.schwab.order_translation import extract_order_value, normalize_preview, to_schwab_order
from src.brokers.schwab.rate_limit import RateLimiter
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
        rate_limit_per_minute: int = 120,
        max_retry_after_sec: float = 30.0,
        limiter: Optional[RateLimiter] = None,
    ) -> None:
        self.oauth = oauth
        self.transport = transport
        self.timeout_sec = timeout_sec
        self.retry_max_attempts = max(retry_max_attempts, 1)
        self.retry_backoff_sec = retry_backoff_sec
        # Schwab's published limit is 120 calls/minute per app. Every HTTP attempt —
        # including a retry — takes a slot, so a burst waits instead of tripping a 429.
        # Only meaningful if this adapter is long-lived and shared: see factory.py.
        self._limiter = limiter or RateLimiter(max_calls=max(rate_limit_per_minute, 1))
        # Cap on how long a broker-supplied Retry-After may make us sleep in-line.
        self.max_retry_after_sec = max_retry_after_sec
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
        """
        Resolve an account number to its hash via GET /accounts/accountNumbers,
        which is where Schwab publishes the {accountNumber, hashValue} pairs. (This
        used to scan GET /accounts for a `hashValue`, a field that endpoint does not
        carry, so resolution would have failed on the first real call.)
        """
        numbers = await self._request("GET", "/accounts/accountNumbers")
        for entry in numbers if isinstance(numbers, list) else []:
            if str(entry.get("accountNumber")) == str(account_number) and entry.get("hashValue"):
                return entry["hashValue"]
        raise BrokerError("No accessible Schwab account matches the configured account number")

    async def preview_order(self, profile: AccountProfile, order_spec: dict[str, Any]) -> dict[str, Any]:
        """
        Translate the broker-neutral spec into Schwab's nested order format (fail
        closed on anything untranslatable), ask Schwab to preview it, and reshape
        the answer into the keys Executor reads — including surfacing anything
        Schwab rejected so it cannot be shown to a human as "approved".
        """
        schwab_order = to_schwab_order(order_spec)
        account_hash = await self._resolve_account_hash(profile)
        response = await self._request("POST", f"/accounts/{account_hash}/previewOrder", json=schwab_order)
        value = extract_order_value(response)
        source = "schwab"
        if value is None:
            value, source = await self._local_estimate(order_spec), "local_estimate"
        return normalize_preview(response, value, source)

    async def _local_estimate(self, order_spec: dict[str, Any]) -> float:
        """Price x quantity x multiplier, used only when Schwab's preview carries no order value — never a silent $0."""
        price = order_spec.get("limitPrice") or order_spec.get("stopPrice")
        if price in (None, ""):
            quote = await self.get_quote(order_spec["symbol"])
            side_price = quote.get("ask") if order_spec.get("instruction") == "BUY" else quote.get("bid")
            price = side_price or quote.get("last")
        if not price:
            raise BrokerError("Cannot estimate the order value: Schwab's preview had none and no price is available")
        multiplier = 100 if order_spec.get("assetType") == "OPTION" else 1
        return float(Decimal(str(price)) * Decimal(order_spec["quantity"]) * multiplier)

    async def submit_order(self, profile: AccountProfile, order_spec: dict[str, Any]) -> dict[str, Any]:
        """
        Submit a LIVE order to Schwab — real money, real fills. Schwab's order
        endpoint returns HTTP 201 with an EMPTY body on success; the new order's
        ID lives only in the Location response header
        (".../accounts/{hash}/orders/{orderId}"), so this is the one call site
        that needs _request()'s raw headers instead of just its JSON body.

        Deliberately reports status "SUBMITTED" only, with no filledQuantity or
        averageFillPrice — unlike PaperBrokerAdapter (see its submit_order()
        docstring), a real broker never fills synchronously at submission time.
        Executor.execute_order() only advances an order past SUBMITTED when a
        broker response says so; a real fill is discovered later by
        PositionReconciliationService polling get_order_status(). Inventing a
        fill here would be reporting a trade result this call never actually
        observed.

        Gated independently of everything else that must already be true to
        reach this line (execution_mode not PAPER/SHADOW, a resolved Schwab
        profile, a valid refresh token): this specific account profile's own
        live_enabled must also be True. Two switches, not one — enabling Schwab
        for read-only data/preview does not, by itself, ever permit a real
        order for any account until this is opted into explicitly too.
        """
        if not profile.live_enabled:
            raise LiveTradingDisabledError(
                f"Live order submission is not enabled for this account profile "
                f"(credential_profile={profile.credential_profile!r}) — set live_enabled=True "
                f"(SCHWAB_LIVE_TRADING_ENABLED=true for the auto-registered alias) to allow it."
            )
        schwab_order = to_schwab_order(order_spec)
        account_hash = await self._resolve_account_hash(profile)
        _, headers = await self._request(
            "POST", f"/accounts/{account_hash}/orders", json=schwab_order, return_headers=True
        )
        order_id = self._extract_order_id_from_location(headers.get("Location"))
        logger.warning(
            "schwab_live_order_submitted",
            account_hash=account_hash,
            symbol=order_spec.get("symbol"),
            instruction=order_spec.get("instruction"),
            quantity=order_spec.get("quantity"),
            broker_order_id=order_id,
        )
        return {
            "orderId": order_id,
            "status": "SUBMITTED",
            "symbol": order_spec.get("symbol"),
            "quantity": order_spec.get("quantity"),
            "enteredTime": datetime.now(UTC).isoformat(),
            "mode": "LIVE",
        }

    @staticmethod
    def _extract_order_id_from_location(location: Optional[str]) -> str:
        """
        Schwab's only success signal for order submission — no body, just this
        header. A missing or unparseable one means Schwab may have accepted a
        real order we now have no ID for, which is worse than a loud failure
        here: raise rather than return a guessed or empty ID that would silently
        corrupt the audit trail for a trade that actually happened.
        """
        if not location:
            raise BrokerError("Schwab accepted the order but returned no Location header to identify it")
        order_id = location.rsplit("/", 1)[-1]
        if not order_id:
            raise BrokerError(f"Could not parse an order ID from Schwab's Location header: {location!r}")
        return order_id

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
        # OCC option symbols contain literal spaces (e.g. "NVDA  280121C00120000"),
        # which aren't valid raw in a URL path — percent-encode the whole
        # segment (plain equity tickers pass through unchanged).
        response = await self._request(
            "GET", f"/{url_quote(symbol, safe='')}/quotes", base_url=self.MARKET_DATA_BASE_URL
        )
        # Schwab nests the quote under the symbol key; unwrap defensively
        # since sandbox/live payload shapes have been known to drift.
        payload = response.get(symbol, response) if isinstance(response, dict) else {}
        return self._parse_quote(symbol, payload)

    @staticmethod
    def _parse_quote(symbol: str, payload: dict[str, Any]) -> dict[str, Any]:
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

    QUOTE_BATCH_SIZE = 50

    async def get_quotes(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        """
        Many symbols in ONE call (GET /quotes?symbols=A,B,C) instead of one call per
        symbol — the difference between one dashboard refresh costing 1 of the 120
        calls/minute and costing up to 50. Returns {symbol: quote}, or
        {symbol: {"error": ...}} for any symbol Schwab did not return (an invalid
        ticker is one symbol's problem, never the batch's). A broker-level failure
        (outage, rate limit) raises, since it affects every symbol equally.

        Not yet exercised against live Schwab: the batch response shape and the
        `errors.invalidSymbols` field follow Schwab's documentation.
        """
        unique = list(dict.fromkeys(symbol for symbol in symbols if symbol))
        result: dict[str, dict[str, Any]] = {}
        for start in range(0, len(unique), self.QUOTE_BATCH_SIZE):
            chunk = unique[start : start + self.QUOTE_BATCH_SIZE]
            response = await self._request(
                "GET", "/quotes", base_url=self.MARKET_DATA_BASE_URL, params={"symbols": ",".join(chunk)}
            )
            payload = response if isinstance(response, dict) else {}
            invalid = set(((payload.get("errors") or {}).get("invalidSymbols")) or [])
            for symbol in chunk:
                entry = payload.get(symbol)
                if isinstance(entry, dict) and symbol not in invalid:
                    result[symbol] = self._parse_quote(symbol, entry)
                else:
                    reason = "invalid symbol" if symbol in invalid else "not in Schwab's response"
                    result[symbol] = {"error": f"Schwab returned no quote for {symbol} ({reason})"}
        return result

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

    def _retry_after_seconds(self, response: httpx.Response) -> Optional[float]:
        """Schwab's own Retry-After hint in seconds, clamped to [0, max_retry_after_sec]; None if absent or not a number."""
        raw = response.headers.get("Retry-After")
        try:
            seconds = float(raw) if raw is not None else None
        except ValueError:
            return None  # an HTTP-date form: rare, and not worth a date parser — fall back to backoff
        if seconds is None or seconds < 0:
            return None
        return min(seconds, self.max_retry_after_sec)

    async def _request(
        self, method: str, path: str, base_url: Optional[str] = None, return_headers: bool = False, **kwargs: Any
    ) -> dict[str, Any] | list[dict[str, Any]] | tuple[dict[str, Any] | list[dict[str, Any]], httpx.Headers]:
        """
        Issues one Schwab API call with an explicit timeout and retry-with-
        backoff on transient failures (connection errors, timeouts, and 5xx
        responses). A 4xx response is never retried — it means the request
        itself was wrong, not that Schwab is having a bad moment — and
        raises immediately. All retries exhausted raises
        BrokerAPIOutageError so callers (Executor) can distinguish "Schwab
        is down" from a normal request-shaped error.

        HTTP 429 is the exception to "4xx is never retried": it means "too fast",
        not "wrong request". It is retried, sleeping for Schwab's own Retry-After
        (capped at max_retry_after_sec) when given, else the exponential backoff;
        if it never clears, BrokerRateLimitError (a BrokerAPIOutageError, so the
        existing "try again shortly" handling applies and the kill switch is NOT
        tripped). Every attempt first takes a slot from the shared rate limiter, and
        the access token is re-checked per attempt so one that expires during a
        long backoff is refreshed rather than reused.

        return_headers=True returns (body, response.headers) instead of just body —
        needed for submit_order(), whose only success signal is an HTTP 201 with an
        EMPTY body; the new order's ID lives solely in the Location header.
        """
        url = f"{base_url or self.BASE_URL}{path}"
        last_exc: Optional[Exception] = None
        last_was_rate_limit = False
        last_retry_after: Optional[float] = None

        for attempt in range(1, self.retry_max_attempts + 1):
            await self._limiter.acquire()
            token = await self.oauth.get_access_token()
            try:
                async with httpx.AsyncClient(transport=self.transport, timeout=self.timeout_sec) as client:
                    response = await client.request(
                        method,
                        url,
                        headers={"Authorization": f"Bearer {token}"},
                        **kwargs,
                    )

                if response.status_code == 429:
                    last_was_rate_limit = True
                    last_retry_after = self._retry_after_seconds(response)
                    last_exc = httpx.HTTPStatusError("Schwab returned 429", request=response.request, response=response)
                    if attempt < self.retry_max_attempts:
                        delay = (
                            last_retry_after
                            if last_retry_after is not None
                            else self.retry_backoff_sec * (2 ** (attempt - 1))
                        )
                        logger.warning(
                            "schwab_rate_limited",
                            method=method,
                            path=path,
                            attempt=attempt,
                            max_attempts=self.retry_max_attempts,
                            wait_sec=delay,
                        )
                        await asyncio.sleep(delay)
                    continue

                last_was_rate_limit = False
                if response.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"Schwab returned {response.status_code}", request=response.request, response=response
                    )
                response.raise_for_status()  # 4xx raises here, not retried below

                body: dict[str, Any] | list[dict[str, Any]] = {} if not response.content else response.json()
                return (body, response.headers) if return_headers else body

            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                is_client_error = isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500
                if is_client_error:
                    raise BrokerError(f"Schwab rejected the request ({method} {path}): {exc}") from exc

                last_exc = exc
                last_was_rate_limit = False  # this failure is a timeout/network/5xx, not a 429
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

        if last_was_rate_limit:
            logger.error("schwab_rate_limit_exhausted", method=method, path=path, attempts=self.retry_max_attempts)
            raise BrokerRateLimitError(
                f"Schwab kept rate-limiting us (HTTP 429) after {self.retry_max_attempts} attempts ({method} {path})",
                retry_after=last_retry_after,
            )
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