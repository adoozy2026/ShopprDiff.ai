"""Simple scam / mispricing heuristics.

v1 is intentionally rule-based and explainable — the dashboard surfaces the
``scam_reasons`` list verbatim so users can see *why* a tile got flagged.

Score is 0-100. Anything ≥40 displays as a warning; ≥70 as a strong block.
"""

from __future__ import annotations

from typing import Any


def score_scam(finding: dict[str, Any], spec: dict[str, Any]) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []

    price_cents = finding.get("price_cents")
    budget_cents = spec.get("budget_cents")

    # 1. Too-good-to-be-true vs the user's stated budget.
    if isinstance(price_cents, int) and isinstance(budget_cents, int) and budget_cents > 0:
        ratio = price_cents / budget_cents
        if ratio < 0.45:
            score += 35
            reasons.append(
                f"price (${price_cents/100:.0f}) is <45% of your budget "
                f"(${budget_cents/100:.0f}) — verify variant + condition"
            )

    # 2. No return policy is a meaningful risk signal for used goods.
    returns = (finding.get("return_policy") or "").lower()
    if returns and ("no returns" in returns or returns.strip() in {"none", "final sale"}):
        score += 30
        reasons.append("no returns accepted")

    # 3. Ships-from country mismatch — a US-only spec hitting an overseas shipper.
    ships_from = (finding.get("ships_from") or "").lower()
    deal_breakers = " ".join(spec.get("deal_breakers") or []).lower()
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

    # 5. Variant mismatch — extraction couldn't pin down a canonical variant.
    canon = finding.get("canonical_attrs") or {}
    if isinstance(canon, dict) and not canon:
        score += 10
        reasons.append("could not extract a clear product variant from listing")

    score = max(0, min(100, score))
    return score, reasons
