"""Deterministic paper broker used by default for paper and shadow workflows."""

import hashlib
import math
import random
from datetime import UTC, datetime
from typing import Any

from src.accounts.profiles import AccountProfile
from src.brokers.base import BrokerAdapter


class PaperBrokerAdapter(BrokerAdapter):
    """Simulate broker operations without network access or live side effects."""

    # Fixed cycle lengths (in bars), deliberately NOT tied to how many bars
    # a given caller happens to request — otherwise the same calendar
    # bucket would price differently depending on lookback_days, which
    # would make get_quote() (implicitly "today") disagree with what
    # get_price_history() reports for that same day, and strategies with
    # different lookback_days requirements would each compute a different
    # "today" for the same symbol. Both would be a real correctness bug,
    # not merely a cosmetic one — RiskChecker's limit-price sanity check
    # compares a strategy-sourced entry price against get_quote()'s "last",
    # and a mismatch would spuriously reject an order that was, from the
    # synthetic history's own point of view, priced exactly right.
    _DAILY_CYCLE_BARS = 40
    _INTRADAY_CYCLE_BARS = 20

    @staticmethod
    def _synthetic_price(symbol: str) -> float:
        """
        A stable, deterministic base "anchor price" per symbol — same
        symbol always yields the same anchor. get_quote() and
        get_price_history() both build on this (see _bucket_close), so
        they describe one consistent, if synthetic, market rather than two
        unrelated random series for the same symbol. Clearly synthetic;
        never presented as if it came from a real feed.
        """
        digest = hashlib.sha256(symbol.encode("utf-8")).hexdigest()
        return 20.0 + (int(digest[:8], 16) % 48000) / 100.0  # ~$20-$500 range

    @staticmethod
    def _bar_seed(symbol: str, bar_interval: str, bucket_index: int) -> int:
        digest = hashlib.sha256(f"{symbol}:{bar_interval}:{bucket_index}".encode("utf-8")).hexdigest()
        return int(digest[:8], 16)

    @classmethod
    def _interval_seconds(cls, bar_interval: str) -> float:
        return 300.0 if bar_interval == "5min" else 86_400.0

    @classmethod
    def _cycle_bars(cls, bar_interval: str) -> int:
        return cls._INTRADAY_CYCLE_BARS if bar_interval == "5min" else cls._DAILY_CYCLE_BARS

    def _bucket_close(self, symbol: str, bar_interval: str, idx: int) -> float:
        """The deterministic close for one (symbol, bar_interval, bucket) — the single source of truth both get_quote() and get_price_history() build on."""
        base = self._synthetic_price(symbol)
        rng = random.Random(self._bar_seed(symbol, bar_interval, idx))
        cycle = math.sin(idx / self._cycle_bars(bar_interval)) * 0.06
        noise = (rng.random() - 0.5) * 0.01
        return max(base * (1 + cycle + noise), 0.01)

    async def get_quote(self, symbol: str) -> dict[str, Any]:
        # "Now" is priced off today's daily bucket — the same value
        # get_price_history(symbol, "daily", ...) reports for today's bar,
        # since most strategies (6 of 8) evaluate daily bars and their
        # entry price must agree with what a live quote says "now" is.
        idx = int(datetime.now(UTC).timestamp() // self._interval_seconds("daily"))
        mid = self._bucket_close(symbol, "daily", idx)
        spread = round(mid * 0.0005, 4)  # a nominal 5 bps synthetic spread
        return {
            "symbol": symbol,
            "bid": round(mid - spread, 4),
            "ask": round(mid + spread, 4),
            "last": round(mid, 4),
            "quote_time": datetime.now(UTC).isoformat(),
            "mode": "PAPER",
        }

    async def get_price_history(self, symbol: str, bar_interval: str, lookback_days: int) -> list[dict[str, Any]]:
        """
        Synthetic OHLCV bars — not a real feed, clearly documented as such.
        Anchored on _synthetic_price(symbol) with a slow sine-wave drift
        layered under small per-bar noise, so strategies see plausible
        trends/breakouts to react to rather than flat or pure-noise data
        that would never trigger a moving-average cross or a breakout.
        Deterministic per (symbol, bar_interval, wall-clock bucket): the
        same bar is reproducible if queried again in the same interval
        (see _bucket_close), and the window genuinely advances as real
        time passes, like a live paper chart.
        """
        now = datetime.now(UTC)
        count = 78 if bar_interval == "5min" else max(lookback_days, 1)  # ~6.5 trading hours for a single session
        interval_seconds = self._interval_seconds(bar_interval)
        epoch_index = int(now.timestamp() // interval_seconds)

        closes: list[float] = []
        for i in range(count):
            idx = epoch_index - (count - 1 - i)
            closes.append(self._bucket_close(symbol, bar_interval, idx))

        bars: list[dict[str, Any]] = []
        for i, close in enumerate(closes):
            idx = epoch_index - (count - 1 - i)
            rng = random.Random(self._bar_seed(symbol, bar_interval, idx) ^ 0xA5A5)
            open_ = closes[i - 1] if i > 0 else close
            spread = close * 0.004
            high = max(open_, close) + spread * rng.random()
            low = max(min(open_, close) - spread * rng.random(), 0.01)
            volume = round(100_000 + rng.random() * 500_000)
            # Derived from the bucket index itself, not a fresh `now -
            # offset` subtraction — the latter would drift by a few
            # milliseconds between two calls issued moments apart even
            # though the bucket (and so every other field) is identical,
            # making the series non-reproducible for no real reason.
            ts = datetime.fromtimestamp(idx * interval_seconds, tz=UTC)
            bars.append(
                {
                    "timestamp": ts.isoformat(),
                    "open": round(open_, 4),
                    "high": round(high, 4),
                    "low": round(low, 4),
                    "close": round(close, 4),
                    "volume": volume,
                }
            )
        return bars

    async def preview_order(self, profile: AccountProfile, order_spec: dict[str, Any]) -> dict[str, Any]:
        quantity = order_spec.get("quantity", 0)
        limit_price = float(order_spec.get("limitPrice") or 0)
        if not limit_price:
            # MARKET order: estimate against a live quote instead of
            # silently reporting a $0 estimated investment.
            quote = await self.get_quote(order_spec.get("symbol", ""))
            limit_price = quote["last"]
        return {
            "orderId": f"paper-preview-{order_spec['orderId']}",
            "estimatedCommission": 0.0,
            # Rounded to cents: this is a dollar amount, and OrderPreview.estimated_cost
            # is a Decimal(decimal_places=2) — an un-rounded float product (e.g. a
            # limit price with 3+ decimals, as strategy-sourced entries can have)
            # carries binary-float artifacts like 5189.120000000001 that fail that
            # validation outright instead of silently losing precision.
            "estimatedTotalInvestment": round(quantity * limit_price, 2),
            "status": "OK",
            "symbol": order_spec.get("symbol"),
            "quantity": quantity,
            "mode": "PAPER",
        }

    async def submit_order(self, profile: AccountProfile, order_spec: dict[str, Any]) -> dict[str, Any]:
        return {
            "orderId": f"paper-order-{order_spec['orderId']}",
            "status": "ACCEPTED",
            "symbol": order_spec.get("symbol"),
            "quantity": order_spec.get("quantity"),
            "enteredTime": datetime.now(UTC).isoformat(),
            "mode": "PAPER",
        }

    async def get_order_status(self, profile: AccountProfile, order_id: str) -> dict[str, Any]:
        return {"orderId": order_id, "status": "FILLED", "filledQuantity": 0, "mode": "PAPER"}

    async def list_accounts(self) -> list[dict[str, Any]]:
        return [{"alias": "paper", "broker": "paper", "mode": "PAPER"}]

    async def get_positions(self, profile: AccountProfile) -> list[dict[str, Any]]:
        return []

    # Fixed paper-mode starting equity. This is a deliberately simple,
    # honestly-labeled placeholder — not a real P&L ledger. Tracking actual
    # paper equity (cash +/- synthetic fills over time) would need a
    # stateful balance table of its own; out of scope here. What matters
    # for DrawdownGuard is that get_balances() returns *some* stable
    # "net_liquidation_value" so a baseline can be captured and compared,
    # instead of the previous permanent $0 that made every account look
    # like a 100% drawdown.
    _PAPER_STARTING_EQUITY = 1_000_000.0

    async def get_balances(self, profile: AccountProfile) -> dict[str, Any]:
        return {
            "availableFunds": self._PAPER_STARTING_EQUITY,
            "net_liquidation_value": self._PAPER_STARTING_EQUITY,
            "mode": "PAPER",
        }