"""Per-candidate researcher loop.

Each researcher runs in its own asyncio task. It writes a ``researcher_findings``
row up front and then PATCHes it through 4 progressive steps so the dashboard
animates as work happens. Insforge realtime triggers fire on every UPDATE, so
the browser sees each transition without polling.

Steps:
  1. ``fetching listing``  — Playwright fetch + DeepSeek extraction of
     price / condition / seller / shipping / returns.
  2. ``checking seller reputation`` — DeepSeek assessment of the seller
     based on its training knowledge.
  3. ``scanning known issues``     — DeepSeek summary of commonly reported
     problems for the product class.
  4. ``evaluating``                — local scam scoring. No model call.

Errors at any step flip the finding to ``status='error'`` with the exception
message in ``log`` and a partial finding payload still attached.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from pydantic import BaseModel

from app.agents.configurator import (
    configure_and_extract,
    extract_from_text,
    should_escalate,
)
from app.agents.scam import score_scam
from app.config import settings
from app.db.client import InsforgeClient
from app.genai_client import get_client, rate_limiter
from app.tools.page_meta import fetch_page_meta
from app.tools.sources import playwright_fetch

# Cap the number of candidates per intent that get the browser-agent
# escalation. Each escalation is +15-45s of latency and several extra
# DeepSeek calls — without a cap, a single query can balloon past 3 min.
MAX_ESCALATIONS_PER_INTENT = 2

log = logging.getLogger(__name__)


# ---- Structured-output schemas ------------------------------------------


class ListingFacts(BaseModel):
    title: str | None = None
    price_cents: int | None = None
    shipping_cost_cents: int | None = None


# ---- Public entry point -------------------------------------------------


async def run_researcher(
    client: InsforgeClient,
    candidate: dict[str, Any],
    spec: dict[str, Any],
    *,
    escalation_budget: list[int] | None = None,
) -> None:
    """Research one candidate end-to-end, optionally escalating to the
    browser-agent configurator if the static extract is thin or the URL
    is on a known-configurable retailer.

    ``escalation_budget`` is a single-element mutable list (so siblings
    share state via asyncio.gather). Each researcher that escalates
    decrements it. None disables escalation entirely.
    """
    candidate_id = candidate["id"]
    intent_id = candidate["intent_id"]
    label = candidate.get("source") or "researcher"

    rows = await client.insert(
        "researcher_findings",
        {
            "candidate_id": candidate_id,
            "intent_id": intent_id,
            "agent_label": label,
            "step": "queued",
            "status": "queued",
            "finding": {},
        },
    )
    finding_id = rows[0]["id"]
    finding: dict[str, Any] = {}

    async def step(name: str, status: str, partial: dict[str, Any] | None = None) -> None:
        if partial:
            finding.update(partial)
        await client.update(
            "researcher_findings",
            where={"id": f"eq.{finding_id}"},
            patch={"step": name, "status": status, "finding": finding},
        )

    await client.update(
        "candidates",
        where={"id": f"eq.{candidate_id}"},
        patch={"status": "researching"},
    )

    try:
        await step("fetching listing", "running")
        # Pull OpenGraph meta in parallel with the LLM extract. The meta
        # scrape is free and works even when the API is rate-limited.
        (listing, spec_attrs), meta = await asyncio.gather(
            _extract_listing(candidate["source_url"], spec),
            fetch_page_meta(candidate["source_url"]),
            return_exceptions=False,
        )
        if not listing.title and meta.title:
            listing.title = meta.title
        listing_payload = listing.model_dump(exclude_none=False)
        listing_payload["spec_attrs"] = spec_attrs
        if meta.description:
            listing_payload["description_summary"] = meta.description[:300]
        await step("extracted listing", "running", listing_payload)

        # ---- Optional browser-agent escalation ----
        if (
            escalation_budget is not None
            and escalation_budget[0] > 0
            and should_escalate(candidate["source_url"], listing.price_cents)
        ):
            escalation_budget[0] -= 1
            cfg = await configure_and_extract(
                candidate["source_url"],
                spec,
                update_step=lambda msg: step(msg, "running"),
            )
            if cfg.steps > 0:
                finding["configurator_steps"] = cfg.steps
                finding["configurator_history"] = [
                    {"action": h.action, "reason": h.reason} for h in cfg.history
                ]
                fresh = await extract_from_text(cfg.text)
                if fresh:
                    for k, v in fresh.items():
                        finding[k] = v
                        if hasattr(listing, k):
                            setattr(listing, k, v)
                    await step("merged configured listing", "running", finding)

        if listing.price_cents:
            await client.update(
                "candidates",
                where={"id": f"eq.{candidate_id}"},
                patch={"raw_price_cents": listing.price_cents},
            )

        await step("checking seller reputation", "running")
        seller_name = (spec_attrs.get("seller") or finding.get("seller"))
        seller_rep = await _assess_seller(seller_name, candidate.get("source"))
        await step("evaluating seller", "running", {"seller_rep": seller_rep})

        await step("scanning known issues", "running")
        product_class = spec.get("product_class") or listing.title or candidate["title"]
        issues = await _find_known_issues(product_class)
        finding["known_issues"] = issues

        scam_score, scam_reasons = score_scam(finding, spec)
        finding["scam_score"] = scam_score
        finding["scam_reasons"] = scam_reasons
        finding["confidence"] = "low" if not listing.price_cents else "medium"

        await client.update(
            "researcher_findings",
            where={"id": f"eq.{finding_id}"},
            patch={"step": "done", "status": "done", "finding": finding},
        )
        await client.update(
            "candidates",
            where={"id": f"eq.{candidate_id}"},
            patch={"status": "done"},
        )
        log.info("researcher done: candidate_id=%s scam=%d", candidate_id, scam_score)

    except Exception as e:
        log.exception("researcher failed: candidate_id=%s", candidate_id)
        try:
            await client.update(
                "researcher_findings",
                where={"id": f"eq.{finding_id}"},
                patch={
                    "step": "error",
                    "status": "error",
                    "log": repr(e)[:500],
                    "finding": finding,
                },
            )
            await client.update(
                "candidates",
                where={"id": f"eq.{candidate_id}"},
                patch={"status": "error"},
            )
        except Exception:
            log.exception("could not record researcher error for %s", candidate_id)


# ---- Step helpers (DeepSeek calls) ----------------------------------------


_EXTRACT_SYSTEM = """You read a single product listing page text and extract a
structured summary of what's actually for sale.

