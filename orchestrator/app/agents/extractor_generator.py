"""Self-improving extractor pool — Replicas glue.

When the configurator (browser-agent loop) successfully extracts a price for
a candidate, we log the run to ``extractor_runs``. Once we've seen
``REPLICAS_MIN_RUNS`` successful runs on a domain that we don't already have
a deterministic extractor for, this module assembles a bundle of those runs
and spawns a Replicas workspace (Claude under the hood) tasked with writing
``app/tools/extractors/<domain>.py``, the matching prompt-hints file, and
golden-file tests.

We track each spawn in ``extractor_jobs`` so that:
  * a second successful candidate on the same domain doesn't fire a duplicate
    job while the first is still in flight (the table has a partial unique
    index enforcing this),
  * the dashboard can later show "Replicas is generating an extractor for
    apple.com" via realtime.

Phase 1 is feature-flagged off by default. When the flag is on we *spawn*
the workspace but don't yet poll for completion — Replicas will commit to a
branch and open a PR on its own, and CI handles the rest.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from app.config import settings
from app.db.client import InsforgeClient

log = logging.getLogger(__name__)


# Path inside the orchestrator package where Phase 2's runtime registry will
# look for committed extractors. We use it now (read-only) to skip
# regeneration when a deterministic extractor is already in the repo.
_EXTRACTORS_DIR = Path(__file__).resolve().parent.parent / "tools" / "extractors"


def _domain_to_module(domain: str) -> str:
    """Convert ``apple.com`` → ``apple_com`` for a Python module name."""
    return domain.replace(".", "_").replace("-", "_")


def _extractor_exists(domain: str) -> bool:
    mod = _domain_to_module(domain)
    return (_EXTRACTORS_DIR / f"{mod}.py").exists()


# ---- Public entry point -------------------------------------------------


async def maybe_trigger(client: InsforgeClient, domains: set[str]) -> None:
    """Inspect each domain's ``extractor_runs`` history and fire a Replicas
    generation job if criteria are met.

    Called from the orchestrator after the researcher band completes. Non-
    fatal: any failure inside is logged and swallowed so the user-facing
    pipeline keeps working.
    """
    if not settings.replicas_enabled:
        if domains:
            log.debug("REPLICAS_ENABLED=false; skipping %d domain(s)", len(domains))
        return

    cli = settings.replicas_cli_path or shutil.which("replicas")
    if not cli or not os.path.exists(cli):
        log.warning(
            "REPLICAS_ENABLED is true but cli not found at %r; not firing", cli
        )
        return

    for domain in domains:
        try:
            await _maybe_trigger_one(client, cli, domain)
        except Exception as e:
            log.warning("extractor_generator: %s failed: %s", domain, e)


# ---- Per-domain decision ------------------------------------------------


async def _maybe_trigger_one(client: InsforgeClient, cli: str, domain: str) -> None:
    if _extractor_exists(domain):
        log.debug("extractor for %s already in repo; skipping", domain)
        return

    # In-flight check (partial unique index in SQL is the strong guarantee;
    # this early-out avoids the row-collision retry).
    inflight = await client.select(
        "extractor_jobs",
        {"domain": f"eq.{domain}", "status": "in.(queued,running)", "limit": "1"},
    )
    if inflight:
        log.debug("extractor_jobs already in flight for %s", domain)
        return

    runs = await client.select(
        "extractor_runs",
        {
            "domain": f"eq.{domain}",
            "succeeded": "eq.true",
            "order": "created_at.desc",
            "limit": "10",
        },
    )
    if len(runs) < settings.replicas_min_runs:
        return

    bundle = _build_bundle(domain, runs)
    name = f"extractor-{_domain_to_module(domain)}-{uuid.uuid4().hex[:6]}"
    reason = (
        f"initial generation — {len(runs)} successful configurator runs on {domain}"
    )

    # Insert the queued row FIRST so the partial unique index blocks any
    # concurrent caller before we burn a replica.
    inserted = await client.insert(
        "extractor_jobs",
        {
            "domain": domain,
            "status": "queued",
            "reason": reason,
        },
    )
    job_id = inserted[0]["id"]
    log.info("extractor_jobs %s queued for %s", job_id, domain)

    replica_id, log_excerpt, ok = await _spawn_replica(cli, name, bundle)
    await client.update(
        "extractor_jobs",
        where={"id": f"eq.{job_id}"},
        patch={
            "replica_id": replica_id,
            "status": "running" if ok else "failed",
            "log_excerpt": log_excerpt[:2000] if log_excerpt else None,
        },
    )


# ---- Bundle building ----------------------------------------------------


_INTERFACE_DOC = """\
## Required interface

