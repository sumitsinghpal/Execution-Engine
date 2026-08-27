"""
Single place that decides which BrokerAdapter a given Settings instance
means: PaperBrokerAdapter for PAPER/SHADOW execution modes or when no
account profile actually uses Schwab, SchwabBrokerAdapter otherwise.

This used to live only inside Executor._build_broker_adapter, which meant
every other component that talks to a broker — DrawdownGuard,
PositionReconciliationService — quietly defaulted to PaperBrokerAdapter()
on its own instead of sharing this decision. That's a real gap for "add
real Schwab credentials and it works": those components would keep
reporting synthetic paper numbers (a fixed $1,000,000 balance, always-empty
positions) even once Schwab was fully configured and Executor itself was
correctly using it, because nothing told them to check. See
src/api/server.py for the endpoints that now build a broker via this
factory and pass it in explicitly instead of relying on that default.
"""

from typing import Optional

from src.accounts.profiles import BrokerName
from src.brokers.base import BrokerAdapter
from src.brokers.paper import PaperBrokerAdapter
from src.brokers.schwab.adapter import SchwabBrokerAdapter
from src.brokers.schwab.auth import SchwabOAuthClient
from src.config import Settings


def build_broker_adapter(settings: Settings, mock_broker: bool = False) -> BrokerAdapter:
    """Choose the configured broker without allowing implicit live trading."""
    if mock_broker or settings.execution_mode.upper() in {"PAPER", "SHADOW"}:
        return PaperBrokerAdapter()

    profiles = settings.account_profiles.values()
    if all(profile.broker != BrokerName.SCHWAB for profile in profiles):
        return PaperBrokerAdapter()

    if not all([settings.schwab_app_key, settings.schwab_app_secret, settings.schwab_redirect_uri]):
        raise ValueError("Schwab mode requires configured OAuth app key, app secret, and redirect URI")

    oauth = SchwabOAuthClient(
        app_key=settings.schwab_app_key,
        app_secret=settings.schwab_app_secret,
        redirect_uri=settings.schwab_redirect_uri,
        refresh_token=settings.schwab_refresh_token,
        timeout_sec=settings.schwab_api_timeout_sec,
    )
    return SchwabBrokerAdapter(
        oauth,
        timeout_sec=settings.schwab_api_timeout_sec,
        retry_max_attempts=settings.schwab_retry_max_attempts,
        retry_backoff_sec=settings.schwab_retry_backoff_sec,
        account_number=settings.schwab_account_number,
    )


__all__ = ["build_broker_adapter"]
