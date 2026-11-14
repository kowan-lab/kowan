"""Settings that used to be environment variables (see :mod:`kowan.tunables`).

``GET /settings/tunables[?group=retention|monitoring|mcp|advanced]`` lists them
with their current value, default and whether the old variable is still in the
environment (no longer read, so it can be removed). ``PUT`` takes
``{"values": {key: text or null}}``: all or nothing, ``null`` restores the
default, and the change applies at once. Admins only; audited like every
mutation.
"""

from __future__ import annotations

from typing import Any

from aiohttp import web

from kowan import tunables
from kowan.auth import require_role
from kowan.web import api_route, error_response, json_response, read_json
from kowan.web.responses import reject_unknown_fields


def register(app: web.Application, svc: Any) -> None:
    api_route(app, "GET", "/settings/tunables", require_role("admin")(_get))
    api_route(app, "PUT", "/settings/tunables", require_role("admin")(_put))


def _svc(request: web.Request) -> Any:
    from kowan.web import SERVICES_KEY

    return request.app[SERVICES_KEY]


def _listing(group: str) -> dict:
    return {"settings": [tunables.describe(t) for t in tunables.REGISTRY if not group or t.group == group]}


async def _get(request: web.Request) -> web.Response:
    return json_response(_listing(request.query.get("group", "")))


async def _put(request: web.Request) -> web.Response:
    try:
        body = await read_json(request)
    except web.HTTPException:
        return error_response(400, "invalid request body")
    if not isinstance(body, dict):
        return error_response(400, "invalid request body")
    reject_unknown_fields(body, {"values", "group"})
    values = body.get("values")
    if not isinstance(values, dict) or not values:
        return error_response(400, "values must be an object of settings")
    errors = tunables.save(_svc(request).store, values)
    if errors:
        return json_response({"error": "; ".join(errors.values()), "fields": errors}, status=400)
    return json_response(_listing(str(body.get("group") or "")))
