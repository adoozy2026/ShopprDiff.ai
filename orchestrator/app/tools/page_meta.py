"""Lightweight OpenGraph / meta-tag scraper.

Used by the Researcher to source product images and human-readable titles
*without* an LLM call — works even when Gemini is rate-limited, and is the
truth source for ``image_url`` since the LLM extractor often returns null
for image fields even when the page has a perfectly good ``og:image``.

We deliberately do NOT parse the whole DOM. A few regexes over the first
~64KB of the response are plenty for the meta tags we want, and avoid
pulling in BeautifulSoup just for this.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx

log = logging.getLogger(__name__)

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# Match either ``content="..."`` or ``content='...'`` after a property/name
# attribute. We accept both attribute orders (property before content and
# vice-versa) because retailers do not agree on a convention.
_META_RE = re.compile(
    r"""<meta\s+[^>]*?
        (?:property|name)\s*=\s*["']([^"']+)["']\s+
        [^>]*?content\s*=\s*["']([^"']*)["']
        [^>]*?>""",
    re.IGNORECASE | re.VERBOSE,
)
_META_RE_REV = re.compile(
    r"""<meta\s+[^>]*?
        content\s*=\s*["']([^"']*)["']\s+
        [^>]*?(?:property|name)\s*=\s*["']([^"']+)["']
        [^>]*?>""",
    re.IGNORECASE | re.VERBOSE,
)


@dataclass
class PageMeta:
    title: str | None = None
    description: str | None = None
    image_url: str | None = None


def _parse_meta(html: str, base_url: str) -> PageMeta:
    pairs: dict[str, str] = {}
    for m in _META_RE.finditer(html):
        pairs.setdefault(m.group(1).lower(), m.group(2))
    for m in _META_RE_REV.finditer(html):
        pairs.setdefault(m.group(2).lower(), m.group(1))

    title = (
        pairs.get("og:title")
        or pairs.get("twitter:title")
        or _first_tag(html, "title")
    )
    description = pairs.get("og:description") or pairs.get("description")
    image = (
        pairs.get("og:image")
        or pairs.get("og:image:secure_url")
        or pairs.get("twitter:image")
    )
    if image:
        image = urljoin(base_url, image)
    return PageMeta(
        title=title.strip() if title else None,
        description=description.strip() if description else None,
        image_url=image,
    )


_TITLE_TAG_RE = re.compile(r"<title[^>]*>([^<]+)</title>", re.IGNORECASE)


def _first_tag(html: str, tag: str) -> str | None:
    if tag != "title":
        return None
    m = _TITLE_TAG_RE.search(html)
    return m.group(1) if m else None


async def fetch_page_meta(url: str, timeout: float = 8.0) -> PageMeta:
    """Best-effort fetch of OpenGraph meta tags for a product page.

    Returns an empty PageMeta on any error (4xx, 5xx, timeout, missing tags).
    Also validates the discovered image URL with a HEAD request — retailers
    frequently publish stale or wrong ``og:image`` URLs, and storing a broken
    one is worse than storing nothing because the dashboard reserves space
    for a tile thumbnail. Never raises.
    """
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers={
                "User-Agent": _BROWSER_UA,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            },
        ) as client:
            r = await client.get(url)
            if r.status_code >= 400:
                log.debug("page_meta: %s returned %d", url, r.status_code)
                return PageMeta()
            html = r.text[:65536]
            base = str(r.url)
            meta = _parse_meta(html, base)
            if meta.image_url and not await _is_image_ok(client, meta.image_url, base):
                log.info("page_meta: dropping broken image %s", meta.image_url)
                meta.image_url = None
            return meta
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.debug("page_meta: fetch failed for %s: %s", url, e)
        return PageMeta()


async def _is_image_ok(client: httpx.AsyncClient, img_url: str, referer: str) -> bool:
    """HEAD-probe ``img_url`` with a browser-like Referer matching the source
    page. Returns True only if the response is 2xx and looks like an image.
    Some CDNs reject HEAD — for those, fall back to a small Range GET.
    """
    headers = {"Referer": referer}
    try:
        r = await client.head(img_url, headers=headers)
    except Exception:
        return False
    if r.status_code < 400 and "image" in (r.headers.get("content-type") or ""):
        return True
    if r.status_code in (405, 501):
        try:
            r = await client.get(img_url, headers={**headers, "Range": "bytes=0-127"})
            return r.status_code < 400 and "image" in (r.headers.get("content-type") or "")
        except Exception:
            return False
    return False