Your reply MUST be one raw JSON object — no prose, no Markdown, no code
fences, no array wrapper. Be conservative: leave fields null if the page
doesn't clearly state them. Do NOT invent prices, conditions, or sellers.
price_cents and shipping_cost_cents must be integers in US cents. If the
page shows a price range, use the lowest. For spec_attrs, fill only the
attribute fields you can identify — leave a field null when unknown rather
than guessing."""


# The JSON extraction instruction is built dynamically per-request so the
# ``spec_attrs`` section reflects categories from the intake agent's spec.

_EXTRACT_JSON_CORE = """Reply with ONLY a JSON object — no prose,
no code fences — matching exactly this shape:

{
  "title": string|null,
  "price_cents": integer|null,
  "shipping_cost_cents": integer|null"""

_EXTRACT_JSON_FOOTER = """Use null for fields the page does not state. price_cents and
shipping_cost_cents are integers in US cents."""


def _build_extract_instruction(spec: dict[str, Any]) -> str:
    """Build the JSON extraction instruction dynamically from the intake spec.

    Only title, price_cents, and shipping_cost_cents are universal. Every other
    attribute the researcher extracts is derived from the spec's categories.
    """
    categories = spec.get("categories") or {}

    attr_schema_lines: list[str] = []
    attr_guide_lines: list[str] = []
    for cat_name, entry in categories.items():
        if not isinstance(entry, dict):
            continue
        key = cat_name.lower().replace(" ", "_").replace("-", "_")
        value_hint = entry.get("value", "")
        attr_schema_lines.append(f'    "{key}": string|null')
        attr_guide_lines.append(
            f"  - {key}: extract the listing's {cat_name}"
            + (f" (user wants: {value_hint})" if value_hint else "")
        )

    if attr_schema_lines:
        spec_block = (
            ',\n  "spec_attrs": {\n'
            + ",\n".join(attr_schema_lines)
            + "\n  }\n}"
        )
        guide = (
            "\nspec_attrs field guide — extract each attribute as stated on "
            "the listing page:\n" + "\n".join(attr_guide_lines)
        )
    else:
        spec_block = ',\n  "spec_attrs": {}\n}'
        guide = ""

    return (
        _EXTRACT_JSON_CORE
        + spec_block
        + "\n\n"
        + _EXTRACT_JSON_FOOTER
        + guide
    )


def _strip_code_fence(text: str) -> str:
    t = text.strip()
    if not t.startswith("```"):
        return t
    t = t.strip("`")
    if "\n" in t:
        # Drop optional language tag on the first line.
        first, rest = t.split("\n", 1)
        if first.strip().isalpha():
            t = rest
    return t.removesuffix("```").strip()


async def _extract_listing(
    url: str, spec: dict[str, Any]
) -> tuple[ListingFacts, dict[str, Any]]:
    """Playwright fetch + DeepSeek extraction, JSON-as-text output.

    Returns ``(listing, spec_attrs)`` where ``spec_attrs`` contains the
    dynamic attributes derived from the intake spec's categories.
    """
    # Fetch page content via Playwright (replaces Gemini url_context).
    page_text = await playwright_fetch(url)
    if not page_text or page_text.startswith("[playwright_fetch error]"):
        log.warning("extract: playwright_fetch failed for %s", url)
        return ListingFacts(), {}

    # Truncate to avoid blowing the context window.
    page_text = page_text[:8000]

    client = get_client()
    instruction = _build_extract_instruction(spec)
    prompt = (
        f"Here is the rendered text of a product listing page at {url}:\n\n"
        f"{page_text}\n\n"
        + instruction
    )
    try:
        await rate_limiter.acquire()
        resp = await client.chat.completions.create(
            model=settings.deepseek_model_researcher,
            messages=[
                {"role": "system", "content": _EXTRACT_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            max_tokens=1024,
        )
    except Exception as e:
        log.warning("extract: DeepSeek call failed for %s: %s", url, e)
        return ListingFacts(), {}

    text = _strip_code_fence(resp.choices[0].message.content or "")
    data = _coerce_listing_json(text)
    if data is None:
        log.warning("extract: could not coerce JSON for %s; text=%r", url, text[:200])
        return ListingFacts(), {}
    spec_attrs = data.pop("spec_attrs", {}) or {}
    try:
        return ListingFacts(**data), spec_attrs
    except Exception as e:
        log.warning("extract: schema validation failed: %s; data keys=%s", e, list(data.keys()))
        return ListingFacts(), spec_attrs


def _coerce_listing_json(text: str) -> dict[str, Any] | None:
    """Recover a single JSON object from messy LLM output.

    Accepts:
      * a bare object  `{...}`
      * a JSON array of objects (take the first)
      * prose surrounding an embedded object (extract the first `{...}` slice)
    """
    if not text:
        return None
    # Try strict parse first.
    try:
        data = json.loads(text)
    except Exception:
        data = None

    if isinstance(data, dict):
        return data
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]

    # Last-ditch: pull the first balanced {...} substring out of the prose.
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    return json.loads(text[start : i + 1])
                except Exception:
                    start = -1
    return None


_SELLER_SYSTEM = """You assess the trustworthiness of an online seller. The
user gives you a seller/retailer name. Based on your knowledge, provide a
brief assessment: customer reviews reputation, any known scam reports, BBB
complaints, Trustpilot rating if known. Reply in plain text, 1-2 sentences.
If you don't have specific information, say so explicitly — don't make up
signal."""


async def _assess_seller(seller: str | None, fallback_retailer: str | None) -> str:
    name = (seller or fallback_retailer or "").strip()
    if not name:
        return "no seller identified"
    client = get_client()
    prompt = f"Assess the trustworthiness of this seller: {name!r}."
    await rate_limiter.acquire()
    resp = await client.chat.completions.create(
        model=settings.deepseek_model_researcher,
        messages=[
            {"role": "system", "content": _SELLER_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        max_tokens=512,
    )
    return (resp.choices[0].message.content or "").strip()


_ISSUES_SYSTEM = """You research common known issues for a product. The user
gives you a product class (e.g. "used iPhone 15 Pro 256GB"). Based on your
knowledge, return a JSON array of up to 4 short bullet strings (each <120
chars). Focus on the product itself — do NOT include seller-specific
complaints. Return ONLY a JSON array, no other text."""


class IssuesResponse(BaseModel):
    issues: list[str] = []


async def _find_known_issues(product_class: str) -> list[str]:
    if not product_class:
        return []
    client = get_client()
    prompt = f"What are commonly reported issues for: {product_class!r}?"
    try:
        await rate_limiter.acquire()
        resp = await client.chat.completions.create(
            model=settings.deepseek_model_researcher,
            messages=[
                {"role": "system", "content": _ISSUES_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            max_tokens=512,
        )
    except Exception as e:
        log.warning("known-issues call failed: %s", e)
        return []

    text = (resp.choices[0].message.content or "").strip()
    if not text:
        return []
    # Strip code fences.
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text[:-3]
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return [str(x)[:200] for x in data][:4]
        if isinstance(data, dict) and isinstance(data.get("issues"), list):
            return [str(x)[:200] for x in data["issues"]][:4]
    except Exception:
        pass
    # Fall back: take bullet-shaped lines if JSON parsing failed.
    lines = [
        ln.lstrip("-*\u2022 ").strip()
        for ln in text.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    return [ln for ln in lines if ln][:4]


# ---- Fan-out helper used by the orchestrator ---------------------------


async def run_all_researchers(
    client: InsforgeClient,
    candidates: list[dict[str, Any]],
    spec: dict[str, Any],
) -> None:
    """Fan out researchers with a stagger so we don't burst the per-minute
    DeepSeek quota. We cap concurrency to 3 and add a ~0.5s offset between
    starts.
    """
    if not candidates:
        return
    log.info("dispatching %d researchers", len(candidates))

    sem = asyncio.Semaphore(3)
    # Shared budget so siblings can collectively cap escalations per intent.
    budget = [MAX_ESCALATIONS_PER_INTENT]

    async def runner(idx: int, c: dict[str, Any]) -> None:
        await asyncio.sleep(0.5 * idx)
        async with sem:
            await run_researcher(client, c, spec, escalation_budget=budget)

    await asyncio.gather(
        *(runner(i, c) for i, c in enumerate(candidates)),
        return_exceptions=True,
    )
