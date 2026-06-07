"""Search Planner agent — turns the spec into 5-8 candidate URLs.

One Gemini call with the ``google_search`` built-in tool. The model issues a
small query plan (broad + retailer-scoped variants); we read URLs and titles
out of ``response.candidates[0].grounding_metadata.grounding_chunks``, drop
non-product domains, dedupe, cap at 8.

We deliberately do NOT enable ``url_context`` here — the planner's job is
discovery, not page reading. The Researcher band (H7-H11) does the fetch +
extraction pass per candidate.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
from google.genai import types

from app.config import settings
from app.genai_client import get_client
from app.tools.sources import GOOGLE_SEARCH_TOOL

log = logging.getLogger(__name__)

MAX_CANDIDATES = 4

# Gemini wraps every grounded URL in a one-time-use redirect under this host.
# We resolve them to the real retailer URL before persisting so the dashboard
# (and the Researcher band) see a stable, click-through-friendly link.
_GROUNDING_REDIRECT_HOST = "vertexaisearch.cloud.google.com"

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

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
    # Unresolved Gemini grounding redirects fall through here when HEAD fails.
    "vertexaisearch.cloud.google.com",
}

SYSTEM_PROMPT = """You are a search planner for a personal shopping service.

You will be given a structured shopping spec. Your job is to find product
listings the user could actually buy. Use the google_search tool to run 3-4
queries: one broad ("<product class> <key constraints>"), plus 2-3 narrower
retailer-scoped queries — INCLUDING `site:amazon.com <product>` as one of
them. Other useful retailers: `site:ebay.com`, `site:bestbuy.com`,
`site:walmart.com`, `site:target.com`, `site:swappa.com`, `site:newegg.com`,
plus any in the spec's retailer_preferences. Prefer retailer product pages
over reviews, articles, or social media.

CRITICAL: ALWAYS include an `site:amazon.com` query among your search calls.
Google's grounded results underweight Amazon in shopping contexts; without an
explicit site-scoped query, Amazon listings vanish from results, which hurts
price comparison.

ALSO CRITICAL: Honor the spec's deal_breakers strictly. If the user said
anything like "US-based seller" or "ships from the US", restrict your queries
to US retailers — append `site:.com OR site:.us` or `"ships from United States"`
to broaden away from foreign listings. Never return links from .com.my,
.com.au, .co.uk, .de, .fr, .es, .it, .nl, .ca etc. when a US-only
constraint applies. The dashboard will hard-filter these afterward; you
help by not surfacing them in the first place.

You do not need to write any prose response. Just run the searches; we read
the grounding metadata directly. Be efficient with searches."""


# Country TLDs we drop when a US-only constraint is detected in the spec.
_FOREIGN_TLDS = (
    ".my", ".au", ".uk", ".de", ".fr", ".es", ".it", ".nl", ".ca",
    ".jp", ".kr", ".cn", ".hk", ".sg", ".id", ".th", ".vn", ".ph",
    ".br", ".mx", ".ar", ".ie", ".pl", ".se", ".no", ".fi", ".dk",
    ".be", ".at", ".ch", ".cz", ".tr", ".gr", ".pt", ".ru", ".za",
    ".nz", ".ae", ".sa", ".il", ".eg",
)

# Multi-segment country suffixes that .endswith() catches as a whole.
_FOREIGN_SUFFIXES = (
    ".com.my", ".com.au", ".co.uk", ".com.sg", ".com.hk", ".com.ph",
    ".com.br", ".com.mx", ".co.id", ".co.in", ".co.jp", ".co.kr",
    ".co.nz", ".co.za", ".com.tw", ".com.tr",
)


def _us_only_constraint(spec: dict[str, Any]) -> bool:
    """True if the spec's deal_breakers/notes hint at a US-only requirement."""
    haystack = " ".join(
        [
            *(spec.get("deal_breakers") or []),
            *(spec.get("must_haves") or []),
            spec.get("notes") or "",
            spec.get("raw_query") or "",
        ]
    ).lower()
    if not haystack:
        return False
    return any(
        tok in haystack
        for tok in ("us-based", "us based", "united states", "ships from us", "u.s.")
    )


def _passes_region_filter(url: str, us_only: bool) -> bool:
    if not us_only:
        return True
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return True
    if any(host.endswith(s) for s in _FOREIGN_SUFFIXES):
        return False
    last_dot = host.rfind(".")
    if last_dot != -1 and host[last_dot:] in _FOREIGN_TLDS:
        return False
    return True


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


