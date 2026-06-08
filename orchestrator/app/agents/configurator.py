"""Browser-agent configurator.

Static fetch (Gemini's ``url_context``) can't see prices that only appear
after you configure a product — Apple's "From $1,999" Mac Studio, eBay
variant dropdowns, Newegg "Add to cart for price". When the static path
returns a null price OR the URL host is on a known-configurable list, we
escalate to this module.

Per candidate, we open a Playwright page, then run a multi-step loop:

  1. Snapshot the visible interactive elements (buttons, links, selects,
     inputs) with their accessible text labels.
  2. Take a screenshot.
  3. Ask Gemini for the next action, given the user's spec + the action
     history + the element snapshot + the screenshot (multimodal).
  4. Execute the action against the page.
  5. Update the researcher_findings.step field so the dashboard shows the
     work happening live.

We cap at ``MAX_STEPS`` actions so a stuck page can't loop forever, and the
final pass extracts a fresh ListingFacts from the configured DOM.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal

from google.genai import types
from pydantic import BaseModel

from app.config import settings
from app.genai_client import get_client
from app.tools.sources import _get_browser, playwright_fetch  # type: ignore

log = logging.getLogger(__name__)


# How many configure-and-read iterations we'll attempt before giving up.
MAX_STEPS = 5

# Per-action timeouts so a stuck page can't block the whole pipeline.
_NAV_TIMEOUT_MS = 20_000
_ACTION_TIMEOUT_MS = 8_000
_WAIT_AFTER_ACTION_MS = 1500

def should_escalate(url: str, price_cents: int | None) -> bool:
    """Decide whether to invoke the browser agent on this candidate."""
    if not url:
        return False
    # If static extraction couldn't find a price, give the browser a shot.
    return price_cents is None


# ---- Action schema ------------------------------------------------------


class ActionDecision(BaseModel):
    action: Literal["click", "select", "type", "scroll", "done", "give_up"]
    target_index: int | None = None  # index into the element snapshot
    value: str | None = None  # text to type, or option label to select
    reason: str  # short, surfaced as the visible step on the tile


@dataclass
class ConfigResult:
    text: str  # final document.body.innerText after configuration
    steps: int  # how many actions we executed
    history: list[ActionDecision]


# ---- DOM snapshotting (runs in the page context) -----------------------


# Pull a compact list of *visible* interactive elements with stable enough
# identifiers (tag, role, accessible name, current value) that the LLM can
# choose by index. Keep the list short — long lists blow the prompt budget.
_SNAPSHOT_JS = r"""
() => {
  const items = [];
  const isVisible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') return false;
    return true;
  };
  const text = (el) => {
    const t = (el.innerText || el.textContent || '').trim().replace(/\s+/g, ' ');
    return t.length > 120 ? t.slice(0, 117) + '...' : t;
  };
  const sel = 'a[href], button, [role="button"], input, select, textarea, [role="radio"], [role="option"], [role="tab"]';
  const els = Array.from(document.querySelectorAll(sel));
  for (const el of els) {
    if (!isVisible(el)) continue;
    if (el.disabled) continue;
    const tag = el.tagName.toLowerCase();
    const role = el.getAttribute('role') || tag;
    const label =
      el.getAttribute('aria-label') ||
      el.getAttribute('title') ||
      el.getAttribute('placeholder') ||
      text(el) ||
      el.getAttribute('name') ||
      '';
    if (!label && !['select', 'input', 'textarea'].includes(tag)) continue;
    let value = null;
    if (tag === 'select') {
      const opts = Array.from(el.options).map((o) => o.label || o.text);
      value = { selected: el.value, options: opts.slice(0, 20) };
    } else if (tag === 'input' || tag === 'textarea') {
      value = { value: el.value || '', type: el.type || 'text' };
    }
    items.push({ tag, role, label: label.slice(0, 120), value });
    if (items.length >= 40) break;
  }
  return items;
};
"""


async def _snapshot(page) -> list[dict[str, Any]]:
    try:
        return await page.evaluate(_SNAPSHOT_JS)
    except Exception as e:
        log.debug("configurator: snapshot failed: %s", e)
        return []


# ---- Gemini decision call ----------------------------------------------


_DECIDE_SYSTEM = """You are a browser agent buying a product on behalf of a
user. You see the page's visible interactive elements and a screenshot. Your
goal is to configure the listing to match the user's spec so the FINAL price
is what they'd actually pay, then return action="done".

