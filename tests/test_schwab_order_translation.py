"""
Regression tests for a real, latent bug: the Schwab adapter used to POST the
broker-neutral FLAT order spec straight to Schwab's previewOrder — not Schwab's
nested order format — and then read paper-broker keys out of the response, so a
real preview would have failed (or shown $0 and ignored Schwab's rejections).
"""

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from src.accounts.profiles import AccountProfile, BrokerName
from src.brokers.base import BrokerError
from src.brokers.schwab.adapter import SchwabBrokerAdapter
from src.brokers.schwab.auth import SchwabOAuthClient
from src.brokers.schwab.order_translation import extract_order_value, normalize_preview, to_schwab_order
from src.broker.order_builder import OrderBuilder
from src.execution.executor import Executor
from src.models.orders import AssetType, Instruction, OrderType, TradeProposal

OPTION_SYMBOL = "QQQ   261218C00400000"


def spec(**overrides):
    base = {"orderId": "d-1", "accountId": "primary", "symbol": "QQQ", "assetType": "ETF", "quantity": 5,
            "instruction": "BUY", "orderType": "LIMIT", "limitPrice": "400"}
    base.update(overrides)
    return base


class TestToSchwabOrder:
    def test_limit_order_is_nested_and_the_limit_price_becomes_price(self):
        assert to_schwab_order(spec()) == {
            "orderType": "LIMIT", "session": "NORMAL", "duration": "DAY", "orderStrategyType": "SINGLE",
            "price": "400",
            "orderLegCollection": [{"instruction": "BUY", "quantity": 5, "instrument": {"symbol": "QQQ", "assetType": "EQUITY"}}],
        }

    def test_neutral_only_fields_never_reach_schwab(self):
        order = to_schwab_order(spec())
        assert not {"orderId", "accountId", "symbol", "assetType", "limitPrice", "quantity"} & set(order)

    def test_etf_and_equity_both_order_as_equity(self):
        for asset in ("ETF", "EQUITY"):
            leg = to_schwab_order(spec(assetType=asset))["orderLegCollection"][0]
            assert leg["instrument"]["assetType"] == "EQUITY"

    def test_market_order_carries_no_prices(self):
        order = to_schwab_order(spec(orderType="MARKET", limitPrice=None))
        assert order["orderType"] == "MARKET" and "price" not in order and "stopPrice" not in order

    def test_stop_and_stop_limit(self):
        stop = to_schwab_order(spec(orderType="STOP", limitPrice=None, stopPrice="390"))
        assert stop["stopPrice"] == "390" and "price" not in stop
        both = to_schwab_order(spec(orderType="STOP_LIMIT", stopPrice="390", limitPrice="389.5"))
        assert (both["stopPrice"], both["price"]) == ("390", "389.5")

    def test_option_buy_opens_and_sell_can_only_close(self):
        buy = to_schwab_order(spec(assetType="OPTION", symbol=OPTION_SYMBOL, limitPrice="3.5"))
        sell = to_schwab_order(spec(assetType="OPTION", symbol=OPTION_SYMBOL, limitPrice="3.5", instruction="SELL"))
        assert buy["orderLegCollection"][0]["instruction"] == "BUY_TO_OPEN"
        assert sell["orderLegCollection"][0]["instruction"] == "SELL_TO_CLOSE"  # never SELL_TO_OPEN: no accidental writing
        assert buy["orderLegCollection"][0]["instrument"] == {"symbol": OPTION_SYMBOL, "assetType": "OPTION"}

    @pytest.mark.parametrize(
        "changes, message",
        [
            ({"assetType": "FUTURE"}, "not supported"),
            ({"assetType": "BOND"}, "not supported"),
            ({"orderType": "TWAP"}, "not translatable"),
            ({"instruction": "SELL_SHORT"}, "not translatable"),
            ({"quantity": 0}, "positive whole number"),
            ({"quantity": 1.5}, "positive whole number"),
            ({"limitPrice": None}, "no limitPrice"),
            ({"limitPrice": "abc"}, "not a number"),
            ({"limitPrice": "-1"}, "must be positive"),
            ({"orderType": "MARKET"}, "must not carry"),
            ({"orderType": "STOP"}, "no stopPrice"),
            ({"symbol": ""}, "no symbol"),
            ({"assetType": "OPTION", "symbol": "QQQ"}, "21-character"),
        ],
    )
    def test_anything_untranslatable_fails_closed(self, changes, message):
        with pytest.raises(BrokerError, match=message):
            to_schwab_order(spec(**changes))

    def test_output_is_accepted_by_schwabkits_independent_validator(self):
        """Two separately written implementations of the same schema must agree — so drift is caught, not shipped."""
        schwabkit_orders = pytest.importorskip("schwabkit.orders")
        for order in (
            to_schwab_order(spec()),
            to_schwab_order(spec(orderType="MARKET", limitPrice=None)),
            to_schwab_order(spec(orderType="STOP_LIMIT", stopPrice="390", limitPrice="389.5")),
            to_schwab_order(spec(assetType="OPTION", symbol=OPTION_SYMBOL, limitPrice="3.5")),
        ):
            assert schwabkit_orders.find_problems(order) == []

    def test_the_old_flat_spec_is_provably_not_a_schwab_order(self):
        """Documents what the bug was: the neutral spec fails Schwab's schema outright."""
        schwabkit_orders = pytest.importorskip("schwabkit.orders")
        assert schwabkit_orders.find_problems(spec()) != []


