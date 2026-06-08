"""NVIDIA NIM client (OpenAI-compatible) for the orchestrator.

Import ``get_client()`` for the AsyncOpenAI instance and ``rate_limiter``
for the shared 40 RPM throttle.
"""

from __future__ import annotations

import asyncio
import time
from functools import lru_cache

from openai import AsyncOpenAI

from app.config import settings


# ---------------------------------------------------------------------------
# Rate limiter — 40 requests per minute (user-specified NVIDIA limit).
# ---------------------------------------------------------------------------


class _RateLimiter:
    """Async sliding-window rate limiter."""

    def __init__(self, max_requests: int, period_seconds: float = 60.0) -> None:
        self._max = max_requests
        self._period = period_seconds
        self._timestamps: list[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                self._timestamps = [
                    t for t in self._timestamps if now - t < self._period
                ]
                if len(self._timestamps) < self._max:
                    self._timestamps.append(now)
                    return
                wait_time = self._period - (now - self._timestamps[0]) + 0.05
            await asyncio.sleep(wait_time)


rate_limiter = _RateLimiter(max_requests=40, period_seconds=60.0)


# ---------------------------------------------------------------------------
# Client singleton
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def get_client() -> AsyncOpenAI:
    if not settings.deepseek_api_key:
        raise RuntimeError("DEEPSEEK_API_KEY not set")
    return AsyncOpenAI(
        api_key=settings.deepseek_api_key,
        base_url="https://integrate.api.nvidia.com/v1",
    )