def _extract_grounding_urls(response: Any) -> list[tuple[str, str]]:
    """Pull (url, title) pairs out of Gemini's grounding metadata."""
    out: list[tuple[str, str]] = []
    candidates = getattr(response, "candidates", None) or []
    for cand in candidates:
        gm = getattr(cand, "grounding_metadata", None)
        if gm is None:
            continue
        chunks = getattr(gm, "grounding_chunks", None) or []
        for ch in chunks:
            web = getattr(ch, "web", None)
            if web is None:
                continue
            url = getattr(web, "uri", None)
            title = getattr(web, "title", None)
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

    # Conflict-heavy specs (multiple condition tags from the chip UI) confuse
    # the model — it often returns without calling google_search at all when
    # the intent is "find me a Mac Studio that is brand-new AND open-box AND
    # gently-used AND certified-refurb". Build an explicit primary query that
    # collapses to the unambiguous parts (product class + budget + region),
    # and a secondary query that uses the rest as soft preferences.
    product_class = (spec.get("product_class") or spec.get("raw_query") or "").strip()
    budget = spec.get("budget_cents")
    budget_str = f"under ${budget / 100:.0f}" if isinstance(budget, int) else ""
    must_haves = ", ".join(spec.get("must_haves") or [])
    us_only_hint = " in United States" if _us_only_constraint(spec) else ""

    primary = f"{product_class} {budget_str}{us_only_hint}".strip()
    secondary = f"{product_class} {must_haves}".strip() if must_haves else primary
    amazon = f"site:amazon.com {product_class} {budget_str}".strip()

    user_msg = (
        "You MUST call the google_search tool at least THREE times — once "
        "with the PRIMARY query, once with the SECONDARY query, and once "
        "with the AMAZON query — to surface product listings. Do NOT answer "
        "from prior knowledge; product URLs are only useful if Google "
        "returned them.\n\n"
        f"PRIMARY query: {primary!r}\n"
        f"SECONDARY query: {secondary!r}\n"
        f"AMAZON query: {amazon!r}\n\n"
        f"Full spec for reference:\n{json.dumps(spec, indent=2)}"
    )

    client = get_client()
    log.info(
        "planner: intent_id=%s primary=%r secondary=%r", intent_id, primary, secondary
    )
    resp = await client.aio.models.generate_content(
        model=settings.gemini_model_researcher,
        contents=user_msg,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            tools=[GOOGLE_SEARCH_TOOL],
            max_output_tokens=2048,
        ),
    )

    raw = _extract_grounding_urls(resp)
    log.info("planner: %d grounding URLs returned", len(raw))
    if not raw:
        log.warning(
            "planner: empty grounding metadata — google_search likely never invoked "
            "for intent %s", intent_id
        )

    # Resolve grounding redirects in parallel so candidate.source_url is the
    # real retailer URL (not a one-time-use vertexai redirect).
    resolved = await _resolve_grounding_redirects(raw)

    us_only = _us_only_constraint(spec)
    seen: set[str] = set()
    drafts: list[CandidateDraft] = []
    rejected_foreign = 0
    for url, title in resolved:
        if url in seen:
            continue
        seen.add(url)
        if not _is_product_url(url):
            continue
        if not _passes_region_filter(url, us_only):
            rejected_foreign += 1
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

    log.info(
        "planner: %d candidates after filtering (us_only=%s, rejected_foreign=%d)",
        len(drafts),
        us_only,
        rejected_foreign,
    )
    return drafts


async def _resolve_grounding_redirects(
    pairs: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Follow any Gemini grounding redirects to their real destination.

    Two important quirks this handles:

    1. ``HEAD`` is unreliable. Amazon's CDN (and a few others) reject
       non-browser-fingerprint HEAD requests with 4xx/5xx, which made the
       previous implementation lose every Amazon URL the model returned. We
       use ``GET`` with a ``Range: bytes=0-0`` to download essentially
       nothing while still triggering the full redirect chain + presenting
       as a normal browser navigation.

    2. ``r.status_code`` doesn't matter for our purpose — even when the
       destination returns 403, httpx has *already* followed the redirect
       chain and ``r.url`` is the resolved URL we want. We only fall back to
       the original (unresolved vertexai redirect) when the request itself
       errored out before reaching the chain end.
    """
    needs_resolve = [
        (i, u)
        for i, (u, _) in enumerate(pairs)
        if _GROUNDING_REDIRECT_HOST in (urlparse(u).hostname or "")
    ]
    if not needs_resolve:
        return pairs

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=10.0,
        headers={
            "User-Agent": _BROWSER_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Range": "bytes=0-0",
        },
    ) as client:

        async def resolve(idx: int, url: str) -> tuple[int, str]:
            try:
                r = await client.get(url)
                # str(r.url) is the post-redirect URL even when the final
                # response is 4xx — exactly what we want for filtering.
                return idx, str(r.url)
            except Exception as e:
                log.debug("redirect resolve failed for %s: %s", url, e)
                return idx, url

        results = await asyncio.gather(*(resolve(i, u) for i, u in needs_resolve))

    out = list(pairs)
    for idx, real_url in results:
        out[idx] = (real_url, out[idx][1])
    return out
