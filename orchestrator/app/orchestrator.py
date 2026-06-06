"""Pipeline dispatcher — runs an intent through intake → planner → research.

Called from the poller per claimed intent. Drives the state machine:

    eliciting  --intake.ask-->     eliciting (with new assistant turn,
                                              picked_up_at cleared so the user
                                              can answer)
    eliciting  --intake.ready--> ready (chained inline)
    ready      --planner-->      researching (candidates inserted)
    researching --researchers->  done (N parallel researchers populated findings)

Anything raised inside this function is logged + we flip the intent to 'error'
so the user sees the failure rather than a stuck spinner.
"""

from __future__ import annotations

import logging
from typing import Any

from app.agents.intake import run_intake
from app.agents.planner import run_planner
from app.agents.researcher import run_all_researchers
from app.agents.synthesizer import run_synthesizer
from app.db.client import InsforgeClient

log = logging.getLogger(__name__)


async def _set_error(client: InsforgeClient, intent_id: str, msg: str) -> None:
    try:
        await client.update(
            "intents",
            where={"id": f"eq.{intent_id}"},
            patch={"status": "error", "spec": {"error": msg[:500]}},
        )
    except Exception as e:
        log.error("failed to mark intent %s as error: %s", intent_id, e)


async def handle_intent(intent: dict[str, Any]) -> None:
    """Drive a claimed intent through as many pipeline stages as possible.

    Stages chain inline (no need to wait for another poll tick) so a fresh
    query with a complete initial spec can reach 'researching' in one
    invocation. If intake asks a question, we hand back to the user and exit.
    """
    intent_id = intent["id"]
    client = InsforgeClient()
    try:
        try:
            await _stage_eliciting(client, intent)
            await _stage_ready(client, intent_id)
            await _stage_researching(client, intent_id)
        except Exception as e:
            log.exception("handle_intent failed: intent_id=%s", intent_id)
            await _set_error(client, intent_id, repr(e))
    finally:
        await client.close()


async def _stage_eliciting(client: InsforgeClient, intent: dict[str, Any]) -> None:
    """If still eliciting, run intake. Mutates ``intent`` dict for downstream stages."""
    if intent.get("status") != "eliciting":
        return

    intent_id = intent["id"]
    raw_query = intent.get("raw_query", "")
    turns = list(intent.get("clarifying_turns") or [])

    result = await run_intake(raw_query=raw_query, clarifying_turns=turns)

    if result.action == "ask" and result.question:
        turns.append({"role": "assistant", "text": result.question})
        await client.update(
            "intents",
            where={"id": f"eq.{intent_id}"},
            patch={
                "clarifying_turns": turns,
                "picked_up_at": None,  # hand back to user
            },
        )
        log.info("intake asked: intent_id=%s", intent_id)
        # Mutate so the next stage sees we're not ready.
        intent["status"] = "eliciting"
        return

    # Ready — write spec, flip status, and let the next stage continue.
    spec = result.spec or {}
    await client.update(
        "intents",
        where={"id": f"eq.{intent_id}"},
        patch={"spec": spec, "status": "ready"},
    )
    log.info("intake finalized spec: intent_id=%s", intent_id)
    intent["status"] = "ready"
    intent["spec"] = spec


async def _stage_ready(client: InsforgeClient, intent_id: str) -> None:
    """If status=ready, run the planner and insert candidates."""
    # Re-read so we have the saved spec even if intake just wrote it in the
    # same invocation (caller's dict already has it, but read is cheap and
    # keeps this stage idempotent if called directly).
    rows = await client.select("intents", {"id": f"eq.{intent_id}"})
    if not rows:
        return
    intent = rows[0]
    if intent.get("status") != "ready":
        return

    spec = intent.get("spec") or {}
    drafts = await run_planner(intent_id=intent_id, spec=spec)

    if not drafts:
        log.warning("planner returned 0 candidates for intent %s", intent_id)
        await client.update(
            "intents",
            where={"id": f"eq.{intent_id}"},
            patch={"status": "researching"},
        )
        return

    rows_to_insert = [
        {
            "intent_id": intent_id,
            "title": d.title,
            "source": d.source,
            "source_url": d.source_url,
            "status": "queued",
        }
        for d in drafts
    ]
    await client.insert("candidates", rows_to_insert)
    log.info("planner inserted %d candidates: intent_id=%s", len(rows_to_insert), intent_id)

    await client.update(
        "intents",
        where={"id": f"eq.{intent_id}"},
        patch={"status": "researching"},
    )


async def _stage_researching(client: InsforgeClient, intent_id: str) -> None:
    """If status=researching, fan out per-candidate researchers and wait."""
    rows = await client.select("intents", {"id": f"eq.{intent_id}"})
    if not rows or rows[0].get("status") != "researching":
        return
    intent = rows[0]
    spec = intent.get("spec") or {}

    candidates = await client.select(
        "candidates",
        {
            "intent_id": f"eq.{intent_id}",
            "status": "in.(queued,researching)",
            "order": "created_at.asc",
        },
    )
    if not candidates:
        log.info("researching: no candidates to dispatch for %s", intent_id)
        await client.update(
            "intents",
            where={"id": f"eq.{intent_id}"},
            patch={"status": "done"},
        )
        return

    await run_all_researchers(client, candidates, spec)
    log.info("researching complete for intent %s (%d candidates)", intent_id, len(candidates))

    # Synthesize a recommendation from the completed findings before flipping
    # the intent to 'done' so the dashboard receives the rec in the same flow.
    await _stage_synthesizing(client, intent_id, spec)

    await client.update(
        "intents",
        where={"id": f"eq.{intent_id}"},
        patch={"status": "done"},
    )


async def _stage_synthesizing(
    client: InsforgeClient, intent_id: str, spec: dict[str, Any]
) -> None:
    """Run the synthesizer over completed researcher_findings and persist a
    row to ``recommendations`` (the DB trigger publishes the realtime event).
    """
    candidates = await client.select(
        "candidates",
        {"intent_id": f"eq.{intent_id}", "order": "created_at.asc"},
    )
    findings = await client.select(
        "researcher_findings",
        {"intent_id": f"eq.{intent_id}", "status": "eq.done"},
    )
    if not findings:
        log.info("synthesizing: no completed findings for %s", intent_id)
        return
    await run_synthesizer(client, intent_id, spec, candidates, findings)
