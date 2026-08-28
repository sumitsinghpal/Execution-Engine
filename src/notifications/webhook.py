"""
Generic outbound webhook notifier. Posts a Slack-compatible {"text": ...}
JSON payload to settings.notification_webhook_url — the one shape both
Slack's incoming-webhook endpoint and Discord's Slack-compatible webhook
endpoint (any Discord webhook URL with /slack appended) accept directly,
so this needs no per-provider branching. Point it at Slack, Discord, or
anything else that speaks the same shape.

A no-op whenever the URL isn't configured, so every call site can call
this unconditionally — no separate "is notifications enabled" check
scattered across callers. Every failure (bad URL, host down, timeout,
non-2xx response) is logged and swallowed, never raised: this system's
job is trading, and a flaky notification endpoint must never block or
fail a trade, a kill-switch trip, or anything else that already happened
by the time this runs.
"""

from __future__ import annotations

import asyncio

import httpx

from src.config import Settings
from src.logging_config import get_logger

logger = get_logger(__name__)

_TIMEOUT_SECONDS = 5.0


def notify_sync(settings: Settings, text: str) -> None:
    """
    Synchronous send, for call sites that aren't async and shouldn't need
    to become async just to fire a notification — e.g.
    KillSwitchService.set_state, which is called from both sync and async
    contexts and must not change its own signature to accommodate this.
    """
    url = settings.notification_webhook_url
    if not url:
        return
    try:
        with httpx.Client(timeout=_TIMEOUT_SECONDS) as client:
            response = client.post(url, json={"text": text})
            response.raise_for_status()
    except Exception as exc:
        logger.warning("notification_webhook_failed", error=str(exc))


async def notify(settings: Settings, text: str) -> None:
    """
    Async wrapper for call sites already running in an async context
    (autonomous_trader.py) — runs the blocking HTTP call in a thread so a
    slow or unreachable webhook host never stalls the event loop the
    autonomous trading cycle itself runs on.
    """
    await asyncio.to_thread(notify_sync, settings, text)


__all__ = ["notify", "notify_sync"]
