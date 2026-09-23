"""
Tests for the CORS origin allowlist (see src/api/server.py's CORSMiddleware setup) —
never "*", which would let any page on the internet call this API using a visitor's
own browser as long as they also had the admin key.
"""

from src.config import Settings


def _settings(**overrides) -> Settings:
    defaults = dict(_env_file=None, env="test")
    defaults.update(overrides)
    return Settings(**defaults)


def test_default_allowlist_covers_the_deployed_dashboard_and_local_dev() -> None:
    origins = _settings().cors_allowed_origins
    assert "https://edge-trading-dashboard-app.vercel.app" in origins
    assert "http://localhost:3000" in origins
    assert "http://localhost:8000" in origins
    assert "*" not in origins


def test_custom_origins_override_the_default_list() -> None:
    settings = _settings(CORS_ALLOWED_ORIGINS="https://my-own-deploy.example.com")
    assert settings.cors_allowed_origins == ["https://my-own-deploy.example.com"]


def test_allowlist_setter_round_trips_through_the_raw_field() -> None:
    settings = _settings()
    settings.cors_allowed_origins = ["https://a.example.com", "https://b.example.com"]
    assert settings.cors_allowed_origins == ["https://a.example.com", "https://b.example.com"]
