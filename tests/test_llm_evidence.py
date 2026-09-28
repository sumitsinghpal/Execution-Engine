"""
Tests for src/execution/llm_evidence.py — the bounded Groq/Gemini scoring
inputs to HedgeHog Silo's probabilistic gate. See that module's docstring
for the safety boundary: this returns a dict of floats and nothing else —
these tests exist to prove it fails closed (drops a score rather than
fabricating one) on every kind of bad input a real LLM call could produce,
and that it makes zero network calls at all when disabled.
"""

import httpx
import pytest

from src.config import Settings
from src.execution.llm_evidence import (
    CandidateContext,
    _extract_json_object,
    _validate_scores,
    compute_llm_evidence,
)


def _candidate():
    return CandidateContext(
        symbol="QQQ", strategy_id="golden_cross", entry_price=100.0,
        stop_loss_price=99.0, take_profit_price=102.0, rationale="forced golden cross",
    )


def _settings(**overrides):
    defaults = dict(_env_file=None, env="test", llm_evidence_enabled=True,
                     groq_api_key="test-groq-key", gemini_api_key="test-gemini-key")
    defaults.update(overrides)
    return Settings(**defaults)


class _FakeResponse:
    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)

    def json(self):
        return self._json


class _FakeAsyncClient:
    """Routes by URL substring to canned Groq/Gemini-shaped responses configured per test."""

    groq_content = None
    groq_raises = False
    gemini_content = None
    gemini_raises = False

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        if "groq" in url:
            if self.groq_raises:
                raise httpx.ConnectError("simulated groq outage")
            return _FakeResponse({"choices": [{"message": {"content": self.groq_content}}]})
        if "generativelanguage" in url:
            if self.gemini_raises:
                raise httpx.ConnectError("simulated gemini outage")
            return _FakeResponse({"candidates": [{"content": {"parts": [{"text": self.gemini_content}]}}]})
        raise ValueError(f"unexpected url in test: {url}")


@pytest.fixture
def fake_client(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    return _FakeAsyncClient


class TestExtractJsonObject:
    def test_extracts_a_plain_json_object(self):
        assert _extract_json_object('{"eig": 0.5}') == {"eig": 0.5}

    def test_extracts_json_wrapped_in_a_markdown_code_fence(self):
        text = '```json\n{"eig": 0.5, "regime": 0.7}\n```'
        assert _extract_json_object(text) == {"eig": 0.5, "regime": 0.7}

    def test_returns_none_for_text_with_no_json_object(self):
        assert _extract_json_object("I cannot rate this candidate.") is None

    def test_returns_none_for_malformed_json(self):
        assert _extract_json_object('{"eig": 0.5,}') is None


class TestValidateScores:
    def test_keeps_valid_in_range_scores(self):
        assert _validate_scores({"eig": 0.5, "regime": 0.7}) == {"eig": 0.5, "regime": 0.7}

    def test_clamps_out_of_range_scores(self):
        assert _validate_scores({"eig": 1.5, "regime": -0.3}) == {"eig": 1.0, "regime": 0.0}

    def test_drops_a_non_numeric_score_but_keeps_the_valid_one(self):
        assert _validate_scores({"eig": "high", "regime": 0.6}) == {"regime": 0.6}

    def test_drops_a_missing_key_entirely_rather_than_fabricating_it(self):
        assert _validate_scores({"eig": 0.5}) == {"eig": 0.5}
        assert "regime" not in _validate_scores({"eig": 0.5})

    def test_drops_nan_and_infinite_values(self):
        assert _validate_scores({"eig": float("nan"), "regime": float("inf")}) == {}

    def test_bool_is_not_treated_as_a_valid_score(self):
        """bool is a subclass of int in Python -- must not silently pass as 0.0/1.0."""
        assert _validate_scores({"eig": True, "regime": 0.5}) == {"regime": 0.5}


class TestComputeLlmEvidenceDisabledByDefault:
    @pytest.mark.asyncio
    async def test_returns_empty_and_makes_no_network_call_when_disabled(self, monkeypatch):
        calls = []
        monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: calls.append(1) or _FakeAsyncClient())
        settings = _settings(llm_evidence_enabled=False)

        result = await compute_llm_evidence(_candidate(), settings)

        assert result == {}
        assert calls == []

    @pytest.mark.asyncio
    async def test_returns_empty_when_enabled_but_no_api_keys_configured(self, monkeypatch):
        calls = []
        monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: calls.append(1) or _FakeAsyncClient())
        settings = _settings(llm_evidence_enabled=True, groq_api_key="", gemini_api_key="")

        result = await compute_llm_evidence(_candidate(), settings)

        assert result == {}
        assert calls == []


class TestComputeLlmEvidenceMerging:
    @pytest.mark.asyncio
    async def test_averages_scores_from_both_providers(self, fake_client):
        fake_client.groq_content = '{"eig": 0.8, "regime": 0.6}'
        fake_client.gemini_content = '{"eig": 0.4, "regime": 0.9}'
        fake_client.groq_raises = fake_client.gemini_raises = False

        result = await compute_llm_evidence(_candidate(), _settings())

        assert result["eig"] == pytest.approx(0.6)
        assert result["regime"] == pytest.approx(0.75)

    @pytest.mark.asyncio
    async def test_uses_the_other_providers_score_when_one_is_unavailable(self, fake_client):
        fake_client.groq_content = '{"eig": 0.9, "regime": 0.9}'
        fake_client.groq_raises = False
        fake_client.gemini_raises = True

        result = await compute_llm_evidence(_candidate(), _settings())

        assert result == {"eig": 0.9, "regime": 0.9}

    @pytest.mark.asyncio
    async def test_both_providers_failing_returns_empty_not_an_exception(self, fake_client):
        fake_client.groq_raises = True
        fake_client.gemini_raises = True

        result = await compute_llm_evidence(_candidate(), _settings())

        assert result == {}

    @pytest.mark.asyncio
    async def test_a_malformed_response_from_one_provider_does_not_block_the_other(self, fake_client):
        fake_client.groq_content = "not json at all"
        fake_client.groq_raises = False
        fake_client.gemini_content = '{"eig": 0.7, "regime": 0.3}'
        fake_client.gemini_raises = False

        result = await compute_llm_evidence(_candidate(), _settings())

        assert result == {"eig": 0.7, "regime": 0.3}

    @pytest.mark.asyncio
    async def test_only_groq_configured_still_returns_its_score(self, fake_client):
        fake_client.groq_content = '{"eig": 0.5, "regime": 0.5}'
        fake_client.groq_raises = False

        settings = _settings(gemini_api_key="")
        result = await compute_llm_evidence(_candidate(), settings)

        assert result == {"eig": 0.5, "regime": 0.5}
