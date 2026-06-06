"""Tool wiring for the agent pipeline.

Three tools are exposed:

  * **web_search** — Anthropic server tool. Claude executes searches on
    Anthropic's side; results come back as ``web_search_tool_result`` blocks.
    No client-side dispatch needed.
  * **web_fetch** — Anthropic server tool. Reads static HTML/PDF. Does NOT
    execute JavaScript; for JS-heavy pages, fall back to ``playwright_fetch``.
  * **playwright_fetch** — local client tool. We launch a headless chromium
    in-process when the model emits a ``tool_use`` block for this tool, then
    return the rendered text as a ``tool_result`` block.

In ``FIXTURE_MODE``, ``playwright_fetch`` short-circuits to canned page
content from ``fixtures.py`` so we never need a browser or network.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.config import settings
from app.tools.fixtures import fixture_fetch

log = logging.getLogger(__name__)


# ---- Anthropic server-tool blocks ---------------------------------------

WEB_SEARCH_TOOL: dict[str, Any] = {
    "type": "web_search_20260209",
    "name": "web_search",
    "max_uses": 8,
}

WEB_FETCH_TOOL: dict[str, Any] = {
    "type": "web_fetch_20260209",
    "name": "web_fetch",
    "max_uses": 10,
    "max_content_tokens": 60000,
}


def server_tools() -> list[dict[str, Any]]:
    """The Anthropic-side tool list to pass to messages.create(tools=...)."""
    return [WEB_SEARCH_TOOL, WEB_FETCH_TOOL]


# ---- Local client tool: playwright_fetch -------------------------------

PLAYWRIGHT_FETCH_TOOL: dict[str, Any] = {
    "name": "playwright_fetch",
    "description": (
        "Fetch a fully-rendered web page (JavaScript executed) when web_fetch "
        "returns thin or empty content. Use ONLY when web_fetch fails for "
        "a product page. Returns the visible text of the rendered DOM."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Fully-qualified URL of the product page to render.",
            },
            "wait_for_selector": {
                "type": "string",
                "description": (
                    "Optional CSS selector to wait for before extracting text. "
                    "Use when the page has skeletal HTML that hydrates async."
                ),
            },
        },
        "required": ["url"],
    },
}


def client_tools() -> list[dict[str, Any]]:
    return [PLAYWRIGHT_FETCH_TOOL]


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


# ---- Client-tool dispatcher ---------------------------------------------

ToolInput = dict[str, Any]


async def dispatch_client_tool(name: str, tool_input: ToolInput) -> str:
    """Route a model ``tool_use`` block to its handler.

    Server tools (web_search / web_fetch) never reach here — Anthropic runs
    them and the message stream contains the results directly.
    """
    if name == "playwright_fetch":
        url = tool_input.get("url")
        if not isinstance(url, str):
            return "[playwright_fetch error] missing url"
        return await playwright_fetch(url, tool_input.get("wait_for_selector"))
    return f"[dispatch error] unknown client tool: {name!r}"
