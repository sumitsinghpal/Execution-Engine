"""Tests for src/agentic/llm_narrator.py — the template fallback path is the one that must always work with zero configuration."""

import httpx
import pytest

from src.agentic.llm_narrator import narrate_entry, narrate_exit
from src.config import Settings


def _settings(**overrides):
    return Settings(_env_file=None, env="test", **overrides)


class TestNarrateEntryFallback:
    @pytest.mark.asyncio
    async def test_no_key_configured_uses_template(self):
        settings = _settings(llm_narration_enabled=False, llm_api_key=None)

        text = await narrate_entry(
            settings,
            strategy_name="Golden Cross (50/200 SMA)",
            symbol="QQQ",
            side="BUY",
            entry_price=450.0,
            stop_loss=445.5,
            take_profit=459.0,
            rule_rationale="50-day SMA crossed above the 200-day SMA (Golden Cross).",
            reward_risk_ratio="2",
        )

        assert "QQQ" in text
        assert "450.00" in text
        assert "Golden Cross" in text

    @pytest.mark.asyncio
    async def test_enabled_but_no_key_still_uses_template(self):
        """narration_enabled alone isn't enough — a real key is also required, matching the Schwab opt-in pattern."""
        settings = _settings(llm_narration_enabled=True, llm_api_key=None)

        text = await narrate_entry(
            settings, strategy_name="Turtle", symbol="SPY", side="BUY",
            entry_price=500.0, stop_loss=490.0, take_profit=520.0,
            rule_rationale="New 20-day high.", reward_risk_ratio="2",
        )
        assert "SPY" in text


class TestNarrateExitFallback:
    @pytest.mark.asyncio
    async def test_no_key_configured_uses_template(self):
        settings = _settings(llm_narration_enabled=False, llm_api_key=None)

        text = await narrate_exit(
            settings, symbol="QQQ", exit_reason="take-profit",
            entry_price=450.0, exit_price=459.0, pnl_usd=90.0,
        )

        assert "QQQ" in text
        assert "profit" in text.lower()


class TestNarrateWithLLMConfigured:
    @pytest.mark.asyncio
    async def test_successful_llm_call_is_used_verbatim(self, monkeypatch):
        settings = _settings(llm_narration_enabled=True, llm_api_key="sk-test-key")

        class _FakeResponse:
            status_code = 200

            def json(self):
                return {"content": [{"type": "text", "text": "The model's exact narration."}]}

        class _FakeAsyncClient:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, *a, **k): return _FakeResponse()

        monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)

        text = await narrate_entry(
            settings, strategy_name="RSI(2) Pullback", symbol="IWM", side="BUY",
            entry_price=200.0, stop_loss=198.0, take_profit=204.0,
            rule_rationale="Oversold dip.", reward_risk_ratio="2",
        )

        assert text == "The model's exact narration."

    @pytest.mark.asyncio
    async def test_http_error_falls_back_to_template(self, monkeypatch):
        settings = _settings(llm_narration_enabled=True, llm_api_key="sk-test-key")

        class _FakeResponse:
            status_code = 401
            text = "unauthorized"

        class _FakeAsyncClient:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, *a, **k): return _FakeResponse()

        monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)

        text = await narrate_entry(
            settings, strategy_name="RSI(2) Pullback", symbol="IWM", side="BUY",
            entry_price=200.0, stop_loss=198.0, take_profit=204.0,
            rule_rationale="Oversold dip.", reward_risk_ratio="2",
        )

        assert "IWM" in text  # fell back to the deterministic template, not a raised exception

    @pytest.mark.asyncio
    async def test_network_failure_falls_back_to_template(self, monkeypatch):
        settings = _settings(llm_narration_enabled=True, llm_api_key="sk-test-key")

        class _FailingAsyncClient:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, *a, **k): raise httpx.ConnectTimeout("timed out")

        monkeypatch.setattr(httpx, "AsyncClient", _FailingAsyncClient)

        text = await narrate_exit(
            settings, symbol="TLT", exit_reason="stop-loss",
            entry_price=100.0, exit_price=98.0, pnl_usd=-20.0,
        )

        assert "TLT" in text  # never raised — a narration failure must not affect an already-completed trade
