"""Settings → General: the server's external URL.

``GET /settings/general`` shows the value in use, the address the browser is
using now (a suggestion for an empty field) and whether ``KOWAN_EXTERNAL_URL``
is still in the environment, which can then be removed. ``PUT`` saves a new
value and applies it at once: SSO, join commands, MCP origins and HSTS read it
live. Saving tries the address from the server itself and returns a warning,
never an error, when it does not answer: behind NAT a server often cannot reach
its own public name, and that is fine.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List

from aiohttp import web

from kowan import externalurl, httpcap
from kowan.auth import require_role
from kowan.web import api_route, error_response, json_response, read_json
from kowan.web.responses import reject_unknown_fields

ENV = "KOWAN_EXTERNAL_URL"
_PROBE_TIMEOUT = 5.0


def register(app: web.Application, svc: Any) -> None:
    api_route(app, "GET", "/settings/general", require_role("admin")(_get))
    api_route(app, "PUT", "/settings/general", require_role("admin")(_put))


def _svc(request: web.Request) -> Any:
    from kowan.web import SERVICES_KEY

    return request.app[SERVICES_KEY]


def _view(request: web.Request, svc: Any) -> Dict[str, Any]:
    from kowan.api.hosts import _request_origin

    return {
        "externalUrl": getattr(svc.config, "external_url", "") or "",
        "currentOrigin": _request_origin(request),
        "envSet": bool(os.environ.get(ENV, "").strip()),
    }


async def _get(request: web.Request) -> web.Response:
    return json_response(_view(request, _svc(request)))


async def _answers(url: str) -> bool:
    """Whether ``url`` leads to a Kowan server (its public auth status)."""
    try:
        resp = await httpcap.request("GET", url + "/api/v1/auth/status", limit=16 << 10,
                                     timeout=_PROBE_TIMEOUT, follow_redirects=True)
    except Exception:  # noqa: BLE001 - unreachable from here
        return False
    return resp.status_code == 200


async def _put(request: web.Request) -> web.Response:
    svc = _svc(request)
    try:
        body = await read_json(request)
    except web.HTTPException:
        return error_response(400, "invalid request body")
    if not isinstance(body, dict):
        return error_response(400, "invalid request body")
    reject_unknown_fields(body, {"externalUrl"})
    raw = body.get("externalUrl", "")
    if not isinstance(raw, str):
        return error_response(400, "externalUrl must be a string")
    value, err = externalurl.normalize(raw)
    if err:
        return error_response(400, f"external URL {err}")

    warnings: List[str] = []
    if value and not await _answers(value):
        warnings.append(f"{value} does not answer from the server itself: check it from a browser "
                        "and from the agents' hosts")
    if value.startswith("http://") and not value.startswith(("http://localhost", "http://127.0.0.1")):
        warnings.append("over plain HTTP, sessions and SSO run without secure cookies")

    externalurl.save(svc, value)
    out = _view(request, svc)
    out["warnings"] = warnings
    return json_response(out)
