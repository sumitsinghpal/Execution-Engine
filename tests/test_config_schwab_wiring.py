"""
Tests for the "add real API keys and it works" wiring: Settings
auto-registering a Schwab account alias, and build_broker_adapter picking
the right adapter for a given configuration.
"""

import pytest

from src.accounts.profiles import AccountProfile, BrokerName
from src.brokers.factory import build_broker_adapter
from src.brokers.paper import PaperBrokerAdapter
from src.brokers.schwab.adapter import SchwabBrokerAdapter
from src.config import Settings


def _settings(**overrides) -> Settings:
    """A hermetic Settings instance ignoring any local .env, like the test_settings fixture does."""
    defaults = dict(_env_file=None, env="test")
    defaults.update(overrides)
    return Settings(**defaults)


class TestSchwabAccountAliasAutoRegistration:
    def test_no_schwab_account_number_leaves_default_profiles_untouched(self):
        settings = _settings()
        assert "schwab_live" not in settings.account_profiles
        assert set(settings.account_profiles) == {"primary", "retirement", "paper"}

    def test_schwab_account_number_registers_the_configured_alias(self):
        settings = _settings(schwab_account_number="99999")
        assert "schwab_live" in settings.account_profiles
        profile = settings.account_profiles["schwab_live"]
        assert profile.broker == BrokerName.SCHWAB
        assert profile.account_hash is None  # resolved lazily by the adapter, not here

    def test_custom_alias_name_is_respected(self):
        settings = _settings(schwab_account_number="99999", schwab_account_alias="my_schwab")
        assert "my_schwab" in settings.account_profiles
        assert "schwab_live" not in settings.account_profiles

    def test_an_explicitly_preconfigured_alias_is_not_overwritten(self):
        """A caller that already populated account_profiles for this alias wins over auto-registration."""
        explicit_profile = AccountProfile(broker=BrokerName.SCHWAB, account_hash="pre-resolved-hash")
        settings = _settings(
            schwab_account_number="99999",
            account_profiles={"schwab_live": explicit_profile},
        )
        assert settings.account_profiles["schwab_live"].account_hash == "pre-resolved-hash"

    def test_registering_the_alias_does_not_auto_allow_it(self):
        """Registering the profile and allowing it to trade are deliberately separate steps."""
        settings = _settings(schwab_account_number="99999")
        assert "schwab_live" not in settings.account_allowlist


class TestBuildBrokerAdapter:
    def test_paper_execution_mode_returns_paper_adapter_even_with_schwab_configured(self):
        settings = _settings(
            execution_mode="PAPER",
            schwab_app_key="key",
            schwab_app_secret="secret",
            schwab_redirect_uri="https://localhost/callback",
            schwab_account_number="99999",
        )
        assert isinstance(build_broker_adapter(settings), PaperBrokerAdapter)

    def test_no_schwab_account_profile_returns_paper_even_outside_paper_mode(self):
        """Nothing in account_profiles actually uses SCHWAB, so there's nothing to switch to."""
        settings = _settings(execution_mode="LIVE_PREVIEW")
        assert isinstance(build_broker_adapter(settings), PaperBrokerAdapter)

    def test_schwab_mode_without_credentials_raises(self):
        settings = _settings(execution_mode="SCHWAB", schwab_account_number="99999")
        with pytest.raises(ValueError, match="Schwab mode requires"):
            build_broker_adapter(settings)

    def test_schwab_mode_with_credentials_returns_schwab_adapter(self):
        settings = _settings(
            execution_mode="SCHWAB",
            schwab_app_key="key",
            schwab_app_secret="secret",
            schwab_redirect_uri="https://localhost/callback",
            schwab_refresh_token="refresh",
            schwab_account_number="99999",
        )
        adapter = build_broker_adapter(settings)
        assert isinstance(adapter, SchwabBrokerAdapter)
        assert adapter.account_number == "99999"

    def test_mock_broker_flag_forces_paper_regardless_of_mode(self):
        settings = _settings(
            execution_mode="SCHWAB",
            schwab_app_key="key",
            schwab_app_secret="secret",
            schwab_redirect_uri="https://localhost/callback",
            schwab_account_number="99999",
        )
        assert isinstance(build_broker_adapter(settings, mock_broker=True), PaperBrokerAdapter)