class TestNormalizePreview:
    SCHWAB = {
        "orderStrategy": {"orderBalance": {"orderValue": 2000.0}},
        "orderValidationResult": {"accepts": [], "rejects": []},
        "commissionAndFee": {"commission": {"commissionLegs": [{"commissionValues": [{"value": 0.65}, {"value": 0.10}]}]}},
    }

    def test_order_value_and_commission_are_read_from_schwabs_shape(self):
        assert extract_order_value(self.SCHWAB) == 2000.0
        result = normalize_preview(self.SCHWAB, 2000.0, "schwab")
        assert result["estimatedTotalInvestment"] == 2000.0 and result["estimatedCommission"] == 0.75
        assert result["status"] == "OK" and result["rejects"] == [] and result["estimateSource"] == "schwab"

    def test_rejects_flip_the_status_and_are_listed(self):
        rejected = {**self.SCHWAB, "orderValidationResult": {"rejects": [{"message": "Insufficient buying power"}]}}
        result = normalize_preview(rejected, 2000.0, "schwab")
        assert result["status"] == "REJECTED" and result["rejects"] == ["Insufficient buying power"]

    def test_a_response_without_an_order_value_is_not_silently_zero(self):
        assert extract_order_value({}) is None and extract_order_value("junk") is None


def _adapter(handler):
    transport = httpx.MockTransport(handler)
    oauth = SchwabOAuthClient("k", "s", "https://localhost/cb", refresh_token="r", transport=transport)
    return SchwabBrokerAdapter(oauth, transport=transport, retry_backoff_sec=0.001)


PROFILE = AccountProfile(broker=BrokerName.SCHWAB, credential_profile="schwab_main", account_hash="HASH", live_enabled=False)


