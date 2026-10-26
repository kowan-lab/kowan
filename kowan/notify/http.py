"""Shared HTTP helpers for the outbound notification providers.

All non-SMTP providers POST to a
remote endpoint; these helpers centralise the shared client, timeout, and the
"any non-2xx response is an error" contract so per-provider ``send`` methods stay
tiny.

Uses ``httpx`` (async) — the one allowed HTTP client dep — instead of a shared
module-level client, because an httpx.AsyncClient is bound to a
specific event loop; a per-call client keeps the providers usable from whatever
loop the notifier runs on.
"""

from __future__ import annotations

import json as _json
from typing import Any, Dict, Optional

import httpx

from kowan.httpcap import request_capped

from kowan.errors import BadGateway

# httpTimeout bounds every outbound HTTP provider request.
HTTP_TIMEOUT = 10.0


async def post_json(
    url: str, headers: Optional[Dict[str, str]], body: Any
) -> None:
    """Marshal ``body`` to JSON and POST it to ``url``.

    Raises :class:`kowan.errors.BadGateway` on transport failure or any non-2xx
    response.
    """
    payload = _json.dumps(body, separators=(",", ":")).encode("utf-8")
    await post(url, "application/json", headers, payload)


async def post(
    url: str,
    content_type: str,
    headers: Optional[Dict[str, str]],
    body: bytes,
) -> None:
    """POST ``body`` to ``url`` with the given content type and headers.

    Raises :class:`kowan.errors.BadGateway` on transport failure or any non-2xx
    response (the response snippet is included in the error).
    """
    hdrs: Dict[str, str] = {}
    if content_type:
        hdrs["Content-Type"] = content_type
    if headers:
        hdrs.update(headers)

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await request_capped(client, "POST", url, 512, content=body, headers=hdrs)
    except httpx.HTTPError as err:
        raise BadGateway(f"do request: {err}") from err

    if resp.status_code < 200 or resp.status_code >= 300:
        snippet = resp.content[:512].strip()
        text = snippet.decode("utf-8", "replace")
        raise BadGateway(f"unexpected status {resp.status_code}: {text}")


__all__ = ["HTTP_TIMEOUT", "post_json", "post"]
