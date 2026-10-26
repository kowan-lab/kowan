"""Scheduled start/stop HTTP API (see docs/schedules.md).

Routes (under /api/v1):

    GET    /schedules                  list stored + label schedules (?targetType&targetId&hostId)
    POST   /schedules                  create (operator)
    GET    /schedules/runs             run history (?scheduleId&limit)
    POST   /schedules/preview          next-run preview {cron, timezone, count}
    POST   /schedules/rescan           re-scan kowan.schedule.* labels now (operator)
    GET    /schedules/{id}             one schedule
    PUT    /schedules/{id}             update (operator; label schedules are read-only)
    DELETE /schedules/{id}             delete (operator)
    POST   /schedules/{id}/run         run now (operator; also for label schedules)

The :class:`ScheduleService` (attached as ``svc.schedules``) runs on its own
CronRunner started from this module's on_startup hook, so server.py only needs
the registrar entry.
"""

from __future__ import annotations

import logging

from aiohttp import web

from kowan import errors
from kowan.schedules.service import ScheduleService, validate_schedule
from kowan.store import NotFoundError
from kowan.web import api_route, bind_svc, json_response, read_json, require_role

log = logging.getLogger("kowan.api.schedules")


def _service(svc) -> ScheduleService:
    return svc.schedules


async def _list(svc, request: web.Request) -> web.Response:
    q = request.query
    items = _service(svc).all_schedules()
    for key in ("targetType", "targetId", "hostId"):
        v = q.get(key, "")
        if v:
            items = [s for s in items if s.get(key) == v]
    return json_response(items)


async def _get(svc, request: web.Request) -> web.Response:
    return json_response(_service(svc).find(request.match_info["id"]))


async def _create(svc, request: web.Request) -> web.Response:
    body = await read_json(request)
    s = validate_schedule(body)
    s.pop("id", None)
    saved = svc.store.save_schedule(s)
    _service(svc).reload()
    return json_response(_service(svc).find(saved["id"]), status=201)


async def _update(svc, request: web.Request) -> web.Response:
    sid = request.match_info["id"]
    if sid.startswith("label:"):
        raise errors.Validation("label schedules are read-only: edit the container labels or add a schedule to override")
    try:
        existing = svc.store.get_schedule(sid)
    except NotFoundError:
        raise errors.NotFound("schedule not found")
    body = await read_json(request)
    s = validate_schedule(body, existing)
    s["id"] = sid
    if (s["cron"], s["timezone"]) != (existing["cron"], existing["timezone"]) or (s["enabled"] and not existing["enabled"]):
        # A new schedule shape starts fresh: no "missed" report for old slots.
        s["lastRunAt"] = ""
    svc.store.save_schedule(s)
    _service(svc).reload()
    return json_response(_service(svc).find(sid))


async def _delete(svc, request: web.Request) -> web.Response:
    sid = request.match_info["id"]
    if sid.startswith("label:"):
        raise errors.Validation("label schedules are read-only")
    try:
        svc.store.delete_schedule(sid)
    except NotFoundError:
        raise errors.NotFound("schedule not found")
    _service(svc).reload()
    return json_response({"status": "deleted"})


async def _run_now(svc, request: web.Request) -> web.Response:
    s = _service(svc).find(request.match_info["id"])
    run = await _service(svc).execute(s, trigger="manual")
    return json_response(run)


async def _runs(svc, request: web.Request) -> web.Response:
    try:
        limit = int(request.query.get("limit", "100"))
    except ValueError:
        raise errors.Validation("limit must be an integer")
    return json_response(svc.store.list_schedule_runs(request.query.get("scheduleId", ""), limit))


async def _preview(svc, request: web.Request) -> web.Response:
    body = await read_json(request)
    if not isinstance(body, dict):
        raise errors.Validation("invalid request body")
    try:
        count = int(body.get("count", 5))
    except (TypeError, ValueError):
        raise errors.Validation("count must be an integer")
    runs = _service(svc).preview(str(body.get("cron", "")), str(body.get("timezone", "") or ""), count)
    return json_response({"nextRuns": runs})


async def _rescan(svc, request: web.Request) -> web.Response:
    await _service(svc).scan_labels()
    return json_response({"status": "ok", "labelSchedules": len(_service(svc).label_schedules())})


def register(app: web.Application, svc) -> None:
    service = ScheduleService(svc)
    svc.attach("schedules", service)
    h = bind_svc(svc)
    op = require_role("operator")

    api_route(app, "GET", "/schedules", h(_list))
    api_route(app, "GET", "/schedules/runs", h(_runs))
    api_route(app, "POST", "/schedules/preview", h(_preview))
    api_route(app, "POST", "/schedules/rescan", op(h(_rescan)))
    api_route(app, "POST", "/schedules", op(h(_create)))
    api_route(app, "GET", "/schedules/{id}", h(_get))
    api_route(app, "PUT", "/schedules/{id}", op(h(_update)))
    api_route(app, "DELETE", "/schedules/{id}", op(h(_delete)))
    api_route(app, "POST", "/schedules/{id}/run", op(h(_run_now)))

    async def _on_startup(_app: web.Application) -> None:
        try:
            await service.start()
        except Exception as err:  # noqa: BLE001 - never block startup
            log.error("schedules: not started: %s", err)

    async def _on_cleanup(_app: web.Application) -> None:
        await service.stop()

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
