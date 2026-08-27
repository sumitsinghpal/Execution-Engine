"""
Tests for src/brokers/schwab_data_paper.py — real Schwab market data,
simulated order execution. A fake Schwab double stands in for the real
SchwabBrokerAdapter (no network); the point being verified is the
delegation/fallback behavior and, critically, that preview_order/
submit_order never touch the Schwab object at all.
"""

from datetime import UTC, datetime

import pytest

from src.brokers.schwab_data_paper import SchwabDataPaperBroker


class _FakeSchwab:
    def __init__(self, quote=None, quote_error=None, bars=None, bars_error=None):
        self._quote = quote
        self._quote_error = quote_error
        self._bars = bars
        self._bars_error = bars_error
        self.get_quote_calls = []
        self.get_price_history_calls = []
        self.preview_order_calls = []
        self.submit_order_calls = []

    async def get_quote(self, symbol):
        self.get_quote_calls.append(symbol)
        if self._quote_error:
            raise self._quote_error
        return self._quote

    async def get_price_history(self, symbol, bar_interval, lookback_days):
        self.get_price_history_calls.append((symbol, bar_interval, lookback_days))
        if self._bars_error:
            raise self._bars_error
        return self._bars

    async def preview_order(self, profile, order_spec):
        self.preview_order_calls.append(order_spec)
        raise AssertionError("SchwabDataPaperBroker must never call the real Schwab preview_order")

    async def submit_order(self, profile, order_spec):
        self.submit_order_calls.append(order_spec)
        raise AssertionError("SchwabDataPaperBroker must never call the real Schwab submit_order")


def _real_quote(price=450.0):
    return {"symbol": "QQQ", "bid": price - 0.1, "ask": price + 0.1, "last": price, "quote_time": datetime.now(UTC).isoformat(), "mode": "LIVE"}


class TestGetQuote:
    @pytest.mark.asyncio
    async def test_delegates_to_real_schwab_when_it_succeeds(self):
        schwab = _FakeSchwab(quote=_real_quote(450.0))
        broker = SchwabDataPaperBroker(schwab)

        quote = await broker.get_quote("QQQ")

        assert quote["last"] == 450.0
        assert quote["mode"] == "LIVE"
        assert schwab.get_quote_calls == ["QQQ"]

    @pytest.mark.asyncio
    async def test_falls_back_to_synthetic_on_schwab_exception(self):
        schwab = _FakeSchwab(quote_error=RuntimeError("auth expired"))
        broker = SchwabDataPaperBroker(schwab)

        quote = await broker.get_quote("QQQ")

        assert quote["mode"] == "PAPER"  # the inherited synthetic path, not a raised exception

    @pytest.mark.asyncio
    async def test_falls_back_to_synthetic_when_schwab_returns_no_last_price(self):
        schwab = _FakeSchwab(quote={"symbol": "QQQ", "bid": None, "ask": None, "last": None, "quote_time": "x", "mode": "LIVE"})
        broker = SchwabDataPaperBroker(schwab)

        quote = await broker.get_quote("QQQ")

        assert quote["mode"] == "PAPER"


class TestGetPriceHistory:
    @pytest.mark.asyncio
    async def test_delegates_to_real_schwab_when_bars_are_returned(self):
        real_bars = [{"timestamp": "2024-01-01T00:00:00Z", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}]
        schwab = _FakeSchwab(bars=real_bars)
        broker = SchwabDataPaperBroker(schwab)

        bars = await broker.get_price_history("QQQ", "daily", 260)

        assert bars == real_bars
        assert schwab.get_price_history_calls == [("QQQ", "daily", 260)]

    @pytest.mark.asyncio
    async def test_falls_back_to_synthetic_when_schwab_returns_empty(self):
        schwab = _FakeSchwab(bars=[])
        broker = SchwabDataPaperBroker(schwab)

        bars = await broker.get_price_history("QQQ", "daily", 260)

        assert len(bars) > 0  # the synthetic generator always returns something

    @pytest.mark.asyncio
    async def test_falls_back_to_synthetic_on_schwab_exception(self):
        schwab = _FakeSchwab(bars_error=RuntimeError("market data outage"))
        broker = SchwabDataPaperBroker(schwab)

        bars = await broker.get_price_history("QQQ", "daily", 260)

        assert len(bars) > 0


class TestOrdersNeverReachSchwab:
    @pytest.mark.asyncio
    async def test_preview_order_never_calls_the_real_schwab_object(self):
        schwab = _FakeSchwab(quote=_real_quote(450.0))
        broker = SchwabDataPaperBroker(schwab)

        preview = await broker.preview_order(None, {"orderId": "1", "symbol": "QQQ", "quantity": 10, "assetType": "EQUITY"})

        assert schwab.preview_order_calls == []  # never touched — would have raised if it had been
        assert preview["mode"] == "PAPER"

    @pytest.mark.asyncio
    async def test_submit_order_never_calls_the_real_schwab_object(self):
        schwab = _FakeSchwab(quote=_real_quote(450.0))
        broker = SchwabDataPaperBroker(schwab)

        receipt = await broker.submit_order(None, {"orderId": "1", "symbol": "QQQ", "quantity": 10})

        assert schwab.submit_order_calls == []
        assert receipt["mode"] == "PAPER"

    @pytest.mark.asyncio
    async def test_market_order_preview_is_priced_off_the_real_quote_not_a_second_unrelated_number(self):
        """
        The whole point of overriding only get_quote(): PaperBrokerAdapter.preview_order()
        calls self.get_quote(...) internally for a MARKET order's estimate, so this
        override alone makes the simulated fill price consistent with the real
        quote that triggered the trade.
        """
        schwab = _FakeSchwab(quote=_real_quote(450.0))
        broker = SchwabDataPaperBroker(schwab)

        preview = await broker.preview_order(None, {"orderId": "1", "symbol": "QQQ", "quantity": 10, "assetType": "EQUITY"})

        assert preview["estimatedTotalInvestment"] == pytest.approx(4500.0)  # 10 * 450.0, the real quote's price
