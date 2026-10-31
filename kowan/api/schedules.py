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
from kowan.auth import authorize as authz
from kowan.schedules.service import ScheduleService, validate_schedule
from kowan.store import NotFoundError
from kowan.web import api_route, bind_svc, json_response, read_json, require_role

log = logging.getLogger("kowan.api.schedules")


def _service(svc) -> ScheduleService:
    return svc.schedules


def _targets(s):
    """A schedule's (or a run's) target, for team/grant scoping. A runbook's
    targets are the workloads it touches (a gone one: fleet-wide only)."""
    if s.get("targetType") == "runbook":
        return _runbook_targets(s.get("targetId", ""))
    if s.get("targetType") == "group":
        hosts = _STORE[0].hosts_with_tag(s.get("targetId", "")) if _STORE else []
        return [("host", h, "") for h in hosts] or [("deployment", "", "")]
    return [(s.get("targetType", ""), s.get("hostId", ""), s.get("targetId", ""))]


# The store, for a runbook schedule's workloads (set by register).
_STORE: list = []


def _runbook_targets(rid: str):
    from kowan.runbooks import model

    parsed = model.parse_label_id(rid)
    try:
        if parsed is not None:
            return [model.access_target(parsed[0])]
        return model.runbook_targets(_STORE[0].get_runbook(rid), _STORE[0])
    except Exception:  # noqa: BLE001
        return [("deployment", "", "")]


async def _find(svc, sid: str) -> dict:
    """The schedule, or 404 when the caller's grants do not cover its target."""
    s = _service(svc).find(sid)
    if not await authz.TargetAccess(svc.store, svc.docker).allowed(_targets(s)[0]):
        raise errors.NotFound("schedule not found")
    return s


async def _list(svc, request: web.Request) -> web.Response:
    q = request.query
    items = _service(svc).all_schedules()
    for key in ("targetType", "targetId", "hostId"):
        v = q.get(key, "")
        if v:
            items = [s for s in items if s.get(key) == v]
    return json_response(await authz.TargetAccess(svc.store, svc.docker).filter(items, _targets))


async def _get(svc, request: web.Request) -> web.Response:
    return json_response(await _find(svc, request.match_info["id"]))


async def _create(svc, request: web.Request) -> web.Response:
    body = await read_json(request)
    s = validate_schedule(body)
    s.pop("id", None)
    # A schedule starts and stops its target from the background runner, with
    # no principal: it may only point at what the caller can operate.
    await authz.TargetAccess(svc.store, svc.docker).check(_targets(s), authz.LEVEL_OPERATE)
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
    access = authz.TargetAccess(svc.store, svc.docker)
    if not await access.allowed(_targets(existing)[0]):
        raise errors.NotFound("schedule not found")
    body = await read_json(request)
    s = validate_schedule(body, existing)
    s["id"] = sid
    await access.check(_targets(existing) + _targets(s), authz.LEVEL_OPERATE)
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
        existing = svc.store.get_schedule(sid)
    except NotFoundError:
        raise errors.NotFound("schedule not found")
    access = authz.TargetAccess(svc.store, svc.docker)
    if not await access.allowed(_targets(existing)[0]):
        raise errors.NotFound("schedule not found")
    await access.check(_targets(existing), authz.LEVEL_OPERATE)
    try:
        svc.store.delete_schedule(sid)
    except NotFoundError:
        raise errors.NotFound("schedule not found")
    _service(svc).reload()
    return json_response({"status": "deleted"})


async def _run_now(svc, request: web.Request) -> web.Response:
    s = await _find(svc, request.match_info["id"])
    await authz.TargetAccess(svc.store, svc.docker).check(_targets(s), authz.LEVEL_OPERATE)
    run = await _service(svc).execute(s, trigger="manual")
    return json_response(run)


async def _runs(svc, request: web.Request) -> web.Response:
    try:
        limit = int(request.query.get("limit", "100"))
    except ValueError:
        raise errors.Validation("limit must be an integer")
    runs = svc.store.list_schedule_runs(request.query.get("scheduleId", ""), limit)
    return json_response(await authz.TargetAccess(svc.store, svc.docker).filter(runs, _targets))


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
    # The scan replaces each host's label schedules: it must see every
    # container, not only the caller's (lists are filtered per caller).
    token = authz.bind(None)
    try:
        await _service(svc).scan_labels()
    finally:
        authz.unbind(token)
    return json_response({"status": "ok", "labelSchedules": len(_service(svc).label_schedules())})


def register(app: web.Application, svc) -> None:
    service = ScheduleService(svc)
    svc.attach("schedules", service)
    _STORE[:] = [svc.store]
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
