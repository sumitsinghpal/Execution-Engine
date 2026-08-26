"""
Application configuration using Pydantic Settings.
All configuration comes from environment variables; no hardcoded secrets.
"""

from decimal import Decimal
from typing import Optional

from pydantic import ConfigDict, Field
from pydantic_settings import BaseSettings

from src.accounts.profiles import AccountProfile, BrokerName


class Settings(BaseSettings):
    """Application configuration from environment variables."""
    
    model_config = ConfigDict(env_file=".env", case_sensitive=False)
    
    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    env: str = "development"
    
    # Database
    database_url: str = "sqlite:///./execution_engine.db"
    
    # Execution mode defaults to non-live simulation. LIVE is not implemented.
    execution_mode: str = "PAPER"

    # Account aliases prevent EDGE-TF callers from providing raw broker account IDs.
    account_profiles: dict[str, AccountProfile] = Field(
        default_factory=lambda: {
            "primary": AccountProfile(broker=BrokerName.PAPER),
            "retirement": AccountProfile(broker=BrokerName.PAPER),
            "paper": AccountProfile(broker=BrokerName.PAPER),
        }
    )

    # Schwab credentials must be explicitly supplied for the Schwab adapter.
    schwab_app_key: Optional[str] = None
    schwab_app_secret: Optional[str] = None
    schwab_refresh_token: Optional[str] = None
    schwab_redirect_uri: Optional[str] = None
    
    # Kill switch
    kill_switch_enabled: bool = False
    
    # Account allowlist. Stored raw (comma-separated) because pydantic-settings tries
    # to JSON-parse any list[str]-typed field loaded from an env var / .env file and
    # crashes on a plain "primary,secondary" string. See the *_allowlist/*_denylist
    # properties below for the parsed list form actually used elsewhere in the code.
    account_allowlist_raw: str = Field(default="primary", validation_alias="ACCOUNT_ALLOWLIST")

    # Symbol configuration (comma-separated strings; see parsed properties below)
    symbol_allowlist_raw: str = Field(
        default="QQQ,SPY,IWM,EEM,GLD,TLT", validation_alias="SYMBOL_ALLOWLIST"
    )
    symbol_denylist_raw: str = Field(default="", validation_alias="SYMBOL_DENYLIST")
    
    # Risk limits
    max_order_notional_usd: Decimal = Decimal("100000")
    max_position_concentration_pct: Decimal = Decimal("15")
    market_hours_only: bool = True

    # Stale-quote protection: a quote older than this is refused outright
    # rather than trusted for notional/price-sanity checks, and a LIMIT
    # order priced further than this from the live quote is rejected as
    # likely mispriced (fat-finger protection) rather than silently routed.
    max_quote_age_seconds: int = 10
    max_limit_price_deviation_pct: Decimal = Decimal("0.03")
    
    # API Security
    api_key_admin: str = "change-me-in-prod"
    request_signing_enabled: bool = False
    request_signing_private_key: Optional[str] = None
    
    # Logging
    log_level: str = "INFO"
    log_format: str = "json"
    
    # Schwab API settings
    schwab_api_timeout_sec: int = 30
    schwab_preview_timeout_sec: int = 10
    schwab_preview_expiry_min: int = 5
    schwab_retry_max_attempts: int = 3
    schwab_retry_backoff_sec: float = 1.0

    def get_account_profile(self, alias: str) -> AccountProfile:
        """Resolve a safe account alias or fail closed."""
        profile = self.account_profiles.get(alias)
        if not profile:
            raise ValueError(f"Unknown account alias: {alias}")
        return profile

    @staticmethod
    def _split_csv(value: str) -> list[str]:
        return [s.strip() for s in value.split(",") if s.strip()]

    @property
    def account_allowlist(self) -> list[str]:
        return self._split_csv(self.account_allowlist_raw)

    @account_allowlist.setter
    def account_allowlist(self, value: list[str]) -> None:
        self.account_allowlist_raw = ",".join(value)

    @property
    def symbol_allowlist(self) -> list[str]:
        return self._split_csv(self.symbol_allowlist_raw)

    @symbol_allowlist.setter
    def symbol_allowlist(self, value: list[str]) -> None:
        self.symbol_allowlist_raw = ",".join(value)

    @property
    def symbol_denylist(self) -> list[str]:
        return self._split_csv(self.symbol_denylist_raw)

    @symbol_denylist.setter
    def symbol_denylist(self, value: list[str]) -> None:
        self.symbol_denylist_raw = ",".join(value)


def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
