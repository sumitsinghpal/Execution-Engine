"""Tests for get_price_history() on both broker adapters."""

import httpx
import pytest

from src.brokers.paper import PaperBrokerAdapter
from src.brokers.schwab.adapter import SchwabBrokerAdapter
from src.brokers.schwab.auth import SchwabOAuthClient


class TestPaperPriceHistory:
    @pytest.mark.asyncio
    async def test_daily_bars_returns_requested_count(self):
        adapter = PaperBrokerAdapter()
        bars = await adapter.get_price_history("QQQ", "daily", 60)
        assert len(bars) == 60
        assert all({"timestamp", "open", "high", "low", "close", "volume"} <= b.keys() for b in bars)

    @pytest.mark.asyncio
    async def test_intraday_bars_ignore_lookback_days(self):
        adapter = PaperBrokerAdapter()
        bars = await adapter.get_price_history("QQQ", "5min", lookback_days=999)
        assert len(bars) == 78  # one session's worth of 5-min bars

    @pytest.mark.asyncio
    async def test_bars_are_internally_consistent_ohlc(self):
        adapter = PaperBrokerAdapter()
        bars = await adapter.get_price_history("SPY", "daily", 30)
        for b in bars:
            assert b["low"] <= b["open"] <= b["high"]
            assert b["low"] <= b["close"] <= b["high"]
            assert b["low"] > 0

    @pytest.mark.asyncio
    async def test_same_bucket_is_deterministic(self):
        """Two calls within the same wall-clock bucket must return identical bars — reproducible, not random noise."""
        adapter = PaperBrokerAdapter()
        first = await adapter.get_price_history("QQQ", "daily", 10)
        second = await adapter.get_price_history("QQQ", "daily", 10)
        assert first == second

    @pytest.mark.asyncio
    async def test_different_symbols_produce_different_series(self):
        adapter = PaperBrokerAdapter()
        qqq = await adapter.get_price_history("QQQ", "daily", 10)
        spy = await adapter.get_price_history("SPY", "daily", 10)
        assert [b["close"] for b in qqq] != [b["close"] for b in spy]


class TestSchwabPriceHistory:
    @pytest.mark.asyncio
    async def test_parses_real_candle_response_shape(self):
        def transport(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/oauth/token":
                return httpx.Response(200, json={"access_token": "tok", "expires_in": 1800})
            if request.url.path == "/marketdata/v1/pricehistory":
                assert request.url.params["symbol"] == "QQQ"
                assert request.url.params["frequencyType"] == "daily"
                return httpx.Response(
                    200,
                    json={
                        "candles": [
                            {"datetime": 1735689600000, "open": 268, "high": 271, "low": 267, "close": 270, "volume": 1000000},
                            {"datetime": 1735776000000, "open": 270, "high": 273, "low": 269, "close": 272, "volume": 1100000},
                        ],
                        "symbol": "QQQ",
                        "empty": False,
                    },
                )
            return httpx.Response(404)

        oauth = SchwabOAuthClient(
            app_key="k", app_secret="s", redirect_uri="https://localhost/callback",
            refresh_token="r", transport=httpx.MockTransport(transport),
        )
        adapter = SchwabBrokerAdapter(oauth, transport=httpx.MockTransport(transport))

        bars = await adapter.get_price_history("QQQ", "daily", 260)

        assert len(bars) == 2
        assert bars[0]["close"] == 270
        assert bars[1]["close"] == 272
        assert "timestamp" in bars[0]

    @pytest.mark.asyncio
    async def test_intraday_uses_minute_frequency_params(self):
        seen = {}

        def transport(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/oauth/token":
                return httpx.Response(200, json={"access_token": "tok", "expires_in": 1800})
            if request.url.path == "/marketdata/v1/pricehistory":
                seen.update(dict(request.url.params))
                return httpx.Response(200, json={"candles": [], "symbol": "QQQ", "empty": True})
            return httpx.Response(404)

        oauth = SchwabOAuthClient(
            app_key="k", app_secret="s", redirect_uri="https://localhost/callback",
            refresh_token="r", transport=httpx.MockTransport(transport),
        )
        adapter = SchwabBrokerAdapter(oauth, transport=httpx.MockTransport(transport))

        await adapter.get_price_history("QQQ", "5min", lookback_days=1)

        assert seen["frequencyType"] == "minute"
        assert seen["frequency"] == "5"
        assert seen["periodType"] == "day"
