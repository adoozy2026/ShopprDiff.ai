"""Synthesizer — turns N completed researcher findings into a HOLISTIC shop
recommendation, not a ranked list.

The earlier version just produced a single rationale + alternatives list,
which was indistinguishable from a Google Shopping top-N. This one produces:

  * ``picks``      one-liner per ranked candidate so each tile renders WHY
                    it's worth showing (not just price + condition).
  * ``tradeoffs``  axis-by-axis "if you optimize for X, pick Y" insights —
                    price vs. return policy vs. shipping vs. seller trust.
                    This is the part that makes it a shopping ADVISOR rather
                    than a sorted list.
  * ``warnings``   honest concerns surfaced front-and-center: no returns,
                    thin seller history, variant mismatches, ships from
                    overseas, etc.
  * ``rationale``  short markdown explaining why the top pick wins for
                    THIS user given their stated spec (referenced
                    explicitly, not assumed).
  * ``alternatives`` adjacent paths worth considering — cheaper variant,
                    refurb route, "if you can wait" angle.

JSON-as-text output is parsed defensively because Gemini's response_schema
mode conflicts with tool use, and pro models occasionally wrap output in
code fences.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from google.genai import types
from pydantic import BaseModel

from app.config import settings
from app.db.client import InsforgeClient
from app.genai_client import get_client

log = logging.getLogger(__name__)


class Alternative(BaseModel):
    title: str
    why_consider: str


class CandidateNote(BaseModel):
    candidate_id: str
    score: int  # 0-100
    one_liner: str  # surfaced on the tile
    detail: str | None = None  # surfaced when expanded


class TradeoffInsight(BaseModel):
    axis: str  # e.g. "price", "return policy", "seller trust"
    winner_candidate_id: str | None = None
    summary: str


class SynthOutput(BaseModel):
    top_pick_candidate_id: str | None = None
    rationale: str = ""
    picks: list[CandidateNote] = []
    tradeoffs: list[TradeoffInsight] = []
    warnings: list[str] = []
    alternatives: list[Alternative] = []


SYSTEM_PROMPT = """You are the final stage of a personal shopping pipeline.
Your job is to make this feel like a shopping ADVISOR, not a sorted product
grid the user could have generated themselves with Google Shopping.

You receive: (1) the user's structured shopping spec with weighted categories,
and (2) a JSON array of researcher findings — one per candidate listing. Each
finding has: candidate_id, title, source (retailer domain), source_url,
price_cents, condition, seller, shipping fields, return_policy, known_issues,
scam_score, scam_reasons, and seller_rep.

The spec uses a weighted categories system:
- Each category has a "value" (what the user wants), "importance" (0.0-1.0),
  and "type" (must_have, preference, deal_breaker, neutral).
- Categories with importance >= 0.8 are critical priorities.
- "must_have" categories are non-negotiable requirements.
- "deal_breaker" categories disqualify listings that violate them.
- "preference" categories influence ranking by their importance weight.
- Budget, condition, shipping preferences etc. are all expressed as categories.

You produce a structured recommendation with four kinds of analysis:

1. PICKS — for every candidate you'd show the user, write one short line
   (≤ 18 words) explaining why this listing is on screen. Reference a
   concrete trait the user cares about ("cheapest US-based option",
   "only one with a 1-year warranty", "best return policy for $50 more").
   Skip candidates that are clearly junk.

2. TRADEOFFS — by axis, surface WHO wins and WHY. Use the spec's category
   names as axes where data supports them, plus standard axes: price,
   return_policy, shipping_speed, seller_trust. Each tradeoff is one
   sentence that names the winner.

3. WARNINGS — honest concerns the user should hear before clicking buy.
   Examples: "Listing 2 has no returns — risky for a used phone",
   "Listing 4 is the 128GB variant; you wanted 256GB", "All US listings
   are $50+ more than overseas — your deal-breaker is genuine cost."

4. RATIONALE for the top pick — 2–3 short sentences. Cite the user's
   ACTUAL categories (especially must_have and deal_breaker types), not
   generic shopping wisdom. Conclude with what they're trading off by
   picking this one.

Plus ALTERNATIVES: 1–3 adjacent shopping ideas (cheaper variant, refurb
path, "wait for a sale", different storage tier) the user might also
consider. Each is one sentence.

