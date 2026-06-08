"""Search Planner agent — turns the spec into candidate URLs from Amazon.

Uses Playwright to search Amazon.com directly: navigates to the search-
results page, parses product titles and ASIN-based URLs out of the DOM,
dedupes, and caps at MAX_CANDIDATES.

The Researcher band (H7-H11) still does the fetch + extraction pass per
candidate.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote_plus, urlparse

from app.tools.sources import get_browser

log = logging.getLogger(__name__)

MAX_CANDIDATES = 4

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

_BUDGET_RE = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)")


def _extract_budget_str(spec: dict[str, Any]) -> str:
    """Extract a human-readable budget string (e.g. 'under $700') from categories.

    Scans category values for dollar amounts. Returns empty string if none found.
    """
    categories = spec.get("categories") or {}
    for entry in categories.values():
        if not isinstance(entry, dict):
            continue
        value = (entry.get("value") or "").strip()
        m = _BUDGET_RE.search(value)
        if m:
            return f"under {value}" if "under" not in value.lower() else value
    # Fallback: check the raw_query too.
    raw = spec.get("raw_query") or ""
    m = _BUDGET_RE.search(raw)
    if m:
        amount = m.group(0)
        return f"under {amount}"
    return ""


@dataclass
class CandidateDraft:
    title: str
    source: str
    source_url: str


def _domain(url: str) -> str:
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


_SUFFIX_RE = re.compile(
    r"\s*[-–—|]\s*(eBay|Amazon\.com|Best Buy|Target|Walmart)\b.*$", re.I
)


def _clean_title(title: str) -> str:
    return _SUFFIX_RE.sub("", title).strip()


async def _search_amazon(query: str) -> list[tuple[str, str]]:
    """Search Amazon.com for *query* and return (url, title) pairs.

    Uses Playwright to load the search-results page, then extracts product
    links and titles from the DOM. Returns up to ~20 results before the
    caller dedupes / caps.

    Amazon rejects direct cold navigation to ``/s?k=…`` from headless
    browsers (returns a "Sorry" error page). Visiting the homepage first
    establishes session cookies that make subsequent search requests
    succeed.
    """
    encoded = quote_plus(query)
    url = f"https://www.amazon.com/s?k={encoded}"
    log.info("amazon search: %s", url)

    browser = await get_browser()
    ctx = await browser.new_context(user_agent=_BROWSER_UA)
    page = await ctx.new_page()
    results: list[tuple[str, str]] = []
    try:
        # Warm up session: visit the homepage so Amazon sets cookies.
        await page.goto(
            "https://www.amazon.com", wait_until="domcontentloaded", timeout=20_000
        )
        await page.goto(url, wait_until="domcontentloaded", timeout=25_000)
        # Wait for the main results grid to appear.
        try:
            await page.wait_for_selector(
                '[data-component-type="s-search-result"]', timeout=10_000
            )
        except Exception:
            log.warning("amazon search: result cards did not appear for %r", query)

        cards = await page.query_selector_all(
            '[data-component-type="s-search-result"]'
        )
        for card in cards:
            # Each card has a data-asin attribute and a title link.
            asin = await card.get_attribute("data-asin")
            if not asin:
                continue
            title_el = await card.query_selector("h2 a span")
            title = (await title_el.inner_text()).strip() if title_el else ""
            if not title:
                continue
            product_url = f"https://www.amazon.com/dp/{asin}"
            results.append((product_url, title))
    except Exception as e:
        log.error("amazon search failed for %r: %s", query, e)
    finally:
        await ctx.close()

    log.info("amazon search: %d results for %r", len(results), query)
    return results


async def run_planner(intent_id: str, spec: dict[str, Any]) -> list[CandidateDraft]:
    """Run search planner. Returns up to MAX_CANDIDATES candidate drafts.

    Searches Amazon.com directly via Playwright. Builds 2-3 queries from
    the spec (broad, must-haves, budget-scoped) and merges results.
    """
    if not isinstance(spec, dict):
        spec = {}

    # Build explicit search queries from the structured spec. The primary
    # query uses the product class + budget; the secondary appends
    # high-importance category values as soft preferences.
    product_class = (spec.get("product_class") or spec.get("raw_query") or "").strip()
    budget_str = _extract_budget_str(spec)
    # Extract high-importance category values for the secondary query.
    categories = spec.get("categories") or {}
    must_have_values = [
        e["value"]
        for e in categories.values()
        if isinstance(e, dict) and e.get("type") == "must_have"
    ]
    must_haves = ", ".join(must_have_values)

    queries: list[str] = []
    # Primary: product class + budget
    primary = f"{product_class} {budget_str}".strip()
    if primary:
        queries.append(primary)
    # Secondary: product class + must-haves (only if meaningfully different)
    if must_haves:
        secondary = f"{product_class} {must_haves}".strip()
        if secondary != primary:
            queries.append(secondary)

    if not queries:
        log.warning("planner: no queries could be built for intent %s", intent_id)
        return []

    log.info("planner: intent_id=%s queries=%r", intent_id, queries)

    # Run all Amazon searches concurrently.
    all_results = await asyncio.gather(*(_search_amazon(q) for q in queries))

    # Flatten, dedupe by ASIN (embedded in the URL), cap at MAX_CANDIDATES.
    seen: set[str] = set()
    drafts: list[CandidateDraft] = []
    for batch in all_results:
        for url, title in batch:
            if url in seen:
                continue
            seen.add(url)
            drafts.append(
                CandidateDraft(
                    title=_clean_title(title) or url,
                    source=_domain(url),
                    source_url=url,
                )
            )
            if len(drafts) >= MAX_CANDIDATES:
                break
        if len(drafts) >= MAX_CANDIDATES:
            break

    log.info("planner: %d candidates from Amazon for intent %s", len(drafts), intent_id)
    return drafts
