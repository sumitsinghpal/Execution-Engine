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

The Schwab adapter is CACHED per process. About twenty call sites (every
API endpoint and every background loop) call build_broker_adapter(), and
each used to get a brand-new SchwabBrokerAdapter with a brand-new
SchwabOAuthClient. Against live Schwab that meant: no access token reuse (the
first request of nearly every call went to the token endpoint first), the
resolved account hash forgotten every time (an extra /accounts/accountNumbers
call before nearly every account operation), and — decisively — a rate limiter
that reset on every call and so limited nothing. One shared adapter shares the
token, the account hash, and the limiter, which is what "120 calls/minute per
app" needs. Paper mode is stateless and is not cached.
"""

import threading
from typing import Optional

from src.accounts.profiles import BrokerName
from src.brokers.base import BrokerAdapter
from src.brokers.paper import PaperBrokerAdapter
from src.brokers.schwab.adapter import SchwabBrokerAdapter
from src.brokers.schwab.auth import SchwabOAuthClient
from src.config import Settings

_schwab_cache: dict[tuple, SchwabBrokerAdapter] = {}
_schwab_cache_lock = threading.Lock()


def clear_broker_cache() -> None:
    """Drop every cached Schwab adapter (tests; also the way to pick up a rotated refresh token in-process)."""
    with _schwab_cache_lock:
        _schwab_cache.clear()


def _cache_key(settings: Settings) -> tuple:
    """Everything that changes what the adapter would be. A different refresh token is a different adapter."""
    return (
        settings.schwab_app_key,
        settings.schwab_app_secret,
        settings.schwab_redirect_uri,
        settings.schwab_refresh_token,
        settings.schwab_account_number,
        settings.schwab_api_timeout_sec,
        settings.schwab_retry_max_attempts,
        settings.schwab_retry_backoff_sec,
        settings.schwab_rate_limit_per_minute,
        settings.schwab_max_retry_after_sec,
        settings.schwab_token_file,
    )


def _build_schwab_adapter(settings: Settings, mock_broker: bool = False) -> BrokerAdapter:
    """Choose the configured broker without allowing implicit live trading."""
    if mock_broker or settings.execution_mode.upper() in {"PAPER", "SHADOW"}:
        return PaperBrokerAdapter()

    profiles = settings.account_profiles.values()
    if all(profile.broker != BrokerName.SCHWAB for profile in profiles):
        return PaperBrokerAdapter()

    if not all([settings.schwab_app_key, settings.schwab_app_secret, settings.schwab_redirect_uri]):
        raise ValueError("Schwab mode requires configured OAuth app key, app secret, and redirect URI")

    key = _cache_key(settings)
    with _schwab_cache_lock:
        adapter: Optional[SchwabBrokerAdapter] = _schwab_cache.get(key)
        if adapter is None:
            oauth = SchwabOAuthClient(
                app_key=settings.schwab_app_key,
                app_secret=settings.schwab_app_secret,
                redirect_uri=settings.schwab_redirect_uri,
                refresh_token=settings.schwab_refresh_token,
                timeout_sec=settings.schwab_api_timeout_sec,
                token_file=settings.schwab_token_file,
            )
            adapter = SchwabBrokerAdapter(
                oauth,
                timeout_sec=settings.schwab_api_timeout_sec,
                retry_max_attempts=settings.schwab_retry_max_attempts,
                retry_backoff_sec=settings.schwab_retry_backoff_sec,
                account_number=settings.schwab_account_number,
                rate_limit_per_minute=settings.schwab_rate_limit_per_minute,
                max_retry_after_sec=settings.schwab_max_retry_after_sec,
            )
            _schwab_cache[key] = adapter
    return adapter


__all__ = ["build_broker_adapter", "clear_broker_cache"]


def build_broker_adapter(settings: Settings, mock_broker: bool = False) -> BrokerAdapter:
    if mock_broker or settings.execution_mode.upper() in {"PAPER", "SHADOW"}:
        return PaperBrokerAdapter()
    if settings.execution_mode.upper() not in {"LIVE", "SCHWAB", "ROBINHOOD"}:
        raise ValueError("Unknown execution mode")
    from src.brokers.robinhood.adapter import RobinhoodHostBridgeAdapter
    from src.brokers.router import BrokerRouter
    brokers = {p.broker for p in settings.account_profiles.values()}
    adapters = {}
    if BrokerName.PAPER in brokers:
        adapters[BrokerName.PAPER] = PaperBrokerAdapter()
    if BrokerName.SCHWAB in brokers:
        adapters[BrokerName.SCHWAB] = _build_schwab_adapter(settings)
    if BrokerName.ROBINHOOD in brokers:
        adapters[BrokerName.ROBINHOOD] = RobinhoodHostBridgeAdapter(
            settings.robinhood_bridge_url, settings.robinhood_bridge_token,
            settings.robinhood_live_trading_enabled)
    if len(adapters) == 1:
        return next(iter(adapters.values()))
    live_brokers = brokers - {BrokerName.PAPER}
    market = settings.market_data_broker or (next(iter(live_brokers)) if len(live_brokers) == 1 else "")
    return BrokerRouter(adapters, market)