Budget: keep each pick.one_liner ≤ 18 words and pick.detail ≤ 35 words
(skip detail if you don't have something useful to say). Each tradeoff
summary ≤ 25 words. Each warning ≤ 30 words. Rationale ≤ 3 sentences.
This budget exists so the structured JSON fits in the output window —
exceeding it gets the response truncated and dropped.

Rules:
  * Only recommend candidates from the input list — never invent listings.
  * If a candidate has scam_score ≥ 40 or its variant doesn't match the
    spec (e.g. 128GB when user wants 256GB), say so explicitly in
    warnings AND in its pick one_liner. Don't quietly downrank.
  * Be SPECIFIC. "Good seller" is useless; "Verified seller with 4,127
    feedback at 99.6%" is useful. Use the actual numbers from findings.
  * Reference the user's spec categories by their actual values and
    importance weights. Quote their budget. Verify each must_have.
  * Keep total output under ~600 words. Brevity is part of holistic."""


_JSON_INSTRUCTION = """Return ONE raw JSON object — no prose, no fences:

{
  "top_pick_candidate_id": string,
  "rationale": string,
  "picks": [
    {"candidate_id": string, "score": integer (0-100),
     "one_liner": string, "detail": string|null}
  ],
  "tradeoffs": [
    {"axis": string, "winner_candidate_id": string|null, "summary": string}
  ],
  "warnings": [string],
  "alternatives": [
    {"title": string, "why_consider": string}
  ]
}

top_pick_candidate_id MUST be one of the candidate_id values from the input.
picks should include every candidate worth showing, sorted by descending
score. winner_candidate_id in tradeoffs MUST also be from the input or null.
warnings and alternatives can be empty if nothing useful applies."""


def _strip_code_fence(text: str) -> str:
    t = text.strip()
    if not t.startswith("```"):
        return t
    t = t.strip("`")
    if "\n" in t:
        first, rest = t.split("\n", 1)
        if first.strip().isalpha():
            t = rest
    return t.removesuffix("```").strip()


def _coerce_json_object(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    try:
        data = json.loads(text)
    except Exception:
        data = None
    if isinstance(data, dict):
        return data
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]

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


def _build_findings_payload(
    candidates: list[dict[str, Any]], findings: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Inline candidate metadata onto each finding so the model sees the URL
    and source domain alongside the structured facts."""
    by_id = {c["id"]: c for c in candidates}
    out: list[dict[str, Any]] = []
    for f in findings:
        if f.get("status") != "done":
            continue
        c = by_id.get(f.get("candidate_id"), {})
        payload = {
            "candidate_id": f.get("candidate_id"),
            "title": c.get("title"),
            "source": c.get("source"),
            "source_url": c.get("source_url"),
        }
        payload.update(f.get("finding") or {})
        out.append(payload)
    return out


async def run_synthesizer(
    client: InsforgeClient,
    intent_id: str,
    spec: dict[str, Any],
    candidates: list[dict[str, Any]],
    findings: list[dict[str, Any]],
) -> SynthOutput:
    payload = _build_findings_payload(candidates, findings)
    if not payload:
        log.warning("synthesizer: no completed findings for intent %s", intent_id)
        return SynthOutput()

    gem = get_client()
    user_msg = (
        "SPEC:\n"
        + json.dumps(spec, indent=2)
        + "\n\nFINDINGS:\n"
        + json.dumps(payload, indent=2)
        + "\n\n"
        + _JSON_INSTRUCTION
    )
    try:
        resp = await gem.aio.models.generate_content(
            model=settings.gemini_model_synthesizer,
            contents=user_msg,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                # 8192 because the schema can easily exceed 4096:
                # picks × N + tradeoffs × ~4 + warnings + alternatives + rationale.
                # The previous 4096 truncated JSON mid-string and silently 404'd
                # the recommendation row.
                max_output_tokens=8192,
            ),
        )
    except Exception as e:
        log.warning("synthesizer call failed: %s", e)
        return SynthOutput()
    # Surface stop_reason so silent truncation is visible in the next debug pass.
    cands = getattr(resp, "candidates", None) or []
    if cands:
        reason = getattr(cands[0], "finish_reason", None)
        if reason and str(reason) not in ("FinishReason.STOP", "1", "STOP"):
            log.warning("synthesizer: unusual finish_reason=%s", reason)

    text = _strip_code_fence(resp.text or "")
    data = _coerce_json_object(text)
    if data is None:
        log.warning("synthesizer: could not parse JSON; text=%r", text[:300])
        return SynthOutput()
    try:
        result = SynthOutput(**data)
    except Exception as e:
        log.warning("synthesizer: schema validation failed: %s; payload=%s", e, list(data.keys()))
        return SynthOutput()

    # Build ranked_candidate_ids in pick order so the dashboard renders them
    # consistently with the synth's intended ranking.
    ranked_ids = [p.candidate_id for p in result.picks] or (
        [result.top_pick_candidate_id] if result.top_pick_candidate_id else []
    )
    await client.insert(
        "recommendations",
        {
            "intent_id": intent_id,
            "ranked_candidate_ids": ranked_ids,
            "rationale": result.rationale,
            "alternatives": [a.model_dump() for a in result.alternatives],
            "picks": [p.model_dump() for p in result.picks],
            "tradeoffs": [t.model_dump() for t in result.tradeoffs],
            "warnings": result.warnings,
        },
    )
    log.info(
        "synthesizer: wrote rec top=%s picks=%d tradeoffs=%d warnings=%d alt=%d",
        result.top_pick_candidate_id,
        len(result.picks),
        len(result.tradeoffs),
        len(result.warnings),
        len(result.alternatives),
    )
    return result
