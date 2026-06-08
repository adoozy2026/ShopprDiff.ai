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
from pydantic import BaseModel, Field

from app.config import settings
from app.genai_client import get_client

log = logging.getLogger(__name__)

MAX_ROUNDS = 2  # at most one "ask" before we force a ready

SYSTEM_PROMPT = """\
You are an intake agent for a personal shopping service.

Task
Extract the user's priorities from their request and produce a structured, \
weighted shopping spec for downstream ranking and filtering. Do not assume a \
fixed list of categories. Infer categories from the user's language and create \
weights that reflect relative importance.

Behavior rules
1. Identify the product or product class as a short noun phrase and place it \
in **product_class**.
2. Extract explicit requirements and implicit priorities. Turn each into a \
**category** named using the user's words when possible.
3. For each category produce:
   - **value**: the user's stated preference or constraint as text.
   - **importance**: a numeric weight from 0.0 to 1.0 representing how \
important this category is relative to others.
   - **type**: one of "must_have", "preference", "deal_breaker", "neutral".
4. Convert qualitative cues into weights using the conversion rules below. \
If the user uses absolute language such as "must", "required", "cannot", set \
**type** to "must_have" or "deal_breaker" and importance to 1.0.
5. If the user gives tradeoffs, reflect them by assigning relative weights \
across affected categories.
6. Ask AT MOST one clarifying question only if a missing item would prevent \
reasonable ranking. If you ask a question, return action="ask" and include the \
question. If you already asked one or have enough info, return action="ready".
7. Be concise. Use null for unknowns.

Output JSON schema
Return a single JSON object exactly matching this structure.

{
  "product_class": "string or null",
  "categories": {
    "<category_name>": {
      "value": "string",
      "importance": 0.0-1.0,
      "type": "must_have|preference|deal_breaker|neutral"
    }
  },
  "missing_info": ["list of missing high-impact items"],
  "action": "ask|ready",
  "question": "string if action is ask else null"
}"""


# ---------------------------------------------------------------------------
# Pydantic models — used as Gemini structured-output schema
# ---------------------------------------------------------------------------


class CategoryEntry(BaseModel):
    value: str
    importance: float = Field(ge=0.0, le=1.0)
    type: Literal["must_have", "preference", "deal_breaker", "neutral"]


class IntakeResponse(BaseModel):
    product_class: str | None = None
    categories: dict[str, CategoryEntry] = {}
    missing_info: list[str] = []
    action: Literal["ask", "ready"]
    question: str | None = None


# ---------------------------------------------------------------------------
# Result dataclass returned to the dispatcher
# ---------------------------------------------------------------------------


@dataclass
class IntakeResult:
    action: Literal["ask", "ready"]
    question: str | None = None
    spec: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
        out.append(
            types.Content(role="user", parts=[types.Part(text="(no initial query provided)")])
        )
    return out


def _build_spec(parsed: IntakeResponse, raw_query: str) -> dict[str, Any]:
    """Convert the structured IntakeResponse into the spec dict persisted on
    the intent row and consumed by downstream agents."""
    return {
        "product_class": parsed.product_class,
        "categories": {name: entry.model_dump() for name, entry in parsed.categories.items()},
        "missing_info": parsed.missing_info,
        "raw_query": raw_query,
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


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

    spec = _build_spec(parsed, raw_query)
    log.debug("intake: ready spec=%s", json.dumps(spec)[:400])
    return IntakeResult(action="ready", spec=spec)
