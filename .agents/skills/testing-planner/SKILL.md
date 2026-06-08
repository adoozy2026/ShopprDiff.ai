---
name: testing-planner
description: Test the Amazon search planner agent in isolation. Use when verifying planner changes, Amazon scraping, or query-building logic.
---

# Testing the Planner Agent

The planner agent (`orchestrator/app/agents/planner.py`) searches Amazon.com via Playwright and returns product candidate URLs.

## How to Run

Tests are shell-based (no browser recording needed). From `orchestrator/`:

```python
import asyncio
from app.agents.planner import run_planner, MAX_CANDIDATES

spec = {
    "product_class": "wireless noise cancelling headphones",
    "categories": {
        "budget": {"value": "$200", "type": "must_have"},
        "features": {"value": "bluetooth 5.0, ANC", "type": "must_have"},
    },
    "raw_query": "wireless noise cancelling headphones under $200",
}

drafts = asyncio.run(run_planner("test-001", spec))
```

Run with `PYTHONPATH=. uv run python <script>` from `orchestrator/`.

## Key Assertions

- `len(drafts) >= 1` and `<= MAX_CANDIDATES` (currently 4)
- Every `draft.source_url` starts with `https://www.amazon.com/dp/`
- Every `draft.source` equals `"amazon.com"`
- Every `draft.title` is non-empty and is a real product name (not a URL)
- Log output shows 2 distinct `amazon search:` URLs when spec has both budget and must_have categories

## Amazon Bot Detection

Amazon blocks headless Playwright requests that look automated. The planner works around this with:

1. **Realistic browser context**: `viewport`, `locale`, `timezone_id`, `Accept-Language` and `Accept` headers are required. Without these, Amazon serves a "Sorry, something went wrong" error page.
2. **Homepage warm-up**: Must visit `amazon.com` homepage first to establish session cookies. Direct navigation to `/s?k=...` fails.
3. **2-second wait**: After homepage visit, wait for cookies to settle before navigating to search URL.

If tests return 0 results, check these three things first. The title selector is `h2 span` (not `h2 a span` — Amazon's DOM may change over time).

## Verifying Google Search Removal

To confirm old Gemini/Google code is fully removed:

```bash
grep -cE 'google_search|GOOGLE_SEARCH_TOOL|genai_client|grounding|google\.genai' orchestrator/app/agents/planner.py
# Expected: 0
```

## No Secrets Required

The planner no longer calls Gemini, so no `GOOGLE_API_KEY` or similar is needed. Only Playwright + Chromium must be installed.

## Architecture Notes

- `_search_amazon(query)` — single Amazon search, returns `list[tuple[url, title]]`
- `run_planner(intent_id, spec)` — builds queries from spec categories, runs them concurrently via `asyncio.gather`, dedupes by ASIN
- `get_browser()` in `sources.py` — shared singleton Playwright browser instance
- Spec format uses weighted `categories` dict with `type: "must_have"` entries
