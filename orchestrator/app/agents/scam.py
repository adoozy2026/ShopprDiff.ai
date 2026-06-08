"""Simple scam / mispricing heuristics.

v1 is intentionally rule-based and explainable — the dashboard surfaces the
``scam_reasons`` list verbatim so users can see *why* a tile got flagged.

Score is 0-100. Anything ≥40 displays as a warning; ≥70 as a strong block.
"""

from __future__ import annotations

import re
from typing import Any

_BUDGET_RE = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)")


def _extract_budget_cents(spec: dict[str, Any]) -> int | None:
    """Parse a numeric budget (in cents) from category values or raw_query."""
    categories = spec.get("categories") or {}
    for entry in categories.values():
        if not isinstance(entry, dict):
            continue
        value = entry.get("value") or ""
        m = _BUDGET_RE.search(value)
        if m:
            try:
                return int(float(m.group(1).replace(",", "")) * 100)
            except ValueError:
                continue
    raw = spec.get("raw_query") or ""
    m = _BUDGET_RE.search(raw)
    if m:
        try:
            return int(float(m.group(1).replace(",", "")) * 100)
        except ValueError:
            pass
    return None


def _deal_breaker_text(spec: dict[str, Any]) -> str:
    """Aggregate all deal_breaker category values into a single lowercase string."""
    categories = spec.get("categories") or {}
    parts: list[str] = []
    for entry in categories.values():
        if isinstance(entry, dict) and entry.get("type") == "deal_breaker":
            parts.append(entry.get("value") or "")
    return " ".join(parts).lower()


def _finding_attr(finding: dict[str, Any], key: str) -> Any:
    """Look for a key in the finding, falling back to spec_attrs.

    Fields like return_policy or ships_from are now extracted dynamically into
    spec_attrs rather than as top-level finding fields. This helper checks
    both locations so scoring works with old and new finding shapes.
    """
    val = finding.get(key)
    if val is not None:
        return val
    spec_attrs = finding.get("spec_attrs") or {}
    return spec_attrs.get(key)


def score_scam(finding: dict[str, Any], spec: dict[str, Any]) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []

    price_cents = finding.get("price_cents")
    budget_cents = _extract_budget_cents(spec)

    # 1. Too-good-to-be-true vs the user's stated budget.
    if isinstance(price_cents, int) and isinstance(budget_cents, int) and budget_cents > 0:
        ratio = price_cents / budget_cents
        if ratio < 0.45:
            score += 35
            reasons.append(
                f"price (${price_cents / 100:.0f}) is <45% of your budget "
                f"(${budget_cents / 100:.0f}) — verify variant + condition"
            )

    # 2. No return policy is a meaningful risk signal for used goods.
    returns = (_finding_attr(finding, "return_policy") or "").lower()
    if returns and ("no returns" in returns or returns.strip() in {"none", "final sale"}):
        score += 30
        reasons.append("no returns accepted")

    # 3. Ships-from country mismatch — a US-only spec hitting an overseas shipper.
    ships_from = (_finding_attr(finding, "ships_from") or "").lower()
    deal_breakers = _deal_breaker_text(spec)
    if ships_from and ("us" in deal_breakers or "united states" in deal_breakers):
        if not any(
            tok in ships_from for tok in ("united states", "usa", " us", "u.s.")
        ) and ships_from.strip() not in {"us", "u.s.", "united states"}:
            score += 20
            reasons.append(f"ships from {ships_from!r} — user wants US-based seller")

    # 4. Seller reputation flag surfaced by the seller-rep step.
    sr = (finding.get("seller_rep") or "").lower()
    if any(tok in sr for tok in ("scam", "fraud", "complaints about", "warning")):
        score += 25
        reasons.append("seller reputation lookup surfaced scam mentions")

    # 5. Variant mismatch — extraction couldn't pin down spec-relevant attrs.
    spec_attrs = finding.get("spec_attrs") or {}
    categories = spec.get("categories") or {}
    if categories and isinstance(spec_attrs, dict):
        filled = sum(1 for v in spec_attrs.values() if v is not None)
        if filled == 0:
            score += 10
            reasons.append("could not extract any spec-relevant attributes from listing")

    score = max(0, min(100, score))
    return score, reasons