```python
# app/tools/extractors/<domain_module>.py
from playwright.async_api import Page

async def extract(page: Page, spec: dict) -> dict:
    \"\"\"Return a dict matching this subset of ListingFacts:
        title, price_cents, condition, seller, shipping_cost_cents,
        shipping_speed, return_policy, image_url, description_summary,
        canonical_attrs.
    Use None for fields the page doesn't state. price_cents and
    shipping_cost_cents are integers in US cents. Always return a dict;
    never raise.\"\"\"
```

Also write `app/tools/extractors/<domain>.md` — short markdown with
domain-specific prompt hints (selectors that worked, dropdown labels to
match against spec.must_haves, captcha quirks, etc.) that the browser
agent fallback will inject into its system prompt.

Add golden-file tests under `orchestrator/tests/fixtures/extractors/<domain>/`
with `input.html`, `spec.json`, `expected.json`. CI will run them.
"""


def _build_bundle(domain: str, runs: list[dict[str, Any]]) -> str:
    samples = []
    for i, r in enumerate(runs[: settings.replicas_min_runs]):
        samples.append(
            f"### Sample {i + 1}\n"
            f"- URL: {r.get('source_url')}\n"
            f"- Spec: ```json\n{json.dumps(r.get('spec') or {}, indent=2)}\n```\n"
            f"- Action history the browser agent took:\n"
            f"```json\n{json.dumps(r.get('action_history') or [], indent=2)}\n```\n"
            f"- Facts the browser agent eventually extracted:\n"
            f"```json\n{json.dumps(r.get('extracted_facts') or {}, indent=2)}\n```\n"
        )
    return (
        f"# Task — write a deterministic extractor for `{domain}`\n\n"
        "I'm a multi-agent shopping app. Today every product on this retailer "
        "goes through a Gemini-driven browser loop because the static page "
        "(used by `url_context`) doesn't surface configured prices. That loop "
        "is slow (15-45s) and not free, so we'd like a deterministic Playwright "
        "extractor for repeat hits.\n\n"
        f"Here are {min(len(runs), settings.replicas_min_runs)} real successful "
        "browser-agent runs on this domain — same shape your generated "
        "extractor should produce.\n\n"
        + "\n".join(samples)
        + "\n\n"
        + _INTERFACE_DOC
        + "\n\n## What to do\n"
        "1. Read the samples above to identify the stable selectors / flow.\n"
        "2. Write the Python extractor + prompt-hints markdown + golden tests.\n"
        "3. Commit on a branch and open a PR. CI will run the golden tests; "
        "merge when green.\n"
    )


# ---- Subprocess: replicas create ---------------------------------------


async def _spawn_replica(cli: str, name: str, bundle: str) -> tuple[str | None, str, bool]:
    """Returns (replica_id, log_excerpt, ok)."""
    args = [
        cli,
        "create",
        name,
        "--agent",
        "claude",
        "--message",
        bundle,
    ]
    # Without --environment, the CLI emits an interactive "Select environment"
    # picker and our piped subprocess hangs/no-ops because we don't have a TTY.
    # The user finds the ID via `replicas env list` (look for
    # "Default <org>/<repo>").
    if settings.replicas_environment_id:
        args.extend(["--environment", settings.replicas_environment_id])

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            # Explicit /dev/null stdin so the CLI doesn't think it's
            # interactive even when other args are missing.
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError as e:
        return None, f"cli not executable: {e}", False
    try:
        out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return None, "replicas create timed out after 120s", False

    out = (out_b or b"").decode("utf-8", "replace")
    rc = proc.returncode or 0

    # The CLI prints something like:
    #   Replica created: <name>
    #     ID: <uuid>
    replica_id = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("ID:") or line.startswith("ID :"):
            replica_id = line.split(":", 1)[1].strip()
            break

    # If we returned 0 but never parsed an ID, the CLI silently no-op'd —
    # most often because it sat on an interactive prompt that timed out
    # without doing anything. Treat that as failure so the job row gets
    # marked failed and isn't blocking retries.
    if rc != 0 or replica_id is None:
        log.warning(
            "replicas create no-op rc=%d id=%s output=%r",
            rc,
            replica_id,
            out[:400],
        )
        return replica_id, out, False

    log.info("replicas create ok name=%s id=%s", name, replica_id)
    return replica_id, out, True
