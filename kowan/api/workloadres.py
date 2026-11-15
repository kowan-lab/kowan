"""Resource history of a workload.

Routes (under /api/v1):
  GET /resource-history/{workload}?hours=24   CPU and memory over time (1..720 h)
                                                 plus the current memory trend
  GET /resource-history/{workload}/containers  the same per container (7 days)
  GET /resource-history/host/{hostID}          what each workload of a host uses:
                                                 averages, peaks, now, and the
                                                 series of the biggest ones

``workload`` is an app id (dep~… / stack~… / ctr~…). The monitor writes the
history (kowan/monitor/workloads.py); ranges over two days come back hourly.
Scoped principals need read access to the workload's containers.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from aiohttp import web

from kowan.auth import authorize as authz
from kowan.errors import Forbidden, Validation
from kowan.monitor.workloads import growth_of
from kowan.web import api_route, json_response
from kowan.web.app import SERVICES_KEY
from kowan.workloads import parse_workload

MAX_HOURS = 720


def register(app: web.Application, svc) -> None:
    api_route(app, "GET", "/resource-history/host/{hostID}", _host)
    api_route(app, "GET", "/resource-history/{workload}", _resources)
    api_route(app, "GET", "/resource-history/{workload}/containers", _containers)


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


def _hours(request: web.Request) -> int:
    try:
        hours = int(request.query.get("hours", "24"))
    except ValueError:
        raise Validation("hours must be a number") from None
    if not 1 <= hours <= MAX_HOURS:
        raise Validation(f"hours must be between 1 and {MAX_HOURS}")
    return hours


def _since(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _readable(svc, workload: str) -> None:
    try:
        kind, _, rest = parse_workload(workload)
    except ValueError as err:
        raise Validation(str(err)) from None
    if not _may_read(kind, rest, svc.store.workload_hosts(workload)):
        raise Forbidden("insufficient permissions")


async def _containers(request: web.Request) -> web.Response:
    svc = request.app[SERVICES_KEY]
    workload = request.match_info["workload"]
    _readable(svc, workload)
    hours = _hours(request)
    return json_response({"hours": hours, "members": svc.store.list_container_metrics(workload, _since(hours), hours)})


def breakdown(rows: List[Dict[str, Any]], top: int) -> Dict[str, Any]:
    """Per-workload figures over the range and the series of the biggest
    ones. A bucket where a workload did not run counts as 0, so averages
    compare like for like; "other" is everything outside the series."""
    times = sorted({r["t"] for r in rows})
    at = {t: i for i, t in enumerate(times)}
    n = len(times)
    per: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        w = per.setdefault(r["workload"], {"cpu": [0.0] * n, "mem": [0] * n, "cpuMax": 0.0, "memMax": 0, "containers": 0})
        i = at[r["t"]]
        w["cpu"][i], w["mem"][i] = float(r["cpu"]), int(r["mem"])
        w["cpuMax"] = max(w["cpuMax"], float(r["cpu_max"]))
        w["memMax"] = max(w["memMax"], int(r["mem_max"]))
        if i == n - 1:
            w["containers"] = int(r["containers"])
    workloads = [
        {"workload": wid, "cpu": round(sum(w["cpu"]) / n, 2), "cpuMax": round(w["cpuMax"], 2),
         "mem": int(sum(w["mem"]) / n), "memMax": w["memMax"], "cpuNow": round(w["cpu"][-1], 2),
         "memNow": w["mem"][-1], "containers": w["containers"]}
        for wid, w in per.items()
    ]
    workloads.sort(key=lambda w: (-w["mem"], -w["cpu"]))
    chosen = [w["workload"] for w in workloads[:top]]
    for w in sorted(workloads, key=lambda w: -w["cpu"])[:top]:
        if w["workload"] not in chosen:
            chosen.append(w["workload"])
    other_cpu = [round(sum(per[wid]["cpu"][i] for wid in per if wid not in chosen), 2) for i in range(n)]
    other_mem = [sum(per[wid]["mem"][i] for wid in per if wid not in chosen) for i in range(n)]
    return {
        "times": times,
        "workloads": workloads,
        "series": [{"workload": wid, "cpu": [round(v, 2) for v in per[wid]["cpu"]], "mem": per[wid]["mem"]} for wid in chosen],
        "other": {"cpu": other_cpu, "mem": other_mem},
    }


async def _host(request: web.Request) -> web.Response:
    svc = request.app[SERVICES_KEY]
    host_id = request.match_info["hostID"]
    hours = _hours(request)
    try:
        top = max(1, min(20, int(request.query.get("top", "6"))))
    except ValueError:
        raise Validation("top must be a number") from None
    rows = svc.store.host_workload_usage(host_id, _since(hours), hours)
    ctx = authz.current()
    if ctx is not None and not ctx.unrestricted:
        labels = svc.store.host_workload_labels(host_id)
        ok = set()
        for wid in {r["workload"] for r in rows}:
            try:
                kind, _, rest = parse_workload(wid)
            except ValueError:
                continue
            if _may_read(kind, rest, {host_id: labels.get(wid, {})}):
                ok.add(wid)
        rows = [r for r in rows if r["workload"] in ok]
    return json_response({"hours": hours, "hostId": host_id, **breakdown(rows, top)})


async def _resources(request: web.Request) -> web.Response:
    svc = request.app[SERVICES_KEY]
    workload = request.match_info["workload"]
    _readable(svc, workload)
    hours = _hours(request)
    hosts = svc.store.workload_hosts(workload)
    since = _since(hours)
    return json_response({
        "hours": hours,
        "points": svc.store.list_workload_metrics(workload, since, hours),
        "growth": growth_of(svc.store, workload, sorted(hosts)),
    })