Rules:
  * Pick the element by its index in the provided list.
  * Prefer the action that most directly reveals the configured price:
    click the spec-matching option, select the matching size/storage/RAM,
    fill required fields, dismiss interstitials. Avoid clicking "Add to
    cart" or "Buy now" — we only want to read the configured price.
  * If the page already shows the configured price for the user's spec,
    return action="done".
  * If you've tried a few actions and the page won't progress (popups,
    region pickers we can't satisfy, captchas), return action="give_up".
  * Be specific in `reason` — it appears verbatim on the dashboard so the
    user sees what you're doing. ≤ 60 chars."""


_DECIDE_JSON = """Return ONE raw JSON object — no fences:

{
  "action": "click" | "select" | "type" | "scroll" | "done" | "give_up",
  "target_index": integer or null,
  "value": string or null,
  "reason": string
}

target_index is required for click/select/type/scroll; null for done/give_up.
value is required for select (option label or value) and type (text). reason
is always required (≤ 60 chars)."""


def _format_elements(elements: list[dict[str, Any]]) -> str:
    lines = []
    for i, el in enumerate(elements):
        label = el.get("label") or ""
        tag = el.get("tag") or "?"
        bits = [f"[{i}] <{tag}>", label]
        v = el.get("value")
        if isinstance(v, dict):
            if v.get("options") is not None:
                bits.append(
                    f"  selected={v.get('selected')!r} options={v.get('options')!r}"
                )
            else:
                bits.append(
                    f"  type={v.get('type')!r} value={v.get('value')!r}"
                )
        lines.append(" ".join(bits))
    return "\n".join(lines)


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


async def _decide(
    spec: dict[str, Any],
    history: list[ActionDecision],
    elements: list[dict[str, Any]],
    screenshot_png: bytes,
) -> ActionDecision:
    user_msg_text = (
        "USER SPEC:\n"
        + json.dumps(spec, indent=2)
        + "\n\nACTION HISTORY:\n"
        + (
            "\n".join(
                f"  {i}. {h.action} target={h.target_index} value={h.value!r} — {h.reason}"
                for i, h in enumerate(history)
            )
            or "  (none)"
        )
        + "\n\nINTERACTIVE ELEMENTS:\n"
        + _format_elements(elements)
        + "\n\n"
        + _DECIDE_JSON
    )

    contents: list[types.Content] = [
        types.Content(
            role="user",
            parts=[
                types.Part(text=user_msg_text),
                types.Part(
                    inline_data=types.Blob(
                        mime_type="image/png",
                        data=screenshot_png,
                    )
                ),
            ],
        )
    ]

    gem = get_client()
    try:
        resp = await gem.aio.models.generate_content(
            model=settings.gemini_model_researcher,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=_DECIDE_SYSTEM,
                max_output_tokens=512,
            ),
        )
    except Exception as e:
        log.warning("configurator: decide call failed: %s", e)
        return ActionDecision(action="give_up", reason="model call failed")

    data = _coerce_json_object(_strip_code_fence(resp.text or ""))
    if not data:
        return ActionDecision(action="give_up", reason="bad JSON from model")
    try:
        return ActionDecision(**data)
    except Exception as e:
        log.warning("configurator: schema validation failed: %s", e)
        return ActionDecision(action="give_up", reason="bad action schema")


# ---- Action execution ---------------------------------------------------


