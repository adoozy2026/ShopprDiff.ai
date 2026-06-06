"""Single AsyncAnthropic instance for the orchestrator. Import get_client()."""

from functools import lru_cache

from anthropic import AsyncAnthropic

from app.config import settings


@lru_cache(maxsize=1)
def get_client() -> AsyncAnthropic:
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    return AsyncAnthropic(api_key=settings.anthropic_api_key)
