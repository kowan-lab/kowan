"""Workload tools: containers grouped the way the UI groups them (a
deployment, a compose project or swarm stack, a lone container), their
resource history and memory trend, the retained log archive, and lifecycle
actions on a whole workload. Ids are the UI's (kowan/workloads.py):
``dep~<id>``, ``stack~<host>~<project>``, ``ctr~<host>~<name>``."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from ...auth import authorize as authz
from ...workloads import compose_deployments, parse_workload, workload_of
from ..registry import ROLE_OPERATOR, ToolError, ToolRegistry
from ._common import (
    HOST_PROP,
    OFFSET_PROP,
    container_name,
    exit_code_from_status,
    human_bytes,
    iso,
    limit_prop,
    ops_for_host,
    page,
    parse_since,
    resolve_host,
    short_id,
    with_timeout,
)
from .fleet import _host_names, resolve_deployment

ACTION_TIMEOUT = 120.0
LOG_MAX_LINE = 2000
MAX_POINTS = 96

WORKLOAD_PROP = {
    "type": "string",
    "minLength": 1,
    "description": "Workload id (dep~<id>, stack~<host>~<project>, ctr~<host>~<name>) or its name "
                   "(deployment name, compose project or container name).",
}

_KINDS = {"dep": "deployment", "stack": "stack", "ctr": "container"}


def _problem(c: Any) -> bool:
    code = exit_code_from_status(c.status)
    return (c.state in ("restarting", "dead") or (c.state == "exited" and code not in (None, 0))
            or "(unhealthy)" in (c.status or ""))


def _state(containers: List[Dict[str, Any]]) -> str:
    if not containers:
        return "no containers"
    if any(c["problem"] for c in containers):
        return "needs attention"
    running = sum(1 for c in containers if c["state"] == "running")
    if running == len(containers):
        return "running"
    return "stopped" if running == 0 else "partly running"


async def _host_containers(svc: Any, hosts: List[Any], errors: List[Dict[str, str]]) -> List[tuple]:
    async def one(h):
        try:
            ops = ops_for_host(svc, h)
            return [(h, c) for c in await with_timeout(ops.list_containers(True), f"containers on {h.name}", 15)]
        except Exception as err:  # noqa: BLE001
            errors.append({"host": h.name or h.id, "error": str(err)})
            return []

    out: List[tuple] = []
    for chunk in await asyncio.gather(*(one(h) for h in hosts)):
        out.extend(chunk)
    return out


async def build_workloads(svc: Any, hosts: List[Any]) -> tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """Workloads on ``hosts``: their containers grouped, plus the deployments
    targeting them that have no container right now."""
    errors: List[Dict[str, str]] = []
    pairs = await _host_containers(svc, hosts, errors)
    compose_deps = compose_deployments(svc.store)
    deps = {d.id: d for d in authz.filter_deployments(svc.store.list_deployments())}
    names = _host_names(svc)
    by_id: Dict[str, Dict[str, Any]] = {}
    for h, c in pairs:
        cname = container_name(c)
        wid, stack, dep_id = workload_of(c.labels or {}, h.id, cname, compose_deps)
        w = by_id.get(wid)
        if w is None:
            kind = wid.split("~", 1)[0]
            d = deps.get(dep_id)
            w = by_id[wid] = {
                "id": wid, "kind": _KINDS[kind],
                "name": d.name if d is not None else (stack or cname),
                "hosts": [], "containers": [],
            }
            if dep_id:
                w["deploymentId"] = dep_id
                if d is not None:
                    w["deploymentStatus"] = d.status
            if stack:
                w["project"] = stack
        if h.name not in w["hosts"]:
            w["hosts"].append(h.name)
        w["containers"].append({
            "id": short_id(c.id), "name": cname, "image": c.image, "state": c.state, "status": c.status,
            "host": h.name, "problem": _problem(c),
        })
    host_ids = {h.id for h in hosts}
    for d in deps.values():
        wid = f"dep~{d.id}"
        if wid in by_id or not host_ids.intersection(d.targets):
            continue
        by_id[wid] = {"id": wid, "kind": "deployment", "name": d.name, "deploymentId": d.id,
                      "deploymentStatus": d.status, "hosts": [names.get(t, t) for t in d.targets], "containers": []}
    rows = list(by_id.values())
    for w in rows:
        w["state"] = _state(w["containers"])
        w["running"] = sum(1 for c in w["containers"] if c["state"] == "running")
        w["total"] = len(w["containers"])
    order = {"needs attention": 0, "partly running": 1, "running": 2, "stopped": 3, "no containers": 4}
    rows.sort(key=lambda w: (order[w["state"]], w["name"].lower()))
    return rows, errors


def _scope_hosts(svc: Any, args: Dict[str, Any]) -> List[Any]:
    if args.get("host"):
        return [resolve_host(svc, args["host"])]
    return authz.filter_hosts(svc.store.list_hosts())


async def resolve_workload(svc: Any, ref: str, host: Optional[str] = None) -> Dict[str, Any]:
    """A workload by id or by name, with its containers."""
    hosts = [resolve_host(svc, host)] if host else authz.filter_hosts(svc.store.list_hosts())
    try:
        kind, host_id, rest = parse_workload(ref)
    except ValueError:
        kind = ""
    if kind == "dep":
        d = resolve_deployment(svc, rest)
        hosts = [h for h in authz.filter_hosts(svc.store.list_hosts()) if h.id in d.targets]
    elif kind:
        hosts = [resolve_host(svc, host_id)]
    rows, errors = await build_workloads(svc, hosts)
    if kind:
        for w in rows:
            if w["id"] == ref:
                return w
        if errors:
            raise ToolError(errors[0]["error"])
        raise ToolError(f"workload {ref!r} not found; call list_workloads")
    matches = [w for w in rows if w["name"].lower() == ref.lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ToolError(f"workload name {ref!r} is ambiguous; use one of: {', '.join(w['id'] for w in matches)}")
    raise ToolError(f"workload {ref!r} not found; call list_workloads")


def _downsample(points: List[Dict[str, Any]], n: int = MAX_POINTS) -> List[Dict[str, Any]]:
    if len(points) <= n:
        return points
    step = len(points) / n
    return [points[int(i * step)] for i in range(n)]


def _resource_summary(points: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not points:
        return {}
    cpu = [p.get("cpu") or 0 for p in points]
    mem = [p.get("mem") or 0 for p in points]
    last = points[-1]
    out = {
        "cpuAvgPercent": round(sum(cpu) / len(cpu), 1),
        "cpuMaxPercent": round(max(p.get("cpuMax") or 0 for p in points), 1),
        "memAvg": human_bytes(sum(mem) / len(mem)),
        "memMax": human_bytes(max(p.get("memMax") or 0 for p in points)),
        "memNow": human_bytes(last.get("mem") or 0),
    }
    if last.get("memLimit"):
        out["memLimit"] = human_bytes(last["memLimit"])
    return out


def _growth_view(g: Optional[Dict[str, Any]], names: Dict[str, str]) -> Optional[Dict[str, Any]]:
    if not g:
        return None
    out = {
        "growing": g["growing"], "host": names.get(g["hostId"], g["hostId"]),
        "perHour": human_bytes(g["slopeBytesPerHour"]) if g["slopeBytesPerHour"] > 0 else "0 B",
        "from": human_bytes(g["start"]), "to": human_bytes(g["end"]), "overHours": g["spanHours"],
        "fit": g["r2"], "restarted": g["restarted"],
    }
    if g.get("hoursToLimit") is not None:
        out["hoursToLimit"] = g["hoursToLimit"]
    return out


def register(reg: ToolRegistry) -> None:
    @reg.tool(
        "list_workloads",
        title="List workloads",
        description=(
            "Workloads as the Kowan UI shows them: each deployment, each compose project or swarm stack nobody "
            "deployed through Kowan, and each lone container, with their containers and a state (needs attention, "
            "partly running, running, stopped). Problems first. Filter by host, kind, state or a name substring."
        ),
        schema={"properties": {
            "host": {**HOST_PROP, "description": "Host id or name (default: every host)."},
            "kind": {"type": "string", "enum": ["deployment", "stack", "container"]},
            "state": {"type": "string", "enum": ["needs attention", "partly running", "running", "stopped", "no containers"]},
            "name": {"type": "string", "description": "Case-insensitive substring of the workload name."},
            "limit": limit_prop(50, 300), "offset": OFFSET_PROP,
        }},
    )
    async def list_workloads(ctx, args):
        rows, errors = await build_workloads(ctx.svc, _scope_hosts(ctx.svc, args))
        if args.get("kind"):
            rows = [w for w in rows if w["kind"] == args["kind"]]
        if args.get("state"):
            rows = [w for w in rows if w["state"] == args["state"]]
        if args.get("name"):
            rows = [w for w in rows if args["name"].lower() in w["name"].lower()]
        brief = []
        for w in rows:
            b = {k: v for k, v in w.items() if k != "containers"}
            b["containers"] = [c["name"] for c in w["containers"][:20]]
            brief.append(b)
        out = page(brief, args, 50, 300)
        if errors:
            out["errors"] = errors
        return out

    @reg.tool(
        "get_workload",
        title="Get workload",
        description=(
            "One workload with each of its containers (state, image, host), the deployment it belongs to, whether "
            "its logs are kept, and its memory trend. Use list_workloads to find ids."
        ),
        schema={"properties": {"workload": WORKLOAD_PROP, "host": HOST_PROP}, "required": ["workload"]},
    )
    async def get_workload(ctx, args):
        from ...monitor.workloads import growth_of

        w = await resolve_workload(ctx.svc, args["workload"], args.get("host"))
        logs = getattr(ctx.svc, "logs", None)
        if logs is not None:
            kept = next((r for r in logs.list_retention() if r["workload"] == w["id"]), None)
            w["logRetention"] = {"days": kept["days"]} if kept else None
        try:
            hosts = sorted(ctx.svc.store.workload_hosts(w["id"]))
            w["memoryTrend"] = _growth_view(growth_of(ctx.svc.store, w["id"], hosts), _host_names(ctx.svc))
        except Exception:  # noqa: BLE001 - no history yet
            w["memoryTrend"] = None
        return w

    @reg.tool(
        "workload_action",
        title="Workload lifecycle action",
        description=(
            "Start, stop or restart every container of a workload (a compose stack, a deployment or a lone "
            "container). A deployment is started and stopped through Kowan, so its status follows; use "
            "deployment_action to redeploy it."
        ),
        schema={"properties": {"workload": WORKLOAD_PROP, "host": HOST_PROP,
                               "action": {"type": "string", "enum": ["start", "stop", "restart"]}},
                "required": ["workload", "action"]},
        role=ROLE_OPERATOR, read_only=False, destructive=False, idempotent=False,
    )
    async def workload_action(ctx, args):
        w = await resolve_workload(ctx.svc, args["workload"], args.get("host"))
        action = args["action"]
        dep_svc = getattr(ctx.svc, "deployments", None)
        if w.get("deploymentId") and action in ("start", "stop") and dep_svc is not None:
            d = resolve_deployment(ctx.svc, w["deploymentId"])
            await with_timeout(getattr(dep_svc, action)(d.id), action, 300)
            return {"workload": w["id"], "name": w["name"], "action": action, "via": "deployment",
                    "status": ctx.svc.store.get_deployment(d.id).status}
        if not w["containers"]:
            raise ToolError(f"{w['name']} has no containers to {action}")
        hosts = {h.name: h for h in authz.filter_hosts(ctx.svc.store.list_hosts())}
        op = {"start": "start_container", "stop": "stop_container", "restart": "restart_container"}[action]
        done, failed = [], []
        for c in w["containers"]:
            if action == "start" and c["state"] == "running" or action == "stop" and c["state"] != "running":
                continue
            try:
                ops = ops_for_host(ctx.svc, hosts[c["host"]])
                await with_timeout(getattr(ops, op)(c["id"]), f"{action} {c['name']}", ACTION_TIMEOUT)
                done.append(c["name"])
            except Exception as err:  # noqa: BLE001
                failed.append({"container": c["name"], "error": str(err)})
        if failed and not done:
            raise ToolError("; ".join(f"{f['container']}: {f['error']}" for f in failed))
        return {"workload": w["id"], "name": w["name"], "action": action, "containers": done, "failed": failed}

    @reg.tool(
        "workload_resources",
        title="Workload resource history",
        description=(
            "CPU and memory of a workload over the last hours (1 to 720, default 24): a summary, up to 96 points "
            "and the current memory trend (growing, how fast, hours to the memory limit)."
        ),
        schema={"properties": {"workload": WORKLOAD_PROP, "host": HOST_PROP,
                               "hours": {"type": "integer", "minimum": 1, "maximum": 720}},
                "required": ["workload"]},
    )
    async def workload_resources(ctx, args):
        from ...api.workloadres import _may_read
        from ...monitor.workloads import growth_of

        ref = args["workload"]
        try:
            parse_workload(ref)
            wid = ref
        except ValueError:
            wid = (await resolve_workload(ctx.svc, ref, args.get("host")))["id"]
        kind, _, rest = parse_workload(wid)
        hosts = ctx.svc.store.workload_hosts(wid)
        if not _may_read(kind, rest, hosts):
            raise ToolError(f"workload {ref!r} not found; call list_workloads")
        hours = int(args.get("hours") or 24)
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        points = ctx.svc.store.list_workload_metrics(wid, since, hours)
        names = _host_names(ctx.svc)
        return {
            "workload": wid, "hours": hours,
            "summary": _resource_summary(points),
            "points": _downsample(points),
            "pointsTotal": len(points),
            "memoryTrend": _growth_view(growth_of(ctx.svc.store, wid, sorted(hosts)), names),
            **({} if points else {"note": "no history yet: the monitor samples running workloads every few minutes"}),
        }

    @reg.tool(
        "memory_growth",
        title="Workloads with growing memory",
        description=(
            "Workloads whose memory has been climbing steadily over the last hours (a likely leak), steepest first, "
            "with the rate and the hours left before the memory limit. all=true lists every workload with a trend."
        ),
        schema={"properties": {"host": {**HOST_PROP, "description": "Host id or name (default: every host)."},
                               "all": {"type": "boolean"}, "limit": limit_prop(20, 100)}},
    )
    async def memory_growth(ctx, args):
        from ...monitor.workloads import growth_of

        rows, errors = await build_workloads(ctx.svc, _scope_hosts(ctx.svc, args))
        names = _host_names(ctx.svc)
        found = []
        for w in rows:
            try:
                g = growth_of(ctx.svc.store, w["id"], sorted(ctx.svc.store.workload_hosts(w["id"])))
            except Exception:  # noqa: BLE001
                continue
            if g is None or not (g["growing"] or args.get("all")):
                continue
            found.append((g["slopeBytesPerHour"], {"workload": w["id"], "name": w["name"],
                                                   **_growth_view(g, names)}))
        found.sort(key=lambda x: -x[0])
        out = page([f for _, f in found], args, 20, 100)
        if not found:
            out["note"] = "no workload's memory is growing steadily" if not args.get("all") else "no memory history yet"
        if errors:
            out["errors"] = errors
        return out

    @reg.tool(
        "search_logs",
        title="Search retained logs",
        description=(
            "Full-text search of the log archive: lines kept for the workloads that have log retention on, including "
            "containers that were recreated or removed since. Newest first. query supports words, \"phrases\", "
            "OR and -excluded words. Page back with before=<next>. container_logs reads a live container instead."
        ),
        schema={"properties": {
            "query": {"type": "string", "maxLength": 500},
            "workload": WORKLOAD_PROP,
            "host": HOST_PROP,
            "since": {"type": ["string", "number"], "description": "Relative (15m, 2h, 3d), RFC3339 or unix seconds."},
            "until": {"type": ["string", "number"], "description": "Same formats as since."},
            "before": {"type": "integer", "minimum": 1, "description": "The next value of a previous page."},
            "limit": limit_prop(100, 500),
        }},
    )
    async def search_logs(ctx, args):
        from ...api.logarchive import _visible
        from ...logstore.store import normalize_ts

        logs = getattr(ctx.svc, "logs", None)
        if logs is None:
            raise ToolError("the log archive is not available on this server")
        kept = logs.list_retention()
        if not kept:
            return {"items": [], "note": "no workload keeps its logs: turn on log retention for a workload "
                                         "(its page, Logs tab) or read live logs with container_logs"}
        workloads = None
        if args.get("workload"):
            ref = args["workload"]
            try:
                parse_workload(ref)
                wid = ref
            except ValueError:
                wid = (await resolve_workload(ctx.svc, ref, args.get("host")))["id"]
            workloads = [wid]
        host_id = resolve_host(ctx.svc, args["host"]).id if args.get("host") else ""
        since = parse_since(args.get("since"))
        until = parse_since(args.get("until"))
        limit = max(1, min(int(args.get("limit") or 100), 500))
        items: List[Dict[str, Any]] = []
        cursor = int(args.get("before") or 0)
        while len(items) < limit:
            batch = logs.search(text=args.get("query") or "", workloads=workloads, host_id=host_id,
                                since=normalize_ts(iso(since)) if since else "",
                                until=normalize_ts(iso(until)) if until else "",
                                before=cursor, limit=limit)
            if not batch:
                cursor = 0
                break
            items.extend(i for i in batch if _visible(i))
            cursor = batch[-1]["id"]
            if len(batch) < limit:
                cursor = 0
                break
        items = items[:limit]
        names = _host_names(ctx.svc)
        rows = []
        for i in items:
            line = i["line"]
            rows.append({"ts": i["ts"], "host": names.get(i["hostId"], i["hostId"]), "container": i["container"],
                         "workload": i["workload"],
                         "line": line if len(line) <= LOG_MAX_LINE else line[:LOG_MAX_LINE] + " [cut]"})
        out: Dict[str, Any] = {"items": rows, "returned": len(rows)}
        if len(items) == limit and cursor:
            out["next"] = items[-1]["id"]
        if workloads and workloads[0] not in {r["workload"] for r in kept}:
            out["note"] = f"{workloads[0]} does not keep its logs; only lines from when it did are here"
        return out
