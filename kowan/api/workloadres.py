"""Resource history of a workload.

Routes (under /api/v1):
  GET /resource-history/{workload}?hours=24   CPU and memory over time (1..720 h)
                                                 plus the current memory trend

``workload`` is an app id (dep~… / stack~… / ctr~…). The monitor writes the
history (kowan/monitor/workloads.py); ranges over two days come back hourly.
Scoped principals need read access to the workload's containers.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aiohttp import web

from kowan.auth import authorize as authz
from kowan.errors import Forbidden, Validation
from kowan.monitor.workloads import growth_of
from kowan.web import api_route, json_response
from kowan.web.app import SERVICES_KEY
from kowan.workloads import parse_workload

MAX_HOURS = 720


def register(app: web.Application, svc) -> None:
    api_route(app, "GET", "/resource-history/{workload}", _resources)


def _may_read(kind: str, rest: str, hosts: dict) -> bool:
    ctx = authz.current()
    if ctx is None or ctx.unrestricted:
        return True
    if kind == "dep" and ctx.allowed(authz.LEVEL_READ, authz.Resource(authz.KIND_DEPLOYMENT, id=rest)):
        return True
    return any(
        ctx.allowed(authz.LEVEL_READ, authz.Resource(authz.KIND_CONTAINER, host_id=h, labels=labels))
        for h, labels in hosts.items()
    )


async def _resources(request: web.Request) -> web.Response:
    svc = request.app[SERVICES_KEY]
    workload = request.match_info["workload"]
    try:
        kind, _, rest = parse_workload(workload)
    except ValueError as err:
        raise Validation(str(err)) from None
    try:
        hours = int(request.query.get("hours", "24"))
    except ValueError:
        raise Validation("hours must be a number") from None
    if not 1 <= hours <= MAX_HOURS:
        raise Validation(f"hours must be between 1 and {MAX_HOURS}")
    hosts = svc.store.workload_hosts(workload)
    if not _may_read(kind, rest, hosts):
        raise Forbidden("insufficient permissions")
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return json_response({
        "hours": hours,
        "points": svc.store.list_workload_metrics(workload, since, hours),
        "growth": growth_of(svc.store, workload, sorted(hosts)),
    })
