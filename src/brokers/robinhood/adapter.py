"""Authenticated host bridge client, reconciled from the Drive 002 package.

The host service must implement this protocol. This client does not implement
Robinhood authentication or claim that a host service is deployed.
"""
import httpx

from src.accounts.profiles import BrokerName
from src.brokers.base import BrokerAdapter, BrokerError, LiveTradingDisabledError


class RobinhoodHostBridgeAdapter(BrokerAdapter):
    def __init__(self, base_url, token, live_enabled=False, timeout=20):
        if not base_url or not token:
            raise BrokerError("Robinhood bridge URL and token are required")
        self.base_url, self.token = base_url.rstrip("/"), token
        self.live_enabled, self.timeout = live_enabled, timeout

    async def _call(self, operation, payload=None):
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout) as client:
            response = await client.post("/v1/broker/robinhood",
                headers={"Authorization": f"Bearer {self.token}"},
                json={"operation": operation, "payload": payload or {}})
        if response.status_code >= 400:
            raise BrokerError(f"Robinhood bridge {operation} failed: HTTP {response.status_code}")
        data = response.json()
        if not isinstance(data, dict) or data.get("ok") is not True or "result" not in data:
            raise BrokerError(f"Robinhood bridge {operation} did not acknowledge success")
        return data["result"]

    async def capabilities(self):
        return await self._call("CAPABILITIES")

    async def _operation(self, operation, profile=None, **payload):
        if profile is not None:
            if profile.broker != BrokerName.ROBINHOOD or not profile.credential_profile:
                raise BrokerError("Explicit Robinhood account binding required")
            payload["account_alias"] = profile.credential_profile
        caps = await self.capabilities()
        if not isinstance(caps, dict) or caps.get(operation) is not True:
            raise BrokerError(f"Robinhood bridge lacks {operation} capability")
        return await self._call(operation, payload)

    async def preview_order(self, profile, order_spec):
        return await self._operation("REVIEW", profile, order=order_spec)

    async def submit_order(self, profile, order_spec):
        if not self.live_enabled or not profile.live_enabled:
            raise LiveTradingDisabledError("Robinhood live submission is disabled")
        return await self._operation("SUBMIT", profile, order=order_spec)

    async def get_order_status(self, profile, order_id):
        return await self._operation("STATUS", profile, order_id=order_id)

    async def cancel_order(self, profile, order_id):
        return await self._operation("CANCEL", profile, order_id=order_id)

    async def list_accounts(self):
        return await self._operation("ACCOUNTS")

    async def get_positions(self, profile):
        return await self._operation("POSITIONS", profile)

    async def get_balances(self, profile):
        return await self._operation("BALANCES", profile)

    async def get_quote(self, symbol):
        return await self._operation("QUOTE", symbol=symbol)

    async def get_price_history(self, symbol, bar_interval, lookback_days):
        return await self._operation("HISTORY", symbol=symbol, bar_interval=bar_interval,
                                     lookback_days=lookback_days)
