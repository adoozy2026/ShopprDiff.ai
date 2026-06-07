"""apple.com — deterministic PDP extractor.

Apple refurbished product pages (`/shop/product/<sku>/...`) embed a full
JSON-LD Product schema in the static HTML — price, condition, shipping
cost, return policy, image, and description are all there before any JS
runs. That's the data the browser-agent loop was spending 15-45s to
re-discover. This module lifts it directly.

Standard (non-refurbished) `/shop/buy-...` PDPs hide prices behind variant
selection and ship without JSON-LD prices; this extractor returns whatever
it can from `<meta og:*>` and lets the browser-agent fallback handle the
configurator dance.

Public surface:
    extract(page, spec)              — async, used by the extractor pool
    extract_from_html(html, url, …)  — pure helper, used by golden tests
"""

from __future__ import annotations

import html as _html
import json
import logging
import re
from typing import Any

from playwright.async_api import Page

log = logging.getLogger(__name__)


_JSON_LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
_OG_META_RE = re.compile(
    r'<meta[^>]+property=["\']og:([a-z]+)["\'][^>]+content=["\']([^"\']*)["\']',
    re.IGNORECASE,
)
_H1_TITLE_RE = re.compile(
    r'<h1[^>]+data-autom=["\']productTitle["\'][^>]*>(.*?)</h1>',
    re.IGNORECASE | re.DOTALL,
)
_CURRENT_PRICE_DIV_RE = re.compile(
    r'<div[^>]+class=["\'][^"\']*rf-pdp-currentprice[^"\']*["\'][^>]*>(.*?)</div>',
    re.IGNORECASE | re.DOTALL,
)
_DOLLARS_RE = re.compile(r"\$\s*([\d,]+(?:\.\d{1,2})?)")
_SELECT_RE = re.compile(
    r'<select[^>]+name=["\']([^"\']+)["\'][^>]*>(.*?)</select>',
    re.IGNORECASE | re.DOTALL,
)
_OPTION_SELECTED_RE = re.compile(
    r'<option[^>]+value=["\']([^"\']*)["\'][^>]*\sselected[^>]*>([^<]*)</option>',
    re.IGNORECASE,
)
_STORAGE_RE = re.compile(r"(\d+)\s*(GB|TB)", re.IGNORECASE)
_IPHONE_MODEL_RE = re.compile(
    r"iPhone\s+\d+(?:\s*Pro\s*Max|\s*Pro|\s*Plus|\s*mini)?",
    re.IGNORECASE,
)
_IPHONE_GEN_RE = re.compile(r"iPhone\s+(\d+)", re.IGNORECASE)

_CONDITION_MAP = {
    "newcondition": "new",
    "refurbishedcondition": "refurbished",
    "usedcondition": "used",
    "damagedcondition": "used",
}


async def extract(page: Page, spec: dict) -> dict:
    """Read the loaded Apple PDP and return ListingFacts-shaped dict."""
    try:
        await page.wait_for_selector(
            'script[type="application/ld+json"], h1[data-autom="productTitle"]',
            timeout=8000,
            state="attached",
        )
    except Exception:
        pass
    try:
        html = await page.content()
        url = page.url
    except Exception as e:
        log.warning("apple extract: page.content() failed: %s", e)
        return _empty()
    try:
        return extract_from_html(html, url, spec)
    except Exception as e:
        log.warning("apple extract: parse failed: %s", e)
        return _empty()


def extract_from_html(html: str, url: str, spec: dict | None = None) -> dict:
    """Pure HTML → facts. Safe to call from tests without a browser."""
    out = _empty()
    if not html:
        return out

    out["seller"] = "Apple"

    product = _find_product_jsonld(html)
    og = {k.lower(): _html.unescape(v) for k, v in _OG_META_RE.findall(html)}

    h1 = _H1_TITLE_RE.search(html)
    if h1:
        out["title"] = _normalize_ws(_html.unescape(_strip_tags(h1.group(1))))
    elif og.get("title"):
        out["title"] = og["title"]
    elif product and product.get("name"):
        out["title"] = str(product["name"])

    if og.get("image"):
        out["image_url"] = og["image"]
    elif product and product.get("image"):
        img = product["image"]
        out["image_url"] = img if isinstance(img, str) else (img[0] if img else None)

    desc = og.get("description") or (product.get("description") if product else None)
    if desc:
        out["description_summary"] = str(desc)[:300]

    if product:
        offer = _first_offer(product.get("offers"))
        if offer:
            price = offer.get("price")
            if price is not None:
                try:
                    out["price_cents"] = round(float(price) * 100)
                except (TypeError, ValueError):
                    pass

            cond_key = str(offer.get("itemCondition") or "").rsplit("/", 1)[-1].lower()
            if cond_key in _CONDITION_MAP:
                out["condition"] = _CONDITION_MAP[cond_key]

            ship = offer.get("shippingDetails")
            rate = (ship or {}).get("shippingRate") if isinstance(ship, dict) else None
            if isinstance(rate, dict) and "value" in rate:
                try:
                    out["shipping_cost_cents"] = round(float(rate["value"]) * 100)
                except (TypeError, ValueError):
                    pass

            ret = offer.get("hasMerchantReturnPolicy")
            if isinstance(ret, dict):
                days = ret.get("merchantReturnDays")
                if days is not None:
                    try:
                        out["return_policy"] = f"{int(days)}-day returns"
                    except (TypeError, ValueError):
                        pass

    if out["price_cents"] is None:
        m = _CURRENT_PRICE_DIV_RE.search(html)
        if m:
            dm = _DOLLARS_RE.search(m.group(1))
            if dm:
                try:
                    out["price_cents"] = round(float(dm.group(1).replace(",", "")) * 100)
                except (TypeError, ValueError):
                    pass

    if out["condition"] is None:
        haystack = f"{url} {out['title'] or ''}".lower()
        if "refurbished" in haystack or "/shop/refurbished" in url.lower():
            out["condition"] = "refurbished"

    out["canonical_attrs"] = _canonical_attrs(html, out["title"], product)
    return out


def _empty() -> dict:
    return {
        "title": None,
        "price_cents": None,
        "condition": None,
        "seller": None,
        "shipping_cost_cents": None,
        "shipping_speed": None,
        "return_policy": None,
        "image_url": None,
        "description_summary": None,
        "canonical_attrs": {},
    }


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s)


def _normalize_ws(s: str) -> str:
    # Preserve U+00A0 (NBSP): Apple uses it deliberately in "iPhone 15 Pro"
    # branding and the LLM extractor preserved it too.
    return s.replace("\t", " ").replace("\r", "").strip()


def _find_product_jsonld(html: str) -> dict | None:
    for raw in _JSON_LD_RE.findall(html):
        try:
            data = json.loads(raw.strip())
        except Exception:
            continue
        for d in data if isinstance(data, list) else [data]:
            if isinstance(d, dict) and str(d.get("@type", "")).lower() == "product":
                return d
    return None


def _first_offer(offers: Any) -> dict | None:
    if isinstance(offers, dict):
        return offers
    if isinstance(offers, list):
        for o in offers:
            if isinstance(o, dict):
                return o
    return None


def _canonical_attrs(html: str, title: str | None, product: dict | None) -> dict:
    attrs: dict[str, Any] = {"brand": "Apple"}

    selected: dict[str, tuple[str, str]] = {}
    for name, body in _SELECT_RE.findall(html):
        opt = _OPTION_SELECTED_RE.search(body)
        if opt:
            selected[name] = (opt.group(1), _html.unescape(opt.group(2).strip()))

    if "dimensionColor" in selected:
        attrs["color"] = selected["dimensionColor"][1]
    elif product and product.get("color"):
        attrs["color"] = str(product["color"])

    storage = None
    if "dimensionCapacity" in selected:
        value, label = selected["dimensionCapacity"]
        storage = _parse_storage(label) or _parse_storage(value)
    if storage is None and title:
        storage = _parse_storage(title)
    if storage is not None:
        attrs["storage_gb"] = storage

    if title:
        gen = _IPHONE_GEN_RE.search(title)
        if gen:
            attrs["generation"] = gen.group(1)
        model = _IPHONE_MODEL_RE.search(title)
        if model:
            attrs["model"] = _normalize_ws(model.group(0))
        if "unlocked" in title.lower():
            attrs["carrier_lock"] = "unlocked"

    return attrs


def _parse_storage(s: str) -> int | None:
    if not s:
        return None
    m = _STORAGE_RE.search(s)
    if not m:
        return None
    n = int(m.group(1))
    return n * 1024 if m.group(2).upper() == "TB" else n
