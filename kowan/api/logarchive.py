"""Retained logs: which workloads keep their logs, and searching them.

Routes (under /api/v1):
  GET    /log-archive/retention               workloads that keep logs (+ line counts)
  PUT    /log-archive/retention/{workload}    {days} start keeping / change (operator)
  DELETE /log-archive/retention/{workload}    stop keeping and delete what is stored (operator)
  GET    /log-archive/search                  q, workload (repeatable), host, since, until, before, limit

The archive lives in ``<data_dir>/logs.db``; the collector starts with the app.
Scoped principals only find lines of the containers they may read.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from aiohttp import web

from kowan.auth import authorize as authz
from kowan.auth import require_role
from kowan.auth.middleware import user_from
from kowan.errors import Validation
from kowan.logstore.collector import Collector
from kowan.logstore.store import LogStore, normalize_ts
from kowan.workloads import parse_workload
from kowan.web import api_route, json_response, read_json, reject_unknown_fields
from kowan.web.app import SERVICES_KEY

_COLLECTOR_KEY = web.AppKey("log_collector", object)

MAX_DAYS = 365


def register(app: web.Application, svc) -> None:
    logs = LogStore(os.path.join(svc.config.data_dir, "logs.db"))
    collector = Collector(svc, logs)
    svc.attach("logs", logs)
    svc.attach("log_collector", collector)

    op = require_role("operator")
    api_route(app, "GET", "/log-archive/retention", _list_retention)
    api_route(app, "PUT", "/log-archive/retention/{workload}", op(_set_retention))
    api_route(app, "DELETE", "/log-archive/retention/{workload}", op(_delete_retention))
    api_route(app, "GET", "/log-archive/search", _search)

    async def _start(_app: web.Application) -> None:
        collector.start()
        _app[_COLLECTOR_KEY] = collector

    async def _stop(_app: web.Application) -> None:
        await collector.stop()
        logs.close()

    app.on_startup.append(_start)
    app.on_cleanup.append(_stop)


def _svc(request: web.Request):
    return request.app[SERVICES_KEY]


def _check_workload(workload: str) -> None:
    try:
        parse_workload(workload)
    except ValueError as err:
        raise Validation(str(err)) from None


async def _list_retention(request: web.Request) -> web.Response:
    return json_response(_svc(request).logs.list_retention())


async def _set_retention(request: web.Request) -> web.Response:
    svc = _svc(request)
    workload = request.match_info["workload"]
    _check_workload(workload)
    body = await read_json(request)
    if not isinstance(body, dict):
        raise Validation("invalid request body")
    reject_unknown_fields(body, {"days"})
    try:
        days = int(body.get("days"))
    except (TypeError, ValueError):
        raise Validation("days must be a number") from None
    if not 1 <= days <= MAX_DAYS:
        raise Validation(f"days must be between 1 and {MAX_DAYS}")
    u = user_from(request)
    svc.logs.set_retention(workload, days, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                           getattr(u, "username", "") if u else "")
    await svc.log_collector.sync()  # start following right away
    return json_response(next(r for r in svc.logs.list_retention() if r["workload"] == workload))


async def _delete_retention(request: web.Request) -> web.Response:
    svc = _svc(request)
    workload = request.match_info["workload"]
    _check_workload(workload)
    removed = svc.logs.delete_retention(workload)
    await svc.log_collector.sync()  # stop its followers
    return json_response({"removed": removed})


def _time(value: str, label: str) -> str:
    if not value:
        return ""
    try:
        return normalize_ts(value)
    except ValueError:
        raise Validation(f"{label} must be an RFC3339 time") from None


def _visible(item: Dict[str, Any]) -> bool:
    """Read access to the container a line came from (team scoping)."""
    ctx = authz.current()
    if ctx is None or ctx.unrestricted:
        return True
    labels = dict(item.pop("labels", None) or {})
    if item["stack"]:
        labels[authz.COMPOSE_PROJECT_LABEL] = item["stack"]
    if item["deploymentId"]:
        labels[authz.DEPLOYMENT_ID_LABEL] = item["deploymentId"]
    res = authz.Resource(authz.KIND_CONTAINER, host_id=item["hostId"], id=item["containerId"], labels=labels)
    return ctx.allowed(authz.LEVEL_READ, res)


async def _search(request: web.Request) -> web.Response:
    """Newest first. ``next`` is the ``before`` to pass for the next page."""
    svc = _svc(request)
    q = request.query
    try:
        limit = max(1, min(int(q.get("limit", "200")), 1000))
        before = int(q.get("before", "0") or 0)
    except ValueError:
        raise Validation("limit and before must be numbers") from None
    hours = q.get("hours", "")
    since = _time(q.get("since", ""), "since")
    if hours and not since:
        try:
            h = max(1, int(hours))
        except ValueError:
            raise Validation("hours must be a number") from None
        since = normalize_ts((datetime.now(timezone.utc) - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    until = _time(q.get("until", ""), "until")
    workloads: List[str] = q.getall("workload", [])
    for w in workloads:
        _check_workload(w)
    items: List[Dict[str, Any]] = []
    cursor = before
    # Scoped principals can lose rows to the filter: read on until the page is
    # full or the archive is exhausted.
    while len(items) < limit:
        batch = svc.logs.search(text=q.get("q", ""), workloads=workloads or None, host_id=q.get("host", ""),
                                since=since, until=until, before=cursor, limit=limit)
        if not batch:
            cursor = 0
            break
        items.extend(i for i in batch if _visible(i))
        for i in batch:
            i.pop("labels", None)
        cursor = batch[-1]["id"]
        if len(batch) < limit:
            cursor = 0
            break
    items = items[:limit]
    hosts = {h.id: h.name for h in svc.store.list_hosts()}
    for i in items:
        i["hostName"] = hosts.get(i["hostId"], i["hostId"])
    nxt = items[-1]["id"] if len(items) == limit and cursor else None
    return json_response({"items": items, "next": nxt})
