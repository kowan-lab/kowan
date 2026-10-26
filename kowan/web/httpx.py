"""Small helpers for writing and decoding JSON HTTP responses.

Every REST handler uses these helpers so the response envelope and
body-decoding semantics are uniform:

  - json_response(data, status)   JSON body (models dumped by alias)
  - error_response(status, msg)   {"error": msg}
  - read_json(request)            400 on bad JSON, 1 MiB body limit
"""

from __future__ import annotations

import json
import logging
from typing import Any

from aiohttp import web

from .. import jsonx

log = logging.getLogger("kowan.httpx")

# Reject request bodies larger than 1 MiB.
_MAX_BODY = 1 << 20  # 1 MiB

# Content-Type used for every JSON body.
_JSON_CT = "application/json; charset=utf-8"


def json_response(data: Any, status: int = 200) -> web.Response:
    """Write ``data`` as a JSON body with the given HTTP status code.

    A ``None`` value produces an empty body (useful for 204 responses).
    """
    if data is None:
        return web.Response(status=status, content_type=None, headers={"Content-Type": _JSON_CT})
    # orjson when available (kowan/jsonx.py), stdlib otherwise; same JSON value,
    # compact, key order preserved, NaN/Inf -> null, default=str semantics.
    try:
        body = jsonx.dumps_response(data)
    except (TypeError, ValueError) as err:  # pragma: no cover - defensive
        log.error("httpx: failed to encode JSON response: %s", err)
        body = b"null"
    return web.Response(status=status, body=body, content_type="application/json", charset="utf-8")


def error_response(status: int, message: str) -> web.Response:
    """Write a JSON error envelope ``{"error": msg}`` with the given status."""
    return json_response({"error": message}, status=status)


def ops_error_response(exc: BaseException) -> web.Response:
    """Map a Docker/agent operation failure to an error envelope.

    Typed application errors keep their status (404 for a missing container,
    400 for invalid input, 409 for conflicts); anything else is an upstream
    failure and stays 502 Bad Gateway.
    """
    from ..errors import AppError

    if isinstance(exc, AppError) and 400 <= int(exc.status) < 600:
        return error_response(int(exc.status), str(exc) or exc.__class__.__name__)
    return error_response(502, str(exc))


class JSONError(web.HTTPException):
    """An HTTP error carrying the ``{"error": msg}`` JSON envelope.

    Handlers may raise this (or it is raised by read_json) and the error
    middleware / aiohttp will surface the JSON body with the right status.
    """

    def __init__(self, status: int, message: str) -> None:
        self.status_code = status
        super().__init__(
            text=json.dumps({"error": message}, separators=(",", ":")),
            content_type="application/json",
        )


async def read_body(request: web.Request, max_bytes: int) -> bytes:
    """Read the request body IN FULL, buffering at most ``max_bytes``.

    Do NOT use ``request.content.read(n)`` for this: aiohttp's StreamReader
    returns as soon as ANY data is buffered — it reads *up to* n bytes and never
    waits for n — so a body arriving in more than one chunk is silently
    truncated (a multi-KiB YAML spec would reach json.loads half-written, which
    surfaces as "Unterminated string"). Loop until EOF instead, stopping once
    ``max_bytes`` is buffered so an oversized body is never read in full.
    """
    chunks: list[bytes] = []
    size = 0
    while size < max_bytes:
        chunk = await request.content.read(max_bytes - size)
        if not chunk:  # EOF
            break
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks)


async def read_json(request: web.Request) -> Any:
    """Read and decode a JSON request body.

    Enforces the 1 MiB limit and raises a 400 JSONError on malformed JSON
    ("decode request body: ...").
    """
    raw = await read_body(request, _MAX_BODY + 1)
    if len(raw) > _MAX_BODY:
        raise JSONError(400, "decode request body: request body too large")
    if not raw:
        # Match json.NewDecoder on an empty body: EOF -> decode error.
        raise JSONError(400, "decode request body: EOF")
    try:
        return jsonx.loads(raw)
    except (ValueError, UnicodeDecodeError) as err:
        raise JSONError(400, f"decode request body: {err}") from err


def reject_unknown_fields(body: dict, allowed: set) -> None:
    """Raise a 400 if ``body`` carries a key outside ``allowed``.

    A strict decode: any request body field absent from the expected shape is
    rejected. Apply this on endpoints that hand-parse a dict body; the
    pydantic-validated paths already model their own shape.
    """
    for key in body:
        if key not in allowed:
            raise JSONError(400, f'decode request body: json: unknown field "{key}"')
