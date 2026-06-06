"""Search Planner agent — turns the spec into 5-8 candidate URLs.

One Anthropic Messages call with the ``web_search`` server tool. The model
issues a small query plan (broad query + 1-2 retailer-scoped variants), then
we extract URL/title pairs from ``web_search_tool_result`` blocks, normalize
by domain, dedupe, cap, and return as candidate dicts ready to insert.

We deliberately do NOT include ``web_fetch`` here — the planner's job is
discovery, not page reading. The Researcher band (H7-H11) does the fetch +
extraction pass per candidate.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from app.anthropic_client import get_client
from app.config import settings
from app.tools.sources import WEB_SEARCH_TOOL

log = logging.getLogger(__name__)

MAX_CANDIDATES = 8

# Domains that obviously aren't product listings — drop on sight.
_NON_PRODUCT_DOMAINS = {
    "reddit.com",
    "youtube.com",
    "youtu.be",
    "twitter.com",
    "x.com",
    "facebook.com",
    "instagram.com",
    "tiktok.com",
    "wikipedia.org",
    "quora.com",
}

SYSTEM_PROMPT = """You are a search planner for a personal shopping service.

You will be given a structured shopping spec. Your job is to find product
listings the user could actually buy. Use the web_search tool to run 2-3
queries: one broad ("<product class> <key constraints>"), plus 1-2 narrower
retailer-scoped queries (e.g. "site:ebay.com <product>", "site:swappa.com
<product>") matching the user's retailer preferences if any. Prefer retailer
product pages over reviews, articles, or social media.

You do not need to write any prose response. Just run the searches; we read
the search results directly. Be efficient with searches."""


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


def _is_product_url(url: str) -> bool:
    d = _domain(url)
    if not d:
        return False
    for bad in _NON_PRODUCT_DOMAINS:
        if d == bad or d.endswith("." + bad):
            return False
    # Heuristic: product URLs tend to be deeper than two path segments.
    path = urlparse(url).path or ""
    return path.count("/") >= 2


def _extract_results(response: Any) -> list[tuple[str, str]]:
    """Pull (url, title) pairs out of web_search_tool_result blocks."""
    out: list[tuple[str, str]] = []
    for block in response.content:
        if getattr(block, "type", None) != "web_search_tool_result":
            continue
        content = getattr(block, "content", None)
        if not isinstance(content, list):
            continue
        for item in content:
            t = getattr(item, "type", None) or (
                item.get("type") if isinstance(item, dict) else None
            )
            if t != "web_search_result":
                continue
            url = getattr(item, "url", None) or (item.get("url") if isinstance(item, dict) else None)
            title = getattr(item, "title", None) or (
                item.get("title") if isinstance(item, dict) else None
            )
            if isinstance(url, str) and isinstance(title, str):
                out.append((url, title))
    return out


_SUFFIX_RE = re.compile(r"\s*[-–—|]\s*(eBay|Amazon\.com|Best Buy|Target|Walmart)\b.*$", re.I)


def _clean_title(title: str) -> str:
    return _SUFFIX_RE.sub("", title).strip()


async def run_planner(intent_id: str, spec: dict[str, Any]) -> list[CandidateDraft]:
    """Run search planner. Returns up to MAX_CANDIDATES candidate drafts.

    The caller persists them to the candidates table.
    """
    if not isinstance(spec, dict):
        spec = {}
    user_msg = (
        "Find product listings matching this spec. Use 2-3 web searches "
        "(broad + retailer-scoped). Stop searching once you have ~10 likely results.\n\n"
        + json.dumps(spec, indent=2)
    )

    client = get_client()
    log.info("planner: intent_id=%s spec_keys=%s", intent_id, list(spec.keys()))
    resp = await client.messages.create(
        model=settings.anthropic_model_researcher,
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
        tools=[WEB_SEARCH_TOOL],
    )

    raw = _extract_results(resp)
    log.info("planner: %d raw search results", len(raw))

    seen: set[str] = set()
    drafts: list[CandidateDraft] = []
    for url, title in raw:
        if url in seen:
            continue
        seen.add(url)
        if not _is_product_url(url):
            continue
        drafts.append(
            CandidateDraft(
                title=_clean_title(title) or url,
                source=_domain(url),
                source_url=url,
            )
        )
        if len(drafts) >= MAX_CANDIDATES:
            break

    log.info("planner: %d candidates after filtering", len(drafts))
    return drafts
