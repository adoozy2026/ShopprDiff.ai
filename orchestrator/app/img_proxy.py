"""Pass-through image proxy.

Retailers frequently 403 hotlinked image requests from origins other than
their own. The dashboard ``<img>`` tags route through this endpoint so the
orchestrator can fetch the image with a Referer matching the source page,
sidestepping that protection. Also handy when the retailer enforces
``Referer`` or ``User-Agent`` filtering at the CDN.

This is intentionally minimal — no caching, no resizing. For 24h hackathon
volume the laptop bandwidth is plenty.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

router = APIRouter()
log = logging.getLogger(__name__)

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


@router.get("/img")
async def img(url: str = Query(..., min_length=8), request: Request = None):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(status_code=400, detail="bad url")

    # Referer matching the image's own origin — most CDN hotlink rules
    # accept same-origin requests.
    referer = f"{parsed.scheme}://{parsed.hostname}/"

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=10.0,
            headers={
                "User-Agent": _BROWSER_UA,
                "Referer": referer,
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            },
        ) as client:
            r = await client.get(url)
    except Exception as e:
        log.debug("img proxy: fetch failed for %s: %s", url, e)
        raise HTTPException(status_code=502, detail="upstream fetch failed")

    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail="upstream rejected")

    ct = r.headers.get("content-type", "")
    if not ct.startswith("image/"):
        raise HTTPException(status_code=415, detail=f"not an image: {ct}")

    return Response(
        content=r.content,
        media_type=ct,
        headers={
            # Browsers cache product images aggressively — fine for our use.
            "Cache-Control": "public, max-age=3600",
            "Access-Control-Allow-Origin": "*",
        },
    )
