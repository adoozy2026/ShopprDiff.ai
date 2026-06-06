"""Intake agent — gates research behind a single clarifying question.

The conversation lives entirely in ``intents.clarifying_turns``:
- ``[{"role":"user","text":raw_query}]`` is the initial state inserted by the UI.
- We call Claude with a forced ``submit_response`` tool whose schema is either
  ``{"action":"ask","question":...}`` or ``{"action":"ready","spec":{...}}``.
- If ``ask``: append ``{"role":"assistant","text":question}``; status stays
  ``eliciting``; the dispatcher clears ``picked_up_at`` so the next user reply
  re-triggers us.
- If ``ready``: write the spec onto the intent and flip status to ``ready`` so
  the planner band picks it up on the next poll.

Hard cap: two rounds total. On the second call we instruct Claude to commit
to a spec even with partial info — better a directed search than a stalled UX.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Literal

from app.anthropic_client import get_client
from app.config import settings

log = logging.getLogger(__name__)

MAX_ROUNDS = 2  # at most one "ask" before we force a ready

SYSTEM_PROMPT = """You are an intake agent for a personal shopping service.

Your job is to extract a structured shopping spec from the user's request. The
spec is what downstream agents will use to search and evaluate products.

You may ask AT MOST one clarifying question to fill in the most important
missing field. If you have enough information OR you have already asked one
question, call submit_response with action="ready" and return the spec.

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

RESPONSE_TOOL: dict[str, Any] = {
    "name": "submit_response",
    "description": (
        "Submit either a clarifying question for the user OR a finalized shopping spec. "
        "Choose action='ask' only when you genuinely cannot proceed without one more answer."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["ask", "ready"]},
            "question": {
                "type": "string",
                "description": "Required when action='ask'. One concise question.",
            },
            "spec": {
                "type": "object",
                "description": "Required when action='ready'. The structured spec.",
                "properties": {
                    "product_class": {"type": ["string", "null"]},
                    "budget_cents": {"type": ["integer", "null"]},
                    "condition": {"type": ["string", "null"]},
                    "must_haves": {"type": "array", "items": {"type": "string"}},
                    "deal_breakers": {"type": "array", "items": {"type": "string"}},
                    "retailer_preferences": {"type": "array", "items": {"type": "string"}},
                    "shipping_speed": {"type": ["string", "null"]},
                    "notes": {"type": ["string", "null"]},
                },
            },
        },
        "required": ["action"],
    },
}


@dataclass
class IntakeResult:
    action: Literal["ask", "ready"]
    question: str | None = None
    spec: dict[str, Any] | None = None


def _count_assistant_turns(turns: list[dict[str, Any]]) -> int:
    return sum(1 for t in turns if t.get("role") == "assistant")


def _build_transcript(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert the stored ``clarifying_turns`` array into Anthropic messages.

    Stored turn shape: ``{"role": "user"|"assistant", "text": "..."}``.
    """
    msgs: list[dict[str, Any]] = []
    for t in turns:
        role = t.get("role")
        text = t.get("text", "")
        if role in ("user", "assistant") and text:
            msgs.append({"role": role, "content": text})
    if not msgs:
        # Defensive: shouldn't happen since UI inserts an initial user turn,
        # but if it does, give the model SOMETHING to work with.
        msgs.append({"role": "user", "content": "(no initial query provided)"})
    return msgs


async def run_intake(
    raw_query: str,
    clarifying_turns: list[dict[str, Any]],
) -> IntakeResult:
    """Run one intake turn. Returns ask or ready.

    ``raw_query`` is informational (already first in clarifying_turns). The
    caller is responsible for persisting the result.
    """
    rounds_used = _count_assistant_turns(clarifying_turns)
    must_finalize = rounds_used >= MAX_ROUNDS - 1  # i.e. on or past the cap

    transcript = _build_transcript(clarifying_turns)

    system = SYSTEM_PROMPT
    if must_finalize:
        system += (
            "\n\nIMPORTANT: You have already asked your one clarifying question, "
            "OR this is the cap. You MUST call submit_response with action='ready' "
            "and your best-effort spec from what you have. Do not ask again."
        )

    client = get_client()
    log.info(
        "intake: rounds_used=%d must_finalize=%s transcript_len=%d",
        rounds_used,
        must_finalize,
        len(transcript),
    )
    resp = await client.messages.create(
        model=settings.anthropic_model_researcher,
        max_tokens=1024,
        system=system,
        messages=transcript,
        tools=[RESPONSE_TOOL],
        tool_choice={"type": "tool", "name": "submit_response"},
    )

    tool_block = next((b for b in resp.content if getattr(b, "type", None) == "tool_use"), None)
    if tool_block is None:
        log.error("intake: no tool_use block in response: %s", resp.content)
        return IntakeResult(action="ready", spec={"raw_query": raw_query})

    payload: dict[str, Any] = tool_block.input  # type: ignore[assignment]
    action = payload.get("action")

    if action == "ask" and not must_finalize:
        q = (payload.get("question") or "").strip()
        if q:
            return IntakeResult(action="ask", question=q)
        # If model asked but produced no question, treat as ready with what we have.

    spec = payload.get("spec") or {}
    if not isinstance(spec, dict):
        spec = {}
    # Carry forward the raw query for downstream agents to reference.
    spec.setdefault("raw_query", raw_query)
    log.debug("intake: ready spec=%s", json.dumps(spec)[:400])
    return IntakeResult(action="ready", spec=spec)
