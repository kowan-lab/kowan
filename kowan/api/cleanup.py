"""Guided and scheduled disk cleanup of a host (kowan/cleanup.py).

Routes (under /api/v1):
  GET /hosts/{hostID}/cleanup           what can go, with sizes and default picks,
                                        plus the schedule and the last scheduled run
  POST /hosts/{hostID}/cleanup          {containers, images, volumes, buildCache}  (operator)
  PUT /hosts/{hostID}/cleanup/schedule  the schedule policy                         (operator)

Removals go through the host's Ops, so team-scoped users need the same
grants as for removing each object by hand.
"""

from __future__ import annotations

from aiohttp import web

from kowan import cleanup
from kowan.auth import require_role
from kowan.errors import NotFound, Validation
from kowan.web import api_route, json_response, ops_error_response, read_json, reject_unknown_fields
from kowan.web.app import SERVICES_KEY


def register(app: web.Application, svc) -> None:
    sched = cleanup.Scheduler(svc)
    svc.attach("cleanup", sched)
    op = require_role("operator")
    api_route(app, "GET", "/hosts/{hostID}/cleanup", _plan)
    api_route(app, "POST", "/hosts/{hostID}/cleanup", op(_run))
    api_route(app, "PUT", "/hosts/{hostID}/cleanup/schedule", op(_schedule))

    async def _start(_app: web.Application) -> None:
        sched.start()

    async def _stop(_app: web.Application) -> None:
        await sched.stop()

    app.on_startup.append(_start)
    app.on_cleanup.append(_stop)


def _host(request: web.Request):
    svc = request.app[SERVICES_KEY]
    try:
        return svc, svc.store.get_host(request.match_info["hostID"])
    except Exception as err:  # noqa: BLE001
        raise NotFound("host not found") from err


async def _plan(request: web.Request) -> web.Response:
    svc, host = _host(request)
    try:
        du = await svc.docker.ops(host).disk_usage()
    except Exception as err:  # noqa: BLE001 - offline host, refused grant
        return ops_error_response(err)
    out = cleanup.plan(du, host.id, svc.store)
    out["schedule"] = cleanup.get_policy(svc.store, host.id)
    out["lastRun"] = cleanup.last_run(svc.store, host.id)
    return json_response(out)


def _ids(body: dict, key: str) -> list:
    v = body.get(key) or []
    if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
        raise Validation(f"{key} must be a list of ids")
    return v


async def _run(request: web.Request) -> web.Response:
    svc, host = _host(request)
    body = await read_json(request)
    if not isinstance(body, dict):
        raise Validation("invalid request body")
    reject_unknown_fields(body, {"containers", "images", "volumes", "buildCache"})
    picked = cleanup.Picked(_ids(body, "containers"), _ids(body, "images"), _ids(body, "volumes"),
                            body.get("buildCache") is True)
    if not (picked.containers or picked.images or picked.volumes or picked.build_cache):
        raise Validation("pick something to remove")
    try:
        return json_response(await cleanup.run(svc.docker.ops(host), picked))
    except Exception as err:  # noqa: BLE001 - the host went away mid-run
        return ops_error_response(err)


async def _schedule(request: web.Request) -> web.Response:
    svc, host = _host(request)
    body = await read_json(request)
    if not isinstance(body, dict):
        raise Validation("invalid request body")
    try:
        policy = cleanup.validate_policy(body)
    except ValueError as err:
        raise Validation(str(err)) from None
    cleanup.set_policy(svc.store, host.id, policy)
    return json_response(policy)
