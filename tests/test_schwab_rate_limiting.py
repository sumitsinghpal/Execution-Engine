"""
Schwab publishes 120 calls/minute per app. Before this, nothing here limited
calls, a 429 was treated as a permanent client error (never retried, Retry-After
ignored), one dashboard refresh cost up to 50 calls, and ~20 call sites each built
a brand-new adapter — so even a limiter inside the adapter would have reset on
every call. See src/brokers/schwab/rate_limit.py and src/brokers/factory.py.
"""

import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from src.brokers.base import BrokerAPIOutageError, BrokerRateLimitError
from src.brokers.factory import build_broker_adapter, clear_broker_cache
from src.brokers.paper import PaperBrokerAdapter
from src.brokers.quotes import fetch_quotes
from src.brokers.schwab.adapter import SchwabBrokerAdapter
from src.brokers.schwab.auth import SchwabOAuthClient
from src.brokers.schwab.rate_limit import RateLimiter
from src.brokers.schwab_data_paper import SchwabDataPaperBroker
from src.config import Settings


def _oauth(transport, counter=None):
    def handler(request):
        if request.url.path == "/v1/oauth/token":
            if counter is not None:
                counter["token_calls"] += 1
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 1800})
        return transport(request)

    return SchwabOAuthClient("k", "s", "https://localhost/cb", refresh_token="r", transport=httpx.MockTransport(handler))


def _adapter(api_handler, **kwargs):
    counter = {"token_calls": 0}
    oauth = _oauth(api_handler, counter)

    def handler(request):
        if request.url.path == "/v1/oauth/token":
            counter["token_calls"] += 1
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 1800})
        return api_handler(request)

    transport = httpx.MockTransport(handler)
    oauth.transport = transport
    kwargs.setdefault("retry_backoff_sec", 1.0)
    adapter = SchwabBrokerAdapter(oauth, transport=transport, **kwargs)
    adapter.token_counter = counter
    return adapter


@pytest.fixture
def slept(monkeypatch):
    """Records every in-adapter sleep instead of really sleeping."""
    seconds = []

    async def fake_sleep(delay):
        seconds.append(delay)

    monkeypatch.setattr("src.brokers.schwab.adapter.asyncio.sleep", fake_sleep)
    return seconds


class TestRateLimiter:
    def make(self, max_calls=2, per=10.0):
        state = {"now": 0.0, "sleeps": []}

        async def sleep(delay):
            state["sleeps"].append(delay)
            state["now"] += delay

        return RateLimiter(max_calls, per, clock=lambda: state["now"], sleep=sleep), state

    @pytest.mark.asyncio
    async def test_calls_under_the_limit_never_wait(self):
        limiter, state = self.make(max_calls=3)
        for _ in range(3):
            await limiter.acquire()
        assert state["sleeps"] == []

    @pytest.mark.asyncio
    async def test_the_call_over_the_limit_waits_until_the_oldest_slot_frees(self):
        limiter, state = self.make(max_calls=2, per=10.0)
        for _ in range(3):
            await limiter.acquire()
        assert len(state["sleeps"]) == 1 and 9.9 <= state["sleeps"][0] <= 10.0

    @pytest.mark.asyncio
    async def test_slots_free_up_as_time_passes(self):
        limiter, state = self.make(max_calls=2, per=10.0)
        await limiter.acquire()
        await limiter.acquire()
        state["now"] += 10.0
        await limiter.acquire()
        assert state["sleeps"] == []

    @pytest.mark.asyncio
    async def test_concurrent_callers_cannot_all_claim_the_last_slot(self):
        limiter, state = self.make(max_calls=2, per=10.0)
        await asyncio.gather(*[limiter.acquire() for _ in range(6)])
        # 6 calls through a 2-per-10s window: two full waits are required, not zero.
        assert len(state["sleeps"]) >= 2 and state["now"] >= 20.0 - 0.01

    def test_a_limit_below_one_is_refused(self):
        with pytest.raises(ValueError):
            RateLimiter(max_calls=0)


