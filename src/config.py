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
    
    # Account allowlist
    account_allowlist: list[str] = ["primary"]
    
    # Symbol configuration
    symbol_allowlist: list[str] = ["QQQ", "SPY", "IWM", "EEM", "GLD", "TLT"]
    symbol_denylist: list[str] = []
    
    # Risk limits
    max_order_notional_usd: Decimal = Decimal("100000")
    max_position_concentration_pct: Decimal = Decimal("15")
    market_hours_only: bool = True
    
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
    
    def __init__(self, **data):
        """Parse comma-separated list fields."""
        # Handle comma-separated lists
        for field in ["account_allowlist", "symbol_allowlist", "symbol_denylist"]:
            if field in data and isinstance(data[field], str):
                data[field] = [s.strip() for s in data[field].split(",") if s.strip()]
        super().__init__(**data)


def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
