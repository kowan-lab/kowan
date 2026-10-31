"""Crash snapshots HTTP API (see kowan/crashes and docs/crash-snapshots.md).

Routes (under /api/v1):

    GET    /crash-snapshots             newest first (?workload= ?hostId= ?limit=), summaries
    GET    /crash-snapshots/{id}        one, with its logs, configuration, environment, resources
    DELETE /crash-snapshots/{id}        delete (operator)

A snapshot is shown to whoever may read its workload.
"""

from __future__ import annotations

from aiohttp import web

from kowan import errors
from kowan.auth import authorize as authz
from kowan.crashes import CrashWatcher
from kowan.runbooks.model import access_target
from kowan.store import NotFoundError
from kowan.web import api_route, bind_svc, json_response, require_role


def _targets(snap):
    try:
        return [access_target(snap["workload"])]
    except (ValueError, KeyError):
        return [("container", snap.get("hostId", ""), snap.get("containerName", ""))]


async def _find(svc, sid: str):
    try:
        snap = svc.store.get_crash_snapshot(sid)
    except NotFoundError:
        raise errors.NotFound("crash snapshot not found") from None
    if not await authz.TargetAccess(svc.store, svc.docker).allowed_all(_targets(snap)):
        raise errors.NotFound("crash snapshot not found")
    return snap


async def _list(svc, request: web.Request) -> web.Response:
    q = request.query
    try:
        limit = int(q.get("limit", "50"))
    except ValueError:
        raise errors.Validation("limit must be a number") from None
    items = svc.store.list_crash_snapshots(q.get("workload", ""), q.get("hostId", ""), limit)
    return json_response(await authz.TargetAccess(svc.store, svc.docker).filter(items, _targets))


async def _get(svc, request: web.Request) -> web.Response:
    return json_response(await _find(svc, request.match_info["id"]))


async def _delete(svc, request: web.Request) -> web.Response:
    snap = await _find(svc, request.match_info["id"])
    await authz.TargetAccess(svc.store, svc.docker).check(_targets(snap), authz.LEVEL_OPERATE)
    svc.store.delete_crash_snapshot(snap["id"])
    return json_response({"status": "deleted"})


def register(app: web.Application, svc) -> None:
    watcher = CrashWatcher(svc)
    svc.attach("crashes", watcher)
    h = bind_svc(svc)
    api_route(app, "GET", "/crash-snapshots", h(_list))
    api_route(app, "GET", "/crash-snapshots/{id}", h(_get))
    api_route(app, "DELETE", "/crash-snapshots/{id}", require_role("operator")(h(_delete)))

    async def _on_startup(_app: web.Application) -> None:
        watcher.start()

    async def _on_cleanup(_app: web.Application) -> None:
        await watcher.stop()

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
