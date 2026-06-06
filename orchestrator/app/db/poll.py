import asyncio
import logging
from datetime import datetime, timezone

from app.config import settings
from app.db.client import InsforgeClient, InsforgeError
from app.orchestrator import handle_intent

log = logging.getLogger(__name__)


async def intent_poller_task() -> None:
    """Polls Insforge for unclaimed actionable intents and dispatches them.

    Insforge does not expose direct Postgres access, so we cannot LISTEN/NOTIFY.
    We mark each intent with ``picked_up_at`` to claim it; the WHERE clause
    filters those out so concurrent pollers (if any) don't double-process.

    Actionable statuses:
      * ``eliciting`` — intake hasn't finalized the spec yet
      * ``ready``    — spec is set; planner should run
    """
    try:
        client = InsforgeClient()
    except InsforgeError as e:
        log.warning("poller disabled: %s", e)
        return

    backoff = 1.0
    while True:
        try:
            rows = await client.select(
                "intents",
                {
                    "status": "in.(eliciting,ready)",
                    "picked_up_at": "is.null",
                    "order": "created_at.asc",
                    "limit": "5",
                },
            )
            for row in rows:
                claimed = await client.update(
                    "intents",
                    where={"id": f"eq.{row['id']}", "picked_up_at": "is.null"},
                    patch={"picked_up_at": datetime.now(timezone.utc).isoformat()},
                )
                if not claimed:
                    continue  # another worker beat us; skip
                asyncio.create_task(handle_intent(claimed[0]))
            backoff = 1.0
        except asyncio.CancelledError:
            await client.close()
            raise
        except Exception as e:
            log.error("poll error: %s; retry in %.1fs", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
            continue

        await asyncio.sleep(settings.poll_interval_seconds)
