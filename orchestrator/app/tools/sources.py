"""Tool wiring for the agent pipeline (DeepSeek stack).

Two tools are exposed:

  * **playwright_fetch** — renders a page with headless Chromium and returns
    the visible DOM text.  Used for product-page extraction (replaces the
    former Gemini ``url_context`` server tool) and as a fallback for
    JavaScript-heavy sites.
  * **get_browser** — lazy-launch a singleton Chromium instance.

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


# ---- Playwright execution ----------------------------------------------

_PLAYWRIGHT_LOCK = asyncio.Lock()
_BROWSER = None  # cached chromium instance


async def get_browser():
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
        browser = await get_browser()
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
    """Execute a tool call and return the response payload."""
    if name == "playwright_fetch":
        url = args.get("url")
        if not isinstance(url, str):
            return {"error": "playwright_fetch: missing url"}
        text = await playwright_fetch(url, args.get("wait_for_selector"))
        return {"content": text}
    return {"error": f"unknown client tool: {name!r}"}
