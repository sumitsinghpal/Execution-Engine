"""Route account operations by the exact configured broker; never fall back."""
from src.brokers.base import BrokerAdapter, BrokerError


class BrokerRouter(BrokerAdapter):
    def __init__(self, adapters, market_data_broker):
        self.adapters = adapters
        self.market_data_broker = market_data_broker

    def _account(self, profile):
        if profile.broker not in self.adapters:
            raise BrokerError(f"No adapter configured for {profile.broker}")
        return self.adapters[profile.broker]

    def _market(self):
        if self.market_data_broker not in self.adapters:
            raise BrokerError("Set MARKET_DATA_BROKER explicitly for multiple broker routes")
        return self.adapters[self.market_data_broker]

    async def preview_order(self, profile, order_spec):
        return await self._account(profile).preview_order(profile, order_spec)

    async def submit_order(self, profile, order_spec):
        return await self._account(profile).submit_order(profile, order_spec)

    async def get_order_status(self, profile, order_id):
        return await self._account(profile).get_order_status(profile, order_id)

    async def cancel_order(self, profile, order_id):
        adapter = self._account(profile)
        if not hasattr(adapter, "cancel_order"):
            raise BrokerError("Selected broker has no cancellation capability")
        return await adapter.cancel_order(profile, order_id)

    async def list_accounts(self):
        result = []
        for adapter in self.adapters.values():
            result.extend(await adapter.list_accounts())
        return result

    async def get_positions(self, profile):
        return await self._account(profile).get_positions(profile)

    async def get_balances(self, profile):
        return await self._account(profile).get_balances(profile)

    async def get_quote(self, symbol):
        return await self._market().get_quote(symbol)

    async def get_price_history(self, symbol, bar_interval, lookback_days):
        return await self._market().get_price_history(symbol, bar_interval, lookback_days)