async def _nth_visible_locator(page, tag_role: str, n: int):
    """Resolve our snapshot's [n]-of-<tag> back to a Playwright locator.

    Our snapshot collected querySelectorAll matches in order, filtered for
    visibility. We re-do the same filtering server-side so the nth match
    here lines up.
    """
    js = f"""
    () => {{
      const sel = 'a[href], button, [role="button"], input, select, textarea, [role="radio"], [role="option"], [role="tab"]';
      const isVisible = (el) => {{
        const r = el.getBoundingClientRect();
        if (r.width < 2 || r.height < 2) return false;
        const cs = getComputedStyle(el);
        if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') return false;
        return true;
      }};
      const els = Array.from(document.querySelectorAll(sel));
      let idx = -1;
      for (const el of els) {{
        if (!isVisible(el)) continue;
        if (el.disabled) continue;
        idx++;
        if (idx === {n}) {{
          el.scrollIntoView({{ block: 'center', behavior: 'instant' }});
          el.setAttribute('data-config-agent-target', 'yes');
          return true;
        }}
      }}
      return false;
    }}
    """
    ok = await page.evaluate(js)
    if not ok:
        return None
    return page.locator("[data-config-agent-target='yes']").first


async def _execute(page, decision: ActionDecision) -> bool:
    n = decision.target_index
    if decision.action in ("click", "select", "type", "scroll") and n is None:
        return False
    try:
        if decision.action == "scroll":
            await page.evaluate(f"window.scrollBy(0, window.innerHeight * 0.7)")
            return True
        loc = await _nth_visible_locator(page, "", n or 0)
        if loc is None:
            return False
        if decision.action == "click":
            await loc.click(timeout=_ACTION_TIMEOUT_MS)
        elif decision.action == "select":
            if decision.value:
                try:
                    await loc.select_option(label=decision.value, timeout=_ACTION_TIMEOUT_MS)
                except Exception:
                    await loc.select_option(value=decision.value, timeout=_ACTION_TIMEOUT_MS)
        elif decision.action == "type":
            await loc.fill(decision.value or "", timeout=_ACTION_TIMEOUT_MS)
        # Clean up our marker attribute so next snapshot/locator round is fresh.
        try:
            await page.evaluate(
                "() => document.querySelectorAll('[data-config-agent-target]')"
                ".forEach((e) => e.removeAttribute('data-config-agent-target'))"
            )
        except Exception:
            pass
        return True
    except Exception as e:
        log.debug("configurator: execute %s failed: %s", decision.action, e)
        try:
            await page.evaluate(
                "() => document.querySelectorAll('[data-config-agent-target]')"
                ".forEach((e) => e.removeAttribute('data-config-agent-target'))"
            )
        except Exception:
            pass
        return False


# ---- Public entry point -------------------------------------------------


UpdateStepFn = Callable[[str], Awaitable[None]]


async def configure_and_extract(
    url: str,
    spec: dict[str, Any],
    update_step: UpdateStepFn,
) -> ConfigResult:
    """Run the browser-agent loop on ``url`` against the user's spec.

    Always returns a ConfigResult — empty ``text`` and ``steps=0`` on failure
    so the caller can fall back to whatever the static extractor produced.
    Calls ``update_step(message)`` for every visible state change so the
    dashboard tile animates live.
    """
    if settings.fixture_mode:
        await update_step("(fixture mode) skipping configurator")
        return ConfigResult(text="", steps=0, history=[])

    browser = None
    try:
        browser = await _get_browser()
    except Exception as e:
        log.warning("configurator: browser launch failed: %s", e)
        return ConfigResult(text="", steps=0, history=[])

    ctx = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        )
    )
    page = await ctx.new_page()
    history: list[ActionDecision] = []
    steps_taken = 0
    text = ""

    try:
        await update_step("opening listing in a browser")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
            await page.wait_for_timeout(1800)
        except Exception as e:
            log.warning("configurator: goto failed for %s: %s", url, e)
            return ConfigResult(text="", steps=0, history=[])

        await update_step("scanning configuration options")

        for step_i in range(MAX_STEPS):
            elements = await _snapshot(page)
            try:
                shot = await page.screenshot(full_page=False, type="png")
            except Exception:
                shot = b""

            decision = await _decide(spec, history, elements, shot)
            history.append(decision)

            if decision.action == "done":
                await update_step("reading configured price")
                break
            if decision.action == "give_up":
                await update_step(f"giving up: {decision.reason}")
                break

            await update_step(decision.reason or f"step {step_i + 1}: {decision.action}")
            ok = await _execute(page, decision)
            if ok:
                steps_taken += 1
            await page.wait_for_timeout(_WAIT_AFTER_ACTION_MS)

        try:
            text = await page.evaluate("() => document.body.innerText") or ""
        except Exception:
            text = ""
    finally:
        try:
            await ctx.close()
        except Exception:
            pass

    return ConfigResult(text=text.strip(), steps=steps_taken, history=history)


