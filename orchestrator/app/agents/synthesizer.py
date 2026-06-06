"""Synthesizer — turns N completed researcher findings into a ranked pick.

One Gemini call (gemini-2.5-pro by default) reads the user's spec plus every
finished researcher_findings row, then produces a recommendation row with:

  * ``ranked_candidate_ids``  ordered best → worst, only including the ones
    worth showing.
  * ``rationale``             short markdown explaining *why* the top pick
    wins for THIS user — referencing their spec.
  * ``alternatives``          adjacent buys the user might also consider
    (cheaper variant, refurbished path, "if you can wait" angle, etc.).

JSON-as-text output is parsed defensively — same reason as the researcher
extraction step (response_mime_type='application/json' can't combine with
tools, and pro models occasionally wrap output in code fences).
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


class RankedPick(BaseModel):
    candidate_id: str
    score: int  # 0-100
    reason: str


class SynthOutput(BaseModel):
    top_pick_candidate_id: str | None = None
    rationale: str = ""
    ranked: list[RankedPick] = []
    alternatives: list[Alternative] = []


SYSTEM_PROMPT = """You are the final stage of a personal shopping pipeline.

You receive: (1) the user's structured shopping spec, and (2) a JSON array of
researcher findings — one per candidate listing the prior agents found and
inspected. Each finding has: candidate_id, title, source (retailer domain),
source_url, price_cents (if known), condition, seller, shipping, returns,
known_issues, scam_score, scam_reasons, and seller_rep.

Your job is to recommend the SINGLE best candidate for this user and explain
why in plain English. Then list 1-3 alternative options the user might also
consider — these can be:
  * a cheaper variant if the budget is tight
  * a more reliable seller if the top pick has any scam signal
  * a refurbished path if the user picked "used"
  * a different storage / color tier worth knowing about

Rules:
  * Only recommend from the candidate list — never invent a listing.
  * If a candidate has scam_score ≥ 40, you may still rank it but explain
    the risk in its `reason`. Prefer safer picks for the top spot.
  * Reference the user's actual deal_breakers and must_haves in the rationale.
  * Keep rationale ≤ 3 short sentences.
  * Each alternative.why_consider is one sentence."""


_JSON_INSTRUCTION = """Return ONE raw JSON object — no prose, no fences:

{
  "top_pick_candidate_id": string,
  "rationale": string,
  "ranked": [
    {"candidate_id": string, "score": integer (0-100), "reason": string}
  ],
  "alternatives": [
    {"title": string, "why_consider": string}
  ]
}

top_pick_candidate_id MUST be one of the candidate_id values from the input
findings. ranked should include every candidate worth showing, sorted by
descending score. alternatives can be empty if nothing useful applies."""


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
                max_output_tokens=2048,
            ),
        )
    except Exception as e:
        log.warning("synthesizer call failed: %s", e)
        return SynthOutput()

    text = _strip_code_fence(resp.text or "")
    data = _coerce_json_object(text)
    if data is None:
        log.warning("synthesizer: could not parse JSON; text=%r", text[:200])
        return SynthOutput()
    try:
        result = SynthOutput(**data)
    except Exception as e:
        log.warning("synthesizer: schema validation failed: %s", e)
        return SynthOutput()

    # Persist into recommendations — the trigger publishes
    # recommendation.created to the realtime channel.
    ranked_ids = [r.candidate_id for r in result.ranked] or (
        [result.top_pick_candidate_id] if result.top_pick_candidate_id else []
    )
    await client.insert(
        "recommendations",
        {
            "intent_id": intent_id,
            "ranked_candidate_ids": ranked_ids,
            "rationale": result.rationale,
            "alternatives": [a.model_dump() for a in result.alternatives],
        },
    )
    log.info(
        "synthesizer: wrote recommendation top=%s alt=%d",
        result.top_pick_candidate_id,
        len(result.alternatives),
    )
    return result
