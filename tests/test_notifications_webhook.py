"""
Unit tests for src/notifications/webhook.py — the generic outbound
notifier fired on kill switch trips/clears and autonomous trade
entries/exits. No real network calls: httpx is monkeypatched.
"""

import pytest

import src.notifications.webhook as webhook
from src.config import Settings


def _settings(webhook_url=None) -> Settings:
    return Settings(notification_webhook_url=webhook_url, _env_file=None)


class TestNotifySync:
    def test_no_op_when_webhook_url_not_configured(self, monkeypatch):
        called = []
        monkeypatch.setattr(webhook.httpx, "Client", lambda **kw: (_ for _ in ()).throw(AssertionError("must not construct a client when unconfigured")))

        webhook.notify_sync(_settings(webhook_url=None), "should not be sent")
        # No exception means the no-op path was taken correctly.

    def test_posts_slack_compatible_payload_to_the_configured_url(self, monkeypatch):
        posted = {}

        class FakeResponse:
            def raise_for_status(self):
                pass

        class FakeClient:
            def __init__(self, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, json):
                posted["url"] = url
                posted["json"] = json
                return FakeResponse()

        monkeypatch.setattr(webhook.httpx, "Client", FakeClient)

        webhook.notify_sync(_settings(webhook_url="https://hooks.example.com/abc"), "hello world")

        assert posted["url"] == "https://hooks.example.com/abc"
        assert posted["json"] == {"text": "hello world"}

    def test_swallows_any_failure_rather_than_raising(self, monkeypatch):
        class FailingClient:
            def __init__(self, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, json):
                raise ConnectionError("host unreachable")

        monkeypatch.setattr(webhook.httpx, "Client", FailingClient)

        # Must not raise — a dead webhook must never break the caller.
        webhook.notify_sync(_settings(webhook_url="https://dead.example.com"), "hello")

    def test_swallows_a_non_2xx_response(self, monkeypatch):
        import httpx as real_httpx

        class FakeResponse:
            def raise_for_status(self):
                raise real_httpx.HTTPStatusError("500", request=None, response=None)

        class FakeClient:
            def __init__(self, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, json):
                return FakeResponse()

        monkeypatch.setattr(webhook.httpx, "Client", FakeClient)

        webhook.notify_sync(_settings(webhook_url="https://hooks.example.com/abc"), "hello")


class TestNotifyAsync:
    @pytest.mark.asyncio
    async def test_delegates_to_notify_sync_off_the_event_loop(self, monkeypatch):
        calls = []
        monkeypatch.setattr(webhook, "notify_sync", lambda settings, text: calls.append((settings, text)))

        settings = _settings(webhook_url="https://hooks.example.com/abc")
        await webhook.notify(settings, "async hello")

        assert calls == [(settings, "async hello")]
