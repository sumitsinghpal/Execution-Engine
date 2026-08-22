from __future__ import annotations

from src.config import get_settings


def has_schwab_credentials() -> bool:
    settings = get_settings()
    return bool(settings.schwab_app_key and settings.schwab_app_secret and settings.schwab_refresh_token)
