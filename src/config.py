from __future__ import annotations

from decimal import Decimal
from functools import lru_cache

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "schwab-execution-engine"
    api_prefix: str = "/v1"
    database_url: str = "sqlite:///./execution.db"
    schwab_base_url: str = "https://api.schwabapi.com"
    schwab_mock_mode: bool = True
    schwab_timeout_seconds: float = 5.0

    schwab_app_key: str | None = None
    schwab_app_secret: str | None = None
    schwab_refresh_token: str | None = None

    admin_api_key: str = "changeme"
    allowed_accounts: str = "primary"
    allowed_symbols: str = "QQQ,SPY"
    denied_symbols: str = ""
    max_order_notional: Decimal = Field(default=Decimal("100000"))
    account_equity_notional: Decimal = Field(default=Decimal("1000000"))
    max_position_concentration: Decimal = Field(default=Decimal("0.25"))
    market_order_assumed_price: Decimal = Field(default=Decimal("100"))
    enforce_market_hours: bool = False
    preview_ttl_minutes: int = 15
    decision_max_age_days: int = 2

    @computed_field  # type: ignore[prop-decorator]
    @property
    def allowed_accounts_list(self) -> list[str]:
        return [x.strip() for x in self.allowed_accounts.split(",") if x.strip()]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def allowed_symbols_list(self) -> list[str]:
        return [x.strip().upper() for x in self.allowed_symbols.split(",") if x.strip()]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def denied_symbols_list(self) -> list[str]:
        return [x.strip().upper() for x in self.denied_symbols.split(",") if x.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
