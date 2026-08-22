"""Broker-neutral execution adapter contract."""

from abc import ABC, abstractmethod
from typing import Any

from src.accounts.profiles import AccountProfile


class BrokerError(RuntimeError):
    """Raised when a broker cannot complete a requested operation."""


class LiveTradingDisabledError(BrokerError):
    """Raised when a live order submission is attempted without explicit enablement."""


class BrokerAdapter(ABC):
    """Broker-neutral API used by the execution core."""

    @abstractmethod
    async def preview_order(self, profile: AccountProfile, order_spec: dict[str, Any]) -> dict[str, Any]:
        """Preview an order without submitting it."""

    @abstractmethod
    async def submit_order(self, profile: AccountProfile, order_spec: dict[str, Any]) -> dict[str, Any]:
        """Submit an order when the adapter explicitly supports it."""

    @abstractmethod
    async def get_order_status(
        self, profile: AccountProfile, order_id: str
    ) -> dict[str, Any]:
        """Return the current broker status for an order."""

    @abstractmethod
    async def list_accounts(self) -> list[dict[str, Any]]:
        """List accessible accounts without exposing credentials."""

    @abstractmethod
    async def get_positions(self, profile: AccountProfile) -> list[dict[str, Any]]:
        """Return positions for an account profile."""

    @abstractmethod
    async def get_balances(self, profile: AccountProfile) -> dict[str, Any]:
        """Return balances for an account profile."""