"""Single Google GenAI client for the orchestrator. Import get_client()."""

from functools import lru_cache

from google import genai

from app.config import settings


@lru_cache(maxsize=1)
def get_client() -> genai.Client:
    if not settings.google_api_key:
        raise RuntimeError("GOOGLE_API_KEY not set")
    return genai.Client(api_key=settings.google_api_key)
