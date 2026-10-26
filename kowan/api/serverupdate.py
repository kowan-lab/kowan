"""Platform self-update HTTP handlers (server + shared + rolling agent batches).

Mounts, under ``/api/v1`` (contract: ``docs/updates-design.md``):

  - GET  /system/server/update    -> status of the server (admin)
  - POST /system/server/update    -> body {image?} -> 202 {id} (admin)
  - POST /system/server/rollback  -> body {restoreDb?} -> 202 {id} (admin)
  - GET  /system/server/updates   -> history, ?limit=20 (admin)
  - GET  /system/updates/{id}     -> one record, for polling (server records:
                                     admin; agent records: operator)
  - POST /agents/update           -> body {hostIds, image?} -> 202 {batchId, ids}
                                     rolling, one host at a time (admin)

The per-host agent routes live in :mod:`kowan.api.hosts` and share the helpers
below. Logic lives in :mod:`kowan.updater`.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from aiohttp import web

from kowan.auth import require_role, user_from
from kowan.errors import Conflict, Forbidden, NotFound, Validation
from kowan.web import SERVICES_KEY, api_route, json_response, read_json, reject_unknown_fields

log = logging.getLogger("kowan.api.serverupdate")


def register(app: web.Application, svc) -> None:
    api_route(app, "GET", "/system/server/update", require_role("admin")(_status))
    api_route(app, "POST", "/system/server/update", require_role("admin")(_update))
    api_route(app, "POST", "/system/server/rollback", require_role("admin")(_rollback))
    api_route(app, "GET", "/system/server/updates", require_role("admin")(_history))
    api_route(app, "GET", "/system/updates/{id}", require_role("operator")(_get_update))
    api_route(app, "POST", "/agents/update", require_role("admin")(_rolling))


def _svc(request: web.Request):
    return request.app[SERVICES_KEY]


def manager(svc) -> Any:
    from kowan.serverupdate import _manager

    return _manager(svc)


def username(request: web.Request) -> str:
    u = user_from(request)
    return getattr(u, "username", "") or ""


async def optional_body(request: web.Request, allowed: set) -> Dict[str, Any]:
    """Parse an OPTIONAL JSON object body (empty body = {})."""
    if not request.can_read_body:
        return {}
    raw = await request.read()
    if not raw.strip():
        return {}
    import json

    try:
        body = json.loads(raw)
    except ValueError as err:
        raise Validation(f"decode request body: {err}")
    if not isinstance(body, dict):
        raise Validation("request body must be a JSON object")
    reject_unknown_fields(body, allowed)
    return body


def image_field(body: Dict[str, Any]) -> str:
    image = body.get("image") or ""
    if not isinstance(image, str):
        raise Validation("image must be a string")
    return image.strip()


def raise_update_error(err: Exception) -> None:
    status = getattr(err, "status", 400)
    if status == 409:
        raise Conflict(str(err))
    raise Validation(str(err))


def limit_param(request: web.Request) -> int:
    try:
        n = int(request.query.get("limit", "20"))
    except ValueError:
        raise Validation("limit must be an integer")
    return max(1, min(n, 200))


async def _status(request: web.Request) -> web.Response:
    from kowan.updater import COMPONENT_SERVER

    return json_response(await manager(_svc(request)).status(COMPONENT_SERVER))


async def _start(request: web.Request, mode: str, body: Dict[str, Any]) -> web.Response:
    from kowan.updater import COMPONENT_SERVER
    from kowan.updater.orchestrator import UpdateError

    try:
        uid = await manager(_svc(request)).start(
            COMPONENT_SERVER, None, mode=mode, image=image_field(body), user=username(request),
            restore_db=bool(body.get("restoreDb", True)),
        )
    except UpdateError as err:
        raise_update_error(err)
    return json_response({"id": uid}, status=202)


async def _update(request: web.Request) -> web.Response:
    return await _start(request, "update", await optional_body(request, {"image"}))


async def _rollback(request: web.Request) -> web.Response:
    body = await optional_body(request, {"restoreDb"})
    if "restoreDb" in body and not isinstance(body["restoreDb"], bool):
        raise Validation("restoreDb must be a boolean")
    return await _start(request, "rollback", body)


async def _history(request: web.Request) -> web.Response:
    from kowan.updater import COMPONENT_SERVER

    return json_response(_svc(request).store.list_updates(COMPONENT_SERVER, None, limit_param(request)))


async def _get_update(request: web.Request) -> web.Response:
    from kowan.updater import COMPONENT_SERVER

    svc = _svc(request)
    try:
        rec = svc.store.get_update(request.match_info["id"])
    except Exception:  # noqa: BLE001 - store NotFoundError
        raise NotFound("update not found")
    u = user_from(request)
    if rec["component"] == COMPONENT_SERVER and getattr(u, "role", "") != "admin":
        raise Forbidden("insufficient permissions")
    return json_response(rec)


async def _rolling(request: web.Request) -> web.Response:
    from kowan.models.host import HostTypeAgent
    from kowan.updater.orchestrator import UpdateError

    svc = _svc(request)
    body = await read_json(request)
    if not isinstance(body, dict):
        raise Validation("invalid request body")
    reject_unknown_fields(body, {"hostIds", "image"})
    ids = body.get("hostIds")
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) and i for i in ids):
        raise Validation("hostIds must be a non-empty array of host ids")
    if len(set(ids)) != len(ids):
        raise Validation("hostIds must not contain duplicates")
    hosts = []
    for hid in ids:
        try:
            h = svc.store.get_host(hid)
        except Exception:  # noqa: BLE001
            raise NotFound(f"host {hid} not found")
        if getattr(h, "type", "") != HostTypeAgent:
            raise Validation(f"host {hid} is not an agent host")
        hosts.append(h)
    try:
        res = await manager(svc).start_batch(hosts, image=image_field(body), user=username(request))
    except UpdateError as err:
        raise_update_error(err)
    return json_response(res, status=202)