# ---- Re-extract structured facts from the configured DOM text ----------


class _ConfiguredFacts(BaseModel):
    title: str | None = None
    price_cents: int | None = None
    condition: str | None = None
    seller: str | None = None
    shipping_cost_cents: int | None = None
    shipping_speed: str | None = None
    return_policy: str | None = None
    description_summary: str | None = None


_REEXTRACT_SYSTEM = """You read the rendered text of a product listing AFTER
configuration. Extract the structured facts as JSON. Use null for fields the
text does not state. Be conservative — the rendered DOM may include
unrelated content; the listing text is usually near a price + title +
condition cluster."""

_REEXTRACT_JSON = """Return ONE raw JSON object — no fences:

{
  "title": string|null,
  "price_cents": integer|null,
  "condition": string|null,
  "seller": string|null,
  "shipping_cost_cents": integer|null,
  "shipping_speed": string|null,
  "return_policy": string|null,
  "description_summary": string|null
}

price_cents and shipping_cost_cents are integers in US cents."""


async def extract_from_text(text: str) -> dict[str, Any]:
    """Pull structured facts out of the configured DOM text.

    Returns a partial dict (only non-null fields) so the caller can merge it
    over whatever the static extractor produced without overwriting good
    fields with null.
    """
    if not text:
        return {}
    snippet = text[:6000]
    gem = get_client()

    flash = await _reextract_one(gem, snippet, settings.gemini_model_researcher)
    flash_dict = (
        {k: v for k, v in flash.model_dump(exclude_none=False).items() if v is not None}
        if flash
        else {}
    )

    # Flash is fast and cheap; when it finds the price, we're done.
    if flash_dict.get("price_cents"):
        return flash_dict

    # Flash missed the price. Pro is meaningfully better at locating money
    # buried in messy DOM text (Apple's "From $X,XXX" right next to a
    # "Total: $Y,YYY" cluster, eBay variant grids, Newegg bundles). Cost is
    # at most one extra pro call per missed-price candidate × up to 2
    # escalations per intent — trivial vs. demo failure rate.
    pro = await _reextract_one(gem, snippet, settings.gemini_model_synthesizer)
    pro_dict = (
        {k: v for k, v in pro.model_dump(exclude_none=False).items() if v is not None}
        if pro
        else {}
    )

    # Merge: pro wins for anything it filled, flash fills the rest. Don't
    # blindly overwrite flash's good fields with pro's nulls.
    merged = dict(flash_dict)
    for k, v in pro_dict.items():
        if v not in (None, ""):
            merged[k] = v
    return merged


async def _reextract_one(
    gem: Any, snippet: str, model: str
) -> _ConfiguredFacts | None:
    """One re-extract pass against a Gemini model. Returns None on failure."""
    try:
        resp = await gem.aio.models.generate_content(
            model=model,
            contents=snippet + "\n\n" + _REEXTRACT_JSON,
            config=types.GenerateContentConfig(
                system_instruction=_REEXTRACT_SYSTEM,
                response_mime_type="application/json",
                response_schema=_ConfiguredFacts,
                max_output_tokens=1024,
            ),
        )
    except Exception as e:
        log.warning("configurator: re-extract (%s) failed: %s", model, e)
        return None

    parsed: _ConfiguredFacts | None = getattr(resp, "parsed", None)
    if parsed is not None:
        return parsed
    try:
        data = json.loads(resp.text or "{}")
        return _ConfiguredFacts(**data)
    except Exception:
        return None


__all__ = [
    "ConfigResult",
    "configure_and_extract",
    "extract_from_text",
    "should_escalate",
]