class TestAdapterPreview:
    @pytest.mark.asyncio
    async def test_the_wire_body_is_the_nested_order_not_the_flat_spec(self):
        sent = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/oauth/token":
                return httpx.Response(200, json={"access_token": "a", "expires_in": 1800})
            sent["path"], sent["body"] = request.url.path, request.read()
            return httpx.Response(200, json={"orderStrategy": {"orderBalance": {"orderValue": 2000.0}}})

        result = await _adapter(handler).preview_order(PROFILE, spec())
        import json
        body = json.loads(sent["body"])
        assert sent["path"] == "/trader/v1/accounts/HASH/previewOrder"
        assert body["orderStrategyType"] == "SINGLE" and body["orderLegCollection"][0]["instrument"]["symbol"] == "QQQ"
        assert "symbol" not in body and "limitPrice" not in body
        assert result["estimatedTotalInvestment"] == 2000.0

    @pytest.mark.asyncio
    async def test_an_untranslatable_order_never_reaches_the_network(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            return httpx.Response(200, json={"access_token": "a", "expires_in": 1800})

        with pytest.raises(BrokerError):
            await _adapter(handler).preview_order(PROFILE, spec(assetType="FUTURE"))
        assert [c for c in calls if c != "/v1/oauth/token"] == []

    @pytest.mark.asyncio
    async def test_a_missing_order_value_falls_back_to_a_local_estimate_not_zero(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/oauth/token":
                return httpx.Response(200, json={"access_token": "a", "expires_in": 1800})
            return httpx.Response(200, json={"orderValidationResult": {"rejects": []}})

        result = await _adapter(handler).preview_order(PROFILE, spec())  # 5 x $400
        assert result["estimatedTotalInvestment"] == 2000.0 and result["estimateSource"] == "local_estimate"

    @pytest.mark.asyncio
    async def test_account_hash_comes_from_account_numbers(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/oauth/token":
                return httpx.Response(200, json={"access_token": "a", "expires_in": 1800})
            if request.url.path == "/trader/v1/accounts/accountNumbers":
                return httpx.Response(200, json=[{"accountNumber": "111", "hashValue": "H111"}, {"accountNumber": "222", "hashValue": "H222"}])
            return httpx.Response(404)

        adapter = _adapter(handler)
        assert await adapter.resolve_account_hash("222") == "H222"
        with pytest.raises(BrokerError, match="No accessible Schwab account"):
            await adapter.resolve_account_hash("999")


class RejectingBroker:
    """Answers previews the way the adapter now does when Schwab rejects."""

    def __init__(self, rejects):
        self.rejects = rejects

    async def get_quote(self, symbol):
        return {"symbol": symbol, "bid": "100", "ask": "100", "last": "100", "quote_time": datetime.now(UTC).isoformat(), "mode": "TEST"}

    async def preview_order(self, profile, order_spec):
        return {"estimatedCommission": 0, "estimatedTotalInvestment": 500, "status": "REJECTED" if self.rejects else "OK", "rejects": self.rejects}


class TestExecutorHonoursBrokerRejections:
    def proposal(self, decision_id):
        return TradeProposal(decision_id=decision_id, account="primary", symbol="QQQ", asset_type=AssetType.ETF,
                             instruction=Instruction.BUY, quantity=5, order_type=OrderType.LIMIT, limit_price=Decimal("100"))

    @pytest.mark.asyncio
    async def test_a_broker_rejection_makes_the_preview_rejected_not_approved(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        preview = await Executor(session=session, broker=RejectingBroker(["Insufficient buying power"])).preview_order(self.proposal("broker-reject-001"))
        assert preview.risk_verdict == "REJECTED"
        assert preview.risk_details["checks"]["broker_preview_ok"] is False
        assert any("Insufficient buying power" in text for text in preview.risk_details["rejections"])

    @pytest.mark.asyncio
    async def test_no_rejects_leaves_the_verdict_and_check_keys_exactly_as_before(self, test_db_engine_and_session):
        _, session = test_db_engine_and_session
        preview = await Executor(session=session, broker=RejectingBroker([])).preview_order(self.proposal("broker-reject-002"))
        assert preview.risk_verdict == "APPROVED"
        assert "broker_preview_ok" not in preview.risk_details["checks"]


def test_order_builder_output_translates_end_to_end():
    """The real OrderBuilder -> the real translation: what Executor actually sends, start to finish."""
    proposal = TradeProposal(decision_id="e2e-1", account="primary", symbol="SPY", asset_type=AssetType.ETF,
                             instruction=Instruction.SELL, quantity=3, order_type=OrderType.STOP, stop_price=Decimal("480.25"))
    order = to_schwab_order(OrderBuilder().build_order_spec(proposal, "primary"))
    assert order["orderType"] == "STOP" and order["stopPrice"] == "480.25"
    assert order["orderLegCollection"][0] == {"instruction": "SELL", "quantity": 3, "instrument": {"symbol": "SPY", "assetType": "EQUITY"}}