class TestRateLimitedResponses:
    def three_calls(self, statuses, headers=None):
        seen = []

        def handler(request):
            status = statuses[min(len(seen), len(statuses) - 1)]
            seen.append(status)
            if status == 200:
                return httpx.Response(200, json=[{"accountNumber": "1", "hashValue": "H"}])
            return httpx.Response(status, headers=headers or {}, json={"error": "x"})

        return handler, seen

    @pytest.mark.asyncio
    async def test_a_429_is_retried_and_recovers(self, slept):
        handler, seen = self.three_calls([429, 429, 200])
        result = await _adapter(handler).resolve_account_hash("1")
        assert result == "H" and seen == [429, 429, 200]

    @pytest.mark.asyncio
    async def test_retry_after_is_honoured(self, slept):
        handler, _ = self.three_calls([429, 200], headers={"Retry-After": "7"})
        await _adapter(handler).resolve_account_hash("1")
        assert slept == [7.0]

    @pytest.mark.asyncio
    async def test_retry_after_is_capped_so_a_huge_hint_cannot_stall_us(self, slept):
        handler, _ = self.three_calls([429, 200], headers={"Retry-After": "3600"})
        await _adapter(handler, max_retry_after_sec=30.0).resolve_account_hash("1")
        assert slept == [30.0]

    @pytest.mark.asyncio
    async def test_no_retry_after_falls_back_to_exponential_backoff(self, slept):
        handler, _ = self.three_calls([429, 429, 200])
        await _adapter(handler, retry_backoff_sec=1.0).resolve_account_hash("1")
        assert slept == [1.0, 2.0]

    @pytest.mark.asyncio
    async def test_an_unparseable_retry_after_is_ignored_not_fatal(self, slept):
        handler, _ = self.three_calls([429, 200], headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
        await _adapter(handler, retry_backoff_sec=1.0).resolve_account_hash("1")
        assert slept == [1.0]

    @pytest.mark.asyncio
    async def test_a_429_that_never_clears_raises_a_rate_limit_error_that_is_still_an_outage(self, slept):
        handler, seen = self.three_calls([429], headers={"Retry-After": "2"})
        with pytest.raises(BrokerRateLimitError) as caught:
            await _adapter(handler, retry_max_attempts=3).resolve_account_hash("1")
        assert seen == [429, 429, 429] and caught.value.retry_after == 2.0
        # Subclass of the outage error on purpose: every existing "try again shortly"
        # handler (503, do NOT trip the kill switch) already applies to it.
        assert isinstance(caught.value, BrokerAPIOutageError)

    @pytest.mark.asyncio
    async def test_a_429_followed_by_a_server_error_is_an_outage_not_a_rate_limit(self, slept):
        handler, _ = self.three_calls([429, 500, 500])
        with pytest.raises(BrokerAPIOutageError) as caught:
            await _adapter(handler, retry_max_attempts=3).resolve_account_hash("1")
        assert not isinstance(caught.value, BrokerRateLimitError)

    @pytest.mark.asyncio
    async def test_other_4xx_are_still_never_retried(self, slept):
        handler, seen = self.three_calls([403])
        with pytest.raises(Exception):
            await _adapter(handler).resolve_account_hash("1")
        assert seen == [403] and slept == []


class TestLimiterIsUsedPerAttempt:
    class CountingLimiter:
        def __init__(self):
            self.acquired = 0

        async def acquire(self):
            self.acquired += 1

    @pytest.mark.asyncio
    async def test_every_attempt_takes_a_slot_including_retries(self, slept):
        seen = []

        def handler(request):
            seen.append(1)
            return httpx.Response(429) if len(seen) < 3 else httpx.Response(200, json=[{"accountNumber": "1", "hashValue": "H"}])

        limiter = self.CountingLimiter()
        await _adapter(handler, limiter=limiter).resolve_account_hash("1")
        assert limiter.acquired == 3  # a retry is still a call against Schwab's limit

    @pytest.mark.asyncio
    async def test_the_access_token_is_reused_across_attempts_not_refetched(self, slept):
        seen = []

        def handler(request):
            seen.append(1)
            return httpx.Response(429) if len(seen) < 3 else httpx.Response(200, json=[{"accountNumber": "1", "hashValue": "H"}])

        adapter = _adapter(handler)
        await adapter.resolve_account_hash("1")
        assert adapter.token_counter["token_calls"] == 1


def quotes_payload(symbols, invalid=()):
    body = {s: {"assetMainType": "EQUITY", "symbol": s, "quote": {"bidPrice": 9.9, "askPrice": 10.1, "lastPrice": 10.0, "quoteTime": 1735689600000}} for s in symbols}
    if invalid:
        body["errors"] = {"invalidSymbols": list(invalid)}
    return body


class TestBatchQuotes:
    @pytest.mark.asyncio
    async def test_many_symbols_are_one_http_call(self):
        calls = []

        def handler(request):
            calls.append((request.url.path, request.url.params["symbols"]))
            return httpx.Response(200, json=quotes_payload(["QQQ", "SPY", "IWM"]))

        quotes = await _adapter(handler).get_quotes(["QQQ", "SPY", "IWM"])
        assert calls == [("/marketdata/v1/quotes", "QQQ,SPY,IWM")]
        assert quotes["SPY"]["last"] == 10.0 and quotes["SPY"]["mode"] == "LIVE" and quotes["QQQ"]["bid"] == 9.9

    @pytest.mark.asyncio
    async def test_an_invalid_symbol_is_that_symbols_problem_only(self):
        def handler(request):
            return httpx.Response(200, json=quotes_payload(["QQQ"], invalid=["ZZZZ"]))

        quotes = await _adapter(handler).get_quotes(["QQQ", "ZZZZ"])
        assert quotes["QQQ"]["last"] == 10.0
        assert "invalid symbol" in quotes["ZZZZ"]["error"]

    @pytest.mark.asyncio
    async def test_a_symbol_missing_from_the_response_is_an_error_entry_not_a_crash(self):
        def handler(request):
            return httpx.Response(200, json=quotes_payload(["QQQ"]))

        quotes = await _adapter(handler).get_quotes(["QQQ", "SPY"])
        assert "error" in quotes["SPY"] and "last" in quotes["QQQ"]

    @pytest.mark.asyncio
    async def test_duplicates_are_collapsed(self):
        calls = []

        def handler(request):
            calls.append(request.url.params["symbols"])
            return httpx.Response(200, json=quotes_payload(["QQQ"]))

        await _adapter(handler).get_quotes(["QQQ", "QQQ", "QQQ"])
        assert calls == ["QQQ"]

    @pytest.mark.asyncio
    async def test_more_than_the_batch_size_is_split_into_chunks(self):
        calls = []
        symbols = [f"S{i:03d}" for i in range(120)]

        def handler(request):
            requested = request.url.params["symbols"].split(",")
            calls.append(len(requested))
            return httpx.Response(200, json=quotes_payload(requested))

        quotes = await _adapter(handler).get_quotes(symbols)
        assert calls == [50, 50, 20] and len(quotes) == 120

    @pytest.mark.asyncio
    async def test_a_broker_level_failure_raises_rather_than_pretending_each_symbol_failed_alone(self, slept):
        def handler(request):
            return httpx.Response(429)

        with pytest.raises(BrokerRateLimitError):
            await _adapter(handler, retry_max_attempts=2).get_quotes(["QQQ", "SPY"])

    @pytest.mark.asyncio
    async def test_the_single_quote_call_still_works_after_the_parsing_refactor(self):
        def handler(request):
            return httpx.Response(200, json={"QQQ": {"quote": {"bidPrice": 1.0, "askPrice": 2.0, "lastPrice": 1.5, "quoteTime": 1735689600000}}})

        quote = await _adapter(handler).get_quote("QQQ")
        assert (quote["symbol"], quote["bid"], quote["ask"], quote["last"], quote["mode"]) == ("QQQ", 1.0, 2.0, 1.5, "LIVE")


class FakeBatchBroker:
    def __init__(self, quotes=None, error=None):
        self.batches, self.singles, self._quotes, self._error = [], [], quotes or {}, error

    async def get_quotes(self, symbols):
        self.batches.append(list(symbols))
        if self._error:
            raise self._error
        return {s: self._quotes.get(s, {"error": "none"}) for s in symbols}

    async def get_quote(self, symbol):
        self.singles.append(symbol)
        raise AssertionError("must not fall back to per-symbol calls when the batch call exists")


class FakeSingleBroker:
    async def get_quote(self, symbol):
        if symbol == "BAD":
            raise RuntimeError("no such symbol")
        return {"symbol": symbol, "last": 1.0}


class TestFetchQuotes:
    @pytest.mark.asyncio
    async def test_uses_the_batch_call_when_the_broker_has_one(self):
        broker = FakeBatchBroker({"QQQ": {"symbol": "QQQ", "last": 5.0}})
        result = await fetch_quotes(broker, ["QQQ", "QQQ"])
        assert broker.batches == [["QQQ"]] and result["QQQ"]["last"] == 5.0

    @pytest.mark.asyncio
    async def test_a_failed_batch_marks_every_symbol_and_does_not_multiply_the_failing_calls(self):
        broker = FakeBatchBroker(error=BrokerRateLimitError("slow down"))
        result = await fetch_quotes(broker, ["A", "B", "C"])
        assert set(result) == {"A", "B", "C"} and all("slow down" in v["error"] for v in result.values())
        assert broker.singles == []

    @pytest.mark.asyncio
    async def test_a_broker_without_a_batch_call_falls_back_to_per_symbol_with_isolated_errors(self):
        result = await fetch_quotes(FakeSingleBroker(), ["QQQ", "BAD"])
        assert result["QQQ"]["last"] == 1.0 and "no such symbol" in result["BAD"]["error"]

    @pytest.mark.asyncio
    async def test_the_paper_broker_is_unaffected(self):
        result = await fetch_quotes(PaperBrokerAdapter(), ["QQQ", "SPY"])
        assert result["QQQ"]["mode"] == "PAPER" and result["SPY"]["last"] > 0

    @pytest.mark.asyncio
    async def test_empty_input_is_empty_output_and_no_calls(self):
        broker = FakeBatchBroker()
        assert await fetch_quotes(broker, []) == {} and broker.batches == []


class TestSchwabDataPaperBatch:
    class FakeSchwab:
        def __init__(self, result=None, error=None):
            self.result, self.error, self.calls = result or {}, error, 0

        async def get_quotes(self, symbols):
            self.calls += 1
            if self.error:
                raise self.error
            return self.result

    @pytest.mark.asyncio
    async def test_real_quotes_where_schwab_answered_synthetic_where_it_did_not(self):
        schwab = self.FakeSchwab({"QQQ": {"symbol": "QQQ", "last": 111.0, "mode": "LIVE"}, "SPY": {"error": "invalid"}})
        result = await SchwabDataPaperBroker(schwab).get_quotes(["QQQ", "SPY"])
        assert schwab.calls == 1
        assert result["QQQ"]["last"] == 111.0 and result["QQQ"]["mode"] == "LIVE"
        assert result["SPY"]["mode"] == "PAPER"  # the synthetic fallback, clearly labelled

    @pytest.mark.asyncio
    async def test_a_whole_batch_failure_degrades_to_synthetic_instead_of_stopping_the_loop(self):
        result = await SchwabDataPaperBroker(self.FakeSchwab(error=BrokerRateLimitError("x"))).get_quotes(["QQQ", "SPY"])
        assert {q["mode"] for q in result.values()} == {"PAPER"}


class TestFactoryCachesTheSchwabAdapter:
    def settings(self, **overrides):
        base = dict(
            _env_file=None, env="test", execution_mode="SCHWAB", schwab_app_key="key", schwab_app_secret="secret",
            schwab_redirect_uri="https://localhost/callback", schwab_refresh_token="refresh", schwab_account_number="99999",
        )
        base.update(overrides)
        return Settings(**base)

    @pytest.fixture(autouse=True)
    def _fresh_cache(self):
        clear_broker_cache()
        yield
        clear_broker_cache()

    def test_the_same_settings_share_one_adapter_and_therefore_one_token_hash_and_limiter(self):
        first, second = build_broker_adapter(self.settings()).adapters["schwab"], build_broker_adapter(self.settings()).adapters["schwab"]
        assert first is second and first.oauth is second.oauth and first._limiter is second._limiter

    def test_a_different_refresh_token_is_a_different_adapter(self):
        assert build_broker_adapter(self.settings()).adapters["schwab"] is not build_broker_adapter(self.settings(schwab_refresh_token="rotated")).adapters["schwab"]

    def test_a_different_rate_limit_is_a_different_adapter(self):
        assert build_broker_adapter(self.settings()).adapters["schwab"] is not build_broker_adapter(self.settings(schwab_rate_limit_per_minute=60)).adapters["schwab"]

    def test_the_configured_limits_reach_the_adapter(self):
        adapter = build_broker_adapter(self.settings(schwab_rate_limit_per_minute=42, schwab_max_retry_after_sec=5.0)).adapters["schwab"]
        assert adapter._limiter.max_calls == 42 and adapter.max_retry_after_sec == 5.0

    def test_clearing_the_cache_builds_a_fresh_adapter(self):
        first = build_broker_adapter(self.settings()).adapters["schwab"]
        clear_broker_cache()
        assert build_broker_adapter(self.settings()).adapters["schwab"] is not first

    def test_paper_mode_is_untouched(self):
        assert isinstance(build_broker_adapter(self.settings(execution_mode="PAPER")), PaperBrokerAdapter)


class TestConcurrentTokenRefresh:
    @pytest.mark.asyncio
    async def test_many_tasks_finding_the_token_expired_cause_one_refresh_not_many(self):
        token_calls = {"n": 0}

        async def slow_handler(request):
            token_calls["n"] += 1
            await asyncio.sleep(0.05)
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 1800})

        oauth = SchwabOAuthClient("k", "s", "https://localhost/cb", refresh_token="r", transport=httpx.MockTransport(slow_handler))
        tokens = await asyncio.gather(*[oauth.get_access_token() for _ in range(10)])
        assert set(tokens) == {"tok"} and token_calls["n"] == 1


