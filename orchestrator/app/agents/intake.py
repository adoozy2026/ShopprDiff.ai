"""Intake agent — gates research behind a single clarifying question.

The conversation lives entirely in ``intents.clarifying_turns``:
- ``[{"role":"user","text":raw_query}]`` is the initial state inserted by the UI.
- We call Gemini with ``response_mime_type='application/json'`` and a Pydantic
  ``IntakeResponse`` schema — the model is forced to return either ``ask`` or
  ``ready`` in a single structured object.
- If ``ask``: append ``{"role":"assistant","text":question}``; status stays
  ``eliciting``; the dispatcher clears ``picked_up_at`` so the next user reply
  re-triggers us.
- If ``ready``: write the spec onto the intent and flip status to ``ready`` so
  the planner band picks it up on the next poll.

Hard cap: two rounds total. On the second call we instruct Gemini to commit
to a spec even with partial info — better a directed search than a stalled UX.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Literal

from google.genai import types
from pydantic import BaseModel

from app.config import settings
from app.genai_client import get_client

log = logging.getLogger(__name__)

MAX_ROUNDS = 2  # at most one "ask" before we force a ready

SYSTEM_PROMPT = """You are an intake agent for a personal shopping service.

Your job is to extract a structured shopping spec from the user's request. The
spec is what downstream agents will use to search and evaluate products.

You may ask AT MOST one clarifying question to fill in the most important
missing field. If you have enough information OR you have already asked one
question, return action="ready" with the spec.

A good spec includes (use null for unknowns):
- product_class: short noun phrase (e.g. "used iPhone 15 Pro", "noise-cancelling headphones")
- budget_cents: maximum acceptable price, in US cents
- condition: one of "new" | "renewed" | "used_like_new" | "used_good" | "used_acceptable" | "any"
- must_haves: list of feature/spec strings the user explicitly required
- deal_breakers: list of constraints that disqualify a listing
- retailer_preferences: list of retailer names or [] if no preference
- shipping_speed: "fast" | "standard" | "any"
- notes: any other context worth remembering

Be concise. Never ask multiple questions at once."""


class IntakeSpec(BaseModel):
    product_class: str | None = None
    budget_cents: int | None = None
    condition: str | None = None
    must_haves: list[str] = []
    deal_breakers: list[str] = []
    retailer_preferences: list[str] = []
    shipping_speed: str | None = None
    notes: str | None = None


class IntakeResponse(BaseModel):
    action: Literal["ask", "ready"]
    question: str | None = None
    spec: IntakeSpec | None = None


@dataclass
class IntakeResult:
    action: Literal["ask", "ready"]
    question: str | None = None
    spec: dict[str, Any] | None = None


def _count_assistant_turns(turns: list[dict[str, Any]]) -> int:
    return sum(1 for t in turns if t.get("role") == "assistant")


def _build_contents(turns: list[dict[str, Any]]) -> list[types.Content]:
    """Convert stored clarifying_turns into Gemini's Content list.

    Stored turn shape: ``{"role": "user"|"assistant", "text": "..."}``.
    Gemini uses ``role='model'`` instead of ``'assistant'``.
    """
    out: list[types.Content] = []
    for t in turns:
        role_in = t.get("role")
        text = t.get("text", "")
        if not text:
            continue
        if role_in == "user":
            role = "user"
        elif role_in == "assistant":
            role = "model"
        else:
            continue
        out.append(types.Content(role=role, parts=[types.Part(text=text)]))
    if not out:
        # Defensive: shouldn't happen since UI inserts an initial user turn.
        out.append(types.Content(role="user", parts=[types.Part(text="(no initial query provided)")]))
    return out


async def run_intake(
    raw_query: str,
    clarifying_turns: list[dict[str, Any]],
) -> IntakeResult:
    """Run one intake turn. Returns ask or ready.

    ``raw_query`` is informational (already first in clarifying_turns). The
    caller persists the result.
    """
    rounds_used = _count_assistant_turns(clarifying_turns)
    must_finalize = rounds_used >= MAX_ROUNDS - 1

    contents = _build_contents(clarifying_turns)

    system = SYSTEM_PROMPT
    if must_finalize:
        system += (
            "\n\nIMPORTANT: You have already asked your one clarifying question, "
            "OR this is the cap. You MUST return action='ready' with your "
            "best-effort spec from what you have. Do not ask again."
        )

    client = get_client()
    log.info(
        "intake: rounds_used=%d must_finalize=%s contents_len=%d",
        rounds_used,
        must_finalize,
        len(contents),
    )

    resp = await client.aio.models.generate_content(
        model=settings.gemini_model_researcher,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=IntakeResponse,
            max_output_tokens=1024,
        ),
    )

    parsed: IntakeResponse | None = getattr(resp, "parsed", None)
    if parsed is None:
        # Fall back to parsing the text payload manually.
        try:
            data = json.loads(resp.text or "{}")
            parsed = IntakeResponse(**data)
        except Exception as e:
            log.error("intake: could not parse response: %s; raw=%s", e, resp.text)
            return IntakeResult(action="ready", spec={"raw_query": raw_query})

    if parsed.action == "ask" and not must_finalize and parsed.question:
        return IntakeResult(action="ask", question=parsed.question.strip())

    spec = (parsed.spec.model_dump(exclude_none=False) if parsed.spec else {})
    spec.setdefault("raw_query", raw_query)
    log.debug("intake: ready spec=%s", json.dumps(spec)[:400])
    return IntakeResult(action="ready", spec=spec)
