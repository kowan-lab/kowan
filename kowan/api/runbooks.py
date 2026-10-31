"""Runbooks HTTP API (see docs/runbooks.md and kowan/runbooks).

Routes (under /api/v1):

    GET    /runbooks                    stored + label runbooks (?workload= those touching it)
    POST   /runbooks                    create (operator; manage on every workload it touches)
    GET    /runbooks/suggestions        suggested runbooks for ?workload=, from its images
    GET    /runbooks/{id}               one runbook, with its last run
    PUT    /runbooks/{id}               update (label runbooks are read-only)
    DELETE /runbooks/{id}               delete, with its runs
    POST   /runbooks/{id}/run           start a run {params, confirm} -> 202 run (operate)
    GET    /runbooks/{id}/runs          its runs, newest first (?limit)
    GET    /runbook-runs/{id}           one run with every step's output
    GET    /runbook-runs/{id}/ws        follow a run live: every event so far, then the
                                        new ones; JSON messages {type: step|output|done}
    GET    /alert-rules/{id}/runbook    the runbook an alert rule runs when it fires (or null)
    PUT    /alert-rules/{id}/runbook    {runbookId, cooldownMinutes} (operator; operate on it)
    DELETE /alert-rules/{id}/runbook    stop running one

Who may do what: reading needs read on every workload a runbook touches,
running needs operate, defining or changing one needs manage, the same
levels as doing those things to the workloads by hand. A run is checked once,
when it starts (see kowan/runbooks/service.py).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

from aiohttp import WSMsgType, web

from kowan import errors, jsonx
from kowan.auth import authorize as authz
from kowan.runbooks import RunbookError, RunbookService
from kowan.runbooks import model
from kowan.runbooks.alerts import AlertRunbooks
from kowan.store import NotFoundError
from kowan.web import api_route, bind_svc, json_response, read_json, require_role

from ._dockerws import prepare_ws

log = logging.getLogger("kowan.api.runbooks")


def _service(svc) -> RunbookService:
    return svc.runbooks


# The store, for the hosts of a group a step targets (set by register).
_STORE: list = []


def _targets(rb: Dict[str, Any]) -> List[Any]:
    return model.runbook_targets(rb, _STORE[0])


def _run_targets(run: Dict[str, Any]) -> List[Any]:
    out: List[Any] = []
    for w in run.get("workloads") or []:
        out += model.access_targets(w, _STORE[0])
    return out


def _access(svc) -> authz.TargetAccess:
    return authz.TargetAccess(svc.store, svc.docker)


def _user(request: web.Request) -> str:
    ctx = authz.current()
    user = getattr(ctx, "user", None) if ctx is not None else None
    return str(getattr(user, "username", "") or "")


def _validate(body: Any) -> Dict[str, Any]:
    try:
        return model.validate_runbook(body)
    except RunbookError as err:
        raise errors.Validation(str(err)) from None


async def _find(svc, rid: str, level: str = authz.LEVEL_READ) -> Dict[str, Any]:
    """The runbook, or 404 when the caller may not read every workload it
    touches (and 403 when they may read but not ``level``)."""
    try:
        rb = await _service(svc).get(rid)
    except (NotFoundError, ValueError):
        raise errors.NotFound("runbook not found") from None
    access = _access(svc)
    if not await access.allowed_all(_targets(rb), authz.LEVEL_READ):
        raise errors.NotFound("runbook not found")
    if level != authz.LEVEL_READ:
        await access.check(_targets(rb), level)
    return rb


def _with_last(svc, rb: Dict[str, Any]) -> Dict[str, Any]:
    runs = svc.store.list_runbook_runs(rb["id"], limit=1)
    last = runs[0] if runs else None
    return {**rb, "workloads": model.workloads_of(rb),
            "lastRun": {k: last[k] for k in ("id", "status", "startedAt", "finishedAt", "user", "trigger")} if last else None}


async def _list(svc, request: web.Request) -> web.Response:
    workload = request.query.get("workload", "")
    hosts = None
    if workload:
        try:
            hosts = _service(svc)._workload_hosts(workload)
        except ValueError as err:
            raise errors.Validation(str(err)) from None
    items = svc.store.list_runbooks() + await _service(svc).labelled(hosts)
    if workload:
        items = [rb for rb in items if workload in model.workloads_of(rb)]
    items = await _access(svc).filter(items, _targets)
    items.sort(key=lambda rb: rb["name"].lower())
    return json_response([_with_last(svc, rb) for rb in items])


async def _get(svc, request: web.Request) -> web.Response:
    return json_response(_with_last(svc, await _find(svc, request.match_info["id"])))


async def _create(svc, request: web.Request) -> web.Response:
    rb = _validate(await read_json(request))
    await _access(svc).check(_targets(rb), authz.LEVEL_MANAGE)
    rb["createdBy"] = _user(request)
    return json_response(_with_last(svc, svc.store.save_runbook(rb)), status=201)


def _not_labels(rid: str) -> None:
    if model.parse_label_id(rid) is not None:
        raise errors.Validation("this runbook is declared with container labels: change them in its compose file")


async def _update(svc, request: web.Request) -> web.Response:
    rid = request.match_info["id"]
    _not_labels(rid)
    await _find(svc, rid, authz.LEVEL_MANAGE)
    rb = _validate(await read_json(request))
    await _access(svc).check(_targets(rb), authz.LEVEL_MANAGE)
    rb["id"] = rid
    return json_response(_with_last(svc, svc.store.save_runbook(rb)))


async def _delete(svc, request: web.Request) -> web.Response:
    rid = request.match_info["id"]
    _not_labels(rid)
    await _find(svc, rid, authz.LEVEL_MANAGE)
    svc.store.delete_runbook(rid)
    return json_response({"status": "deleted"})


async def _run(svc, request: web.Request) -> web.Response:
    rb = await _find(svc, request.match_info["id"], authz.LEVEL_OPERATE)
    body = (await read_json(request) or {}) if request.can_read_body else {}
    if not isinstance(body, dict):
        raise errors.Validation("invalid request body")
    extra = sorted(set(body) - {"params", "confirm"})
    if extra:
        raise errors.Validation(f"unknown field {extra[0]!r}")
    if rb.get("confirm") and body.get("confirm") is not True:
        raise errors.Validation("this runbook asks for confirmation: send confirm: true")
    try:
        run = _service(svc).start(rb, body.get("params"), _user(request), "manual")
    except RunbookError as err:
        status = 409 if "already running" in str(err) else 400
        return json_response({"error": str(err)}, status=status)
    return json_response(run, status=202)


async def _runs(svc, request: web.Request) -> web.Response:
    rb = await _find(svc, request.match_info["id"])
    try:
        limit = int(request.query.get("limit", "50"))
    except ValueError:
        raise errors.Validation("limit must be a number") from None
    runs = svc.store.list_runbook_runs(rb["id"], limit)
    for r in runs:
        r["steps"] = [{k: v for k, v in s.items() if k != "output"} for s in r["steps"]]
    return json_response(runs)


async def _find_run(svc, run_id: str) -> Dict[str, Any]:
    try:
        run = svc.store.get_runbook_run(run_id)
    except NotFoundError:
        raise errors.NotFound("run not found") from None
    if not await _access(svc).allowed_all(_run_targets(run), authz.LEVEL_READ):
        raise errors.NotFound("run not found")
    live = _service(svc).live(run_id)
    return live.run if live is not None and not live.done.is_set() else run


async def _get_run(svc, request: web.Request) -> web.Response:
    return json_response(await _find_run(svc, request.match_info["id"]))


async def _follow_run(svc, request: web.Request) -> web.StreamResponse:
    run = await _find_run(svc, request.match_info["id"])
    ws = await prepare_ws(request)
    live = _service(svc).live(run["id"])
    if live is None:
        # Ended a while ago (or before a restart): replay it from the store.
        for i, rec in enumerate(run["steps"]):
            await ws.send_str(jsonx.dumps({"type": "step", "record": i,
                                           "data": {k: v for k, v in rec.items() if k != "output"}}).decode())
            if rec.get("output"):
                await ws.send_str(jsonx.dumps({"type": "output", "record": i, "text": rec["output"]}).decode())
        await ws.send_str(jsonx.dumps({"type": "done", "run": {**run, "steps": [
            {k: v for k, v in s.items() if k != "output"} for s in run["steps"]]}}).decode())
        await ws.close()
        return ws
    q = live.follow()

    async def reader() -> None:
        async for msg in ws:
            if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.ERROR):
                break

    gone = asyncio.ensure_future(reader())
    try:
        while not ws.closed:
            get = asyncio.ensure_future(q.get())
            done, _ = await asyncio.wait({get, gone}, return_when=asyncio.FIRST_COMPLETED)
            if gone in done:
                get.cancel()
                break
            ev = get.result()
            if ev is None:
                break
            await ws.send_str(jsonx.dumps(ev).decode())
        if not ws.closed:
            await ws.close()
    except (ConnectionResetError, RuntimeError):
        pass
    finally:
        live.unfollow(q)
        gone.cancel()
    return ws


async def _suggestions(svc, request: web.Request) -> web.Response:
    workload = request.query.get("workload", "")
    try:
        target = model.access_target(workload)
    except (ValueError, KeyError):
        raise errors.Validation("workload must be dep~<id>, stack~<host>~<project> or ctr~<host>~<name>") from None
    if not await _access(svc).allowed(target, authz.LEVEL_READ):
        raise errors.NotFound("workload not found")
    services = await _service(svc).services(workload)
    return json_response(model.suggestions(services, workload))


def _rule(svc, rule_id: str) -> Any:
    try:
        return svc.store.get_alert_rule(rule_id)
    except Exception:  # noqa: BLE001
        raise errors.NotFound("alert rule not found") from None


async def _get_rule_runbook(svc, request: web.Request) -> web.Response:
    _rule(svc, request.match_info["id"])
    return json_response(svc.store.get_rule_runbook(request.match_info["id"]))


async def _set_rule_runbook(svc, request: web.Request) -> web.Response:
    rule_id = request.match_info["id"]
    _rule(svc, rule_id)
    body = await read_json(request)
    if not isinstance(body, dict):
        raise errors.Validation("invalid request body")
    extra = sorted(set(body) - {"runbookId", "cooldownMinutes"})
    if extra:
        raise errors.Validation(f"unknown field {extra[0]!r}")
    cooldown = body.get("cooldownMinutes", 10)
    if isinstance(cooldown, bool) or not isinstance(cooldown, int) or not 0 <= cooldown <= 1440:
        raise errors.Validation("cooldownMinutes must be between 0 and 1440")
    # The alert starts it with no one behind it: only someone who could run
    # it by hand may hand it to an alert.
    rb = await _find(svc, str(body.get("runbookId") or ""), authz.LEVEL_OPERATE)
    try:
        model.resolve_params(rb, {})
    except RunbookError as err:
        raise errors.Validation(f"an alert runs it with the defaults: {err}") from None
    return json_response(svc.store.set_rule_runbook(rule_id, rb["id"], cooldown))


async def _delete_rule_runbook(svc, request: web.Request) -> web.Response:
    _rule(svc, request.match_info["id"])
    svc.store.delete_rule_runbook(request.match_info["id"])
    return json_response({"status": "deleted"})


def register(app: web.Application, svc) -> None:
    service = RunbookService(svc)
    svc.attach("runbooks", service)
    _STORE[:] = [svc.store]
    try:
        svc.store.fail_unfinished_runbook_runs()
    except Exception as err:  # noqa: BLE001
        log.error("runbooks: close unfinished runs: %s", err)
    h = bind_svc(svc)
    op = require_role("operator")

    api_route(app, "GET", "/runbooks", h(_list))
    api_route(app, "POST", "/runbooks", op(h(_create)))
    api_route(app, "GET", "/runbooks/suggestions", h(_suggestions))
    api_route(app, "GET", "/runbooks/{id}", h(_get))
    api_route(app, "PUT", "/runbooks/{id}", op(h(_update)))
    api_route(app, "DELETE", "/runbooks/{id}", op(h(_delete)))
    api_route(app, "POST", "/runbooks/{id}/run", op(h(_run)))
    api_route(app, "GET", "/runbooks/{id}/runs", h(_runs))
    api_route(app, "GET", "/runbook-runs/{id}", h(_get_run))
    api_route(app, "GET", "/runbook-runs/{id}/ws", h(_follow_run))
    api_route(app, "GET", "/alert-rules/{id}/runbook", h(_get_rule_runbook))
    api_route(app, "PUT", "/alert-rules/{id}/runbook", op(h(_set_rule_runbook)))
    api_route(app, "DELETE", "/alert-rules/{id}/runbook", op(h(_delete_rule_runbook)))

    alerts = AlertRunbooks(svc)
    svc.attach("alert_runbooks", alerts)

    async def _on_startup(_app: web.Application) -> None:
        alerts.start()

    async def _on_cleanup(_app: web.Application) -> None:
        await alerts.stop()
        await service.stop()

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
