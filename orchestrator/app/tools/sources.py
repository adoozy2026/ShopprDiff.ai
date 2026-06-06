"""Tool wiring for the agent pipeline (Gemini stack).

Three tools are exposed:

  * **google_search** — Gemini built-in server tool. Google runs the search and
    returns grounding metadata. No client-side dispatch needed.
  * **url_context** — Gemini built-in server tool. Reads URLs the model sees
    in context. Does not fully render JavaScript pages; for those, fall back
    to ``playwright_fetch``.
  * **playwright_fetch** — local function declaration. Gemini emits a
    ``FunctionCall`` block for it; we run a headless chromium and reply with
    a ``FunctionResponse`` part.

In ``FIXTURE_MODE``, ``playwright_fetch`` short-circuits to canned page
content from ``fixtures.py`` so we never need a browser or network.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from google.genai import types

from app.config import settings
from app.tools.fixtures import fixture_fetch

log = logging.getLogger(__name__)


# ---- Gemini server-tool bundle ------------------------------------------

GOOGLE_SEARCH_TOOL = types.Tool(google_search=types.GoogleSearch())
URL_CONTEXT_TOOL = types.Tool(url_context=types.UrlContext())


def server_tools() -> list[types.Tool]:
    """Server-side tool list (search + url context)."""
    return [GOOGLE_SEARCH_TOOL, URL_CONTEXT_TOOL]


# ---- Local client tool: playwright_fetch -------------------------------

PLAYWRIGHT_FETCH_DECLARATION = types.FunctionDeclaration(
    name="playwright_fetch",
    description=(
        "Fetch a fully-rendered web page (JavaScript executed) when url_context "
        "returns thin or empty content. Use ONLY after url_context fails for a "
        "product page. Returns the visible text of the rendered DOM."
    ),
    parameters={
        "type": "OBJECT",
        "properties": {
            "url": {
                "type": "STRING",
                "description": "Fully-qualified URL of the product page to render.",
            },
            "wait_for_selector": {
                "type": "STRING",
                "description": (
                    "Optional CSS selector to wait for before extracting text. "
                    "Use when the page has skeletal HTML that hydrates async."
                ),
            },
        },
        "required": ["url"],
    },
)

PLAYWRIGHT_TOOL = types.Tool(function_declarations=[PLAYWRIGHT_FETCH_DECLARATION])


def client_tools() -> list[types.Tool]:
    return [PLAYWRIGHT_TOOL]


# ---- Playwright execution ----------------------------------------------

_PLAYWRIGHT_LOCK = asyncio.Lock()
_BROWSER = None  # cached chromium instance


async def _get_browser():
    """Lazy-launch a singleton chromium so per-call cost stays low."""
    global _BROWSER
    if _BROWSER is not None:
        return _BROWSER
    async with _PLAYWRIGHT_LOCK:
        if _BROWSER is None:
            from playwright.async_api import async_playwright

            pw = await async_playwright().start()
            _BROWSER = await pw.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            log.info("playwright chromium launched")
    return _BROWSER


async def playwright_fetch(url: str, wait_for_selector: str | None = None) -> str:
    """Render ``url`` and return the visible text.

    Honors ``FIXTURE_MODE``: returns canned content from ``fixtures.py``
    without launching a browser. Real-mode catches its own errors so the
    agent loop can keep going on a partial result.
    """
    if settings.fixture_mode:
        log.debug("playwright_fetch FIXTURE_MODE: %s", url)
        return fixture_fetch(url)

    try:
        browser = await _get_browser()
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
            )
        )
        page = await ctx.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
            if wait_for_selector:
                try:
                    await page.wait_for_selector(wait_for_selector, timeout=8_000)
                except Exception as e:
                    log.warning("wait_for_selector(%r) timed out: %s", wait_for_selector, e)
            text = await page.evaluate("() => document.body.innerText")
            return (text or "").strip()
        finally:
            await ctx.close()
    except Exception as e:
        log.error("playwright_fetch failed for %s: %s", url, e)
        return f"[playwright_fetch error] {e}"


async def shutdown_browser() -> None:
    """Close the cached chromium. Call from FastAPI lifespan teardown."""
    global _BROWSER
    if _BROWSER is None:
        return
    try:
        await _BROWSER.close()
    finally:
        _BROWSER = None


# ---- Function-call dispatcher ------------------------------------------

ToolInput = dict[str, Any]


async def dispatch_function_call(name: str, args: ToolInput) -> dict[str, Any]:
    """Execute a Gemini ``FunctionCall`` and return the response payload.

    Server tools (google_search, url_context) never reach here — Gemini runs
    them inline and exposes results via grounding metadata.
    """
    if name == "playwright_fetch":
        url = args.get("url")
        if not isinstance(url, str):
            return {"error": "playwright_fetch: missing url"}
        text = await playwright_fetch(url, args.get("wait_for_selector"))
        return {"content": text}
    return {"error": f"unknown client tool: {name!r}"}
