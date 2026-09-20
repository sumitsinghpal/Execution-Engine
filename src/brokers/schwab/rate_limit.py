"""
Client-side rate limiter for Schwab's API.

Schwab publishes a limit of 120 calls per minute per app. Retrying politely after a
429 is not enough on its own: with several background loops (price alerts, the
autonomous trader, the bracket monitor, the strategy scanner) and a dashboard all
sharing one app, the sensible thing is to never send the 121st call in the first
place. A sliding window: at most `max_calls` in any `per_seconds`. It WAITS rather
than erroring, so a burst becomes a short delay, not a failure.

Process-wide by design: the limit belongs to the app, so every SchwabBrokerAdapter
sharing an app key must share one limiter (see src/brokers/factory.py, which caches
the adapter). With N server workers each process has its own window, so set
SCHWAB_RATE_LIMIT_PER_MINUTE to 120 / N.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Awaitable, Callable


class RateLimiter:
    def __init__(
        self,
        max_calls: int = 120,
        per_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if max_calls < 1:
            raise ValueError("max_calls must be at least 1")
        self.max_calls = max_calls
        self.per_seconds = per_seconds
        self._clock = clock
        self._sleep = sleep
        self._calls: deque[float] = deque()
        self._lock: asyncio.Lock | None = None

    async def acquire(self) -> None:
        """Returns once a call slot is free. Serialized so concurrent callers cannot all claim the last slot."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            while True:
                now = self._clock()
                while self._calls and now - self._calls[0] >= self.per_seconds:
                    self._calls.popleft()
                if len(self._calls) < self.max_calls:
                    self._calls.append(now)
                    return
                await self._sleep(max(self.per_seconds - (now - self._calls[0]), 0.001))