class TestQuotesEndpointUsesTheBatchCall:
    """GET /v1/quotes (the dashboard's refresh) must cost ONE broker call, keep its response shape, and keep per-symbol errors per-symbol."""

    @pytest.fixture
    def client(self, app_with_test_db):
        from fastapi.testclient import TestClient

        return TestClient(app_with_test_db, headers={"x-admin-key": "change-me-in-prod"})

    def test_one_batch_call_for_many_symbols_and_the_same_response_shape(self, client, monkeypatch):
        broker = FakeBatchBroker({
            "QQQ": {"symbol": "QQQ", "bid": 1.0, "ask": 2.0, "last": 1.5, "mode": "LIVE"},
            "SPY": {"error": "Schwab returned no quote for SPY (invalid symbol)"},
        })
        monkeypatch.setattr("src.api.server.build_broker_adapter", lambda settings: broker)

        response = client.get("/v1/quotes", params={"symbols": "QQQ,SPY,QQQ"})

        assert response.status_code == 200
        assert broker.batches == [["QQQ", "SPY"]] and broker.singles == []  # ONE call, duplicates collapsed
        quotes = response.json()["quotes"]
        assert [q["symbol"] for q in quotes] == ["QQQ", "SPY", "QQQ"]  # order and duplicates preserved, as before
        assert quotes[0]["last"] == 1.5 and "error" in quotes[1] and "last" not in quotes[1]


class TestPriceAlertsUseTheBatchCall:
    @pytest.mark.asyncio
    async def test_many_alerts_on_many_symbols_cost_one_broker_call(self, test_db_engine_and_session, monkeypatch):
        from src.execution.price_alerts import PriceAlertService, check_alerts_once

        _, session = test_db_engine_and_session
        service = PriceAlertService(session)
        for alert in service.list_all(active_only=True):
            service.cancel(alert.id)
        service.create("ZRLA", "ABOVE", 10.0, created_by="t")
        service.create("ZRLB", "BELOW", 50.0, created_by="t")
        service.create("ZRLB", "ABOVE", 999.0, created_by="t")  # same symbol as another alert
        broker = FakeBatchBroker({"ZRLA": {"symbol": "ZRLA", "last": 11.0}, "ZRLB": {"symbol": "ZRLB", "last": 40.0}})
        notified = []
        monkeypatch.setattr("src.execution.price_alerts.notify_sync", lambda settings, text: notified.append(text))

        fired = await check_alerts_once(session, Settings(_env_file=None, env="test"), broker)

        assert broker.batches == [["ZRLA", "ZRLB"]] and broker.singles == []
        assert fired == 2 and len(notified) == 2  # ZRLA crossed above 10; ZRLB fell below 50
