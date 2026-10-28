"""Newer version tags for a deployment's images (``kowan/deployment/tagupdates.py``).

  GET  /deployments/{id}/tag-updates            per service: current tag and newer ones (any role)
  POST /deployments/{id}/tag-updates/preview    {tags: {service: tag}} -> changes + diff (operator)
  POST /deployments/{id}/tag-updates            {tags, deploy?: bool=true} -> new revision, redeploy (operator)

A container deployment has one service, keyed "".
"""

from __future__ import annotations

from typing import Any

from aiohttp import web

from kowan.auth import authorize as authz
from kowan.auth import require_role
from kowan.auth.middleware import user_from
from kowan.deployment import tagupdates
from kowan.errors import Conflict, NotFound, Validation
from kowan.web import api_route, json_response, read_json, reject_unknown_fields
from kowan.web.app import SERVICES_KEY


def register(app: web.Application, svc: Any) -> None:
    op = require_role("operator")
    api_route(app, "GET", "/deployments/{id}/tag-updates", _check)
    api_route(app, "POST", "/deployments/{id}/tag-updates/preview", op(_preview))
    api_route(app, "POST", "/deployments/{id}/tag-updates", op(_apply))


def _load(request: web.Request, level: str) -> Any:
    svc = request.app[SERVICES_KEY]
    try:
        dep = svc.store.get_deployment(request.match_info["id"])
    except Exception as err:  # noqa: BLE001
        raise NotFound("deployment not found") from err
    authz.check_deployment(dep, level)
    if dep.git is not None:
        raise Validation("a git deployment takes its images from the repository: change the tags there")
    return svc, dep


async def _check(request: web.Request) -> web.Response:
    svc, dep = _load(request, authz.LEVEL_READ)
    return json_response({"items": await tagupdates.check(svc.store, dep)})


async def _body(request: web.Request, allowed: set) -> dict:
    body = await read_json(request)
    if not isinstance(body, dict):
        raise Validation("invalid request body")
    reject_unknown_fields(body, allowed)
    return body


async def _preview(request: web.Request) -> web.Response:
    _, dep = _load(request, authz.LEVEL_OPERATE)
    body = await _body(request, {"tags"})
    _, changes, diff = tagupdates.plan(dep, body.get("tags") or {})
    return json_response({"changes": changes, "diff": diff})


async def _apply(request: web.Request) -> web.Response:
    from kowan.api.deployments import GateRefused, _for_reader, _gate, _now_rfc3339

    svc, dep = _load(request, authz.LEVEL_OPERATE)
    body = await _body(request, {"tags", "deploy"})
    after, changes, _ = tagupdates.plan(dep, body.get("tags") or {})
    try:
        await _gate(svc, after)
    except GateRefused as err:
        return json_response(err.report, status=400)
    after.updated_at = _now_rfc3339()
    u = user_from(request)
    svc.store.update_deployment(after, author=getattr(u, "username", "") if u else "", source="tags",
                                note=tagupdates.note_of(changes))
    if body.get("deploy", True) is not False and after.enabled:
        try:
            after = await svc.deployments.dispatch_deploy(after.id, redeploy=True)
        except Conflict:
            pass  # a deploy is already running; it picks up the new tags next
    return json_response({"changes": changes, "deployment": _for_reader(request, after)})
