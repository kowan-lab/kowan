"""Read-only fleet tools: dashboard summary, deployments, templates, alerts and
workflows. Viewer role."""


from __future__ import annotations

import asyncio
from typing import Any, Dict, List

from ...redact import redact_compose, strip_userinfo
from ...auth import authorize as authz
from ..registry import ToolError, ToolRegistry, to_plain
from ._common import (
    DEPLOYMENT_PROP,
    OFFSET_PROP,
    container_name,
    exit_code_from_status,
    find_by_id_or_name,
    human_bytes,
    limit_prop,
    page,
    ping_host,
    short_id,
    with_timeout,
)

COMPOSE_MAX_CHARS = 16 * 1024


def resolve_deployment(svc: Any, ref: str) -> Any:
    try:
        dep = svc.store.get_deployment(ref)
    except Exception:  # noqa: BLE001
        dep = None
    if dep is None:
        try:
            dep = svc.store.get_deployment_by_name(ref)
        except Exception:  # noqa: BLE001
            raise ToolError(f"deployment {ref!r} not found; call list_deployments") from None
    # Team/grant scoping; the level follows the tool (read / operate / manage).
    if not authz.deployment_allowed(dep, authz.LEVEL_READ):
        raise ToolError(f"deployment {ref!r} not found; call list_deployments")
    authz.check_deployment(dep)
    return dep


def _host_names(svc: Any) -> Dict[str, str]:
    try:
        return {h.id: h.name for h in svc.store.list_hosts()}
    except Exception:  # noqa: BLE001
        return {}


def deployment_row(d: Any, names: Dict[str, str]) -> Dict[str, Any]:
    row = {
        "id": d.id, "name": d.name, "type": d.type, "strategy": d.strategy, "status": d.status,
        "targets": [{"id": t, "name": names.get(t, t)} for t in d.targets],
        "enabled": d.enabled, "protected": d.protected, "updatedAt": d.updated_at,
    }
    if d.type == "container" and d.spec is not None:
        row["image"] = d.spec.image
    if d.git is not None and d.git.repo_url:
        row["git"] = {"repoUrl": strip_userinfo(d.git.repo_url), "ref": d.git.ref, "path": d.git.path,
                      "deployedCommit": d.deployed_commit[:12]}
    return row


def sanitize_deployment(d: Any, names: Dict[str, str]) -> Dict[str, Any]:
    """Full deployment view minus secrets: env values, webhook tokens/secrets."""
    out = deployment_row(d, names)
    out["replicas"] = d.replicas
    out["createdAt"] = d.created_at
    out["webhookEnabled"] = bool(d.redeploy_token)
    if d.spec is not None:
        spec = to_plain(d.spec)
        spec["env"] = [kv.get("key", "") for kv in spec.get("env") or []]
        spec["envNote"] = "environment values are hidden"
        out["spec"] = spec
    if d.compose:
        # The same reading copy viewers get over REST: no values that may be
        # secrets (env, build args, inline secrets, anchors, x- fields).
        c = redact_compose(d.compose)
        if len(c) > COMPOSE_MAX_CHARS:
            out["composeNote"] = f"compose truncated to {COMPOSE_MAX_CHARS} of {len(c)} characters"
            c = c[:COMPOSE_MAX_CHARS]
        out["compose"] = c
    return out


async def fleet_snapshot(svc: Any, *, crashed_limit: int = 25, alerts_limit: int = 10) -> Dict[str, Any]:
    hosts = authz.filter_hosts(svc.store.list_hosts())
    online = await asyncio.gather(*(ping_host(svc, h) for h in hosts))

    async def host_view(h, ok):
        view: Dict[str, Any] = {"id": h.id, "name": h.name, "online": ok}
        if not ok:
            return view, []
        crashed: List[Dict[str, Any]] = []
        try:
            ops = svc.docker.ops(h)
            cs = await with_timeout(ops.list_containers(True), f"containers on {h.name}", 15)
            view["containersTotal"] = len(cs)
            view["containersRunning"] = sum(1 for c in cs if c.state == "running")
            for c in cs:
                code = exit_code_from_status(c.status)
                unhealthy = "(unhealthy)" in (c.status or "")
                if c.state in ("restarting", "dead") or (c.state == "exited" and code not in (None, 0)) or unhealthy:
                    crashed.append({
                        "host": h.name, "hostId": h.id, "id": short_id(c.id), "name": container_name(c),
                        "image": c.image, "state": c.state, "status": c.status, "exitCode": code,
                        "unhealthy": unhealthy,
                    })
        except Exception as err:  # noqa: BLE001
            view["error"] = str(err)
        try:
            du = await with_timeout(ops.disk_usage(), f"disk usage on {h.name}", 15)
            reclaim = int(du.images_reclaimable or 0) + int(du.volumes_reclaimable or 0) + int(du.build_cache_size or 0)
            view["reclaimableBytes"] = reclaim
            view["reclaimable"] = human_bytes(reclaim)
        except Exception as err:  # noqa: BLE001
            view.setdefault("error", str(err))
        return view, crashed

    results = await asyncio.gather(*(host_view(h, ok) for h, ok in zip(hosts, online)))
    host_views = [v for v, _ in results]
    crashed = [c for _, cs in results for c in cs]

    try:
        alerts = authz.filter_host_items(svc.store.list_alerts(1000))
    except Exception:  # noqa: BLE001
        alerts = []
    open_alerts = [a for a in alerts if not a.resolved]
    reclaim_total = sum(v.get("reclaimableBytes", 0) for v in host_views)
    return {
        "hosts": {"total": len(hosts), "online": sum(1 for o in online if o)},
        "containers": {
            "running": sum(v.get("containersRunning", 0) for v in host_views),
            "total": sum(v.get("containersTotal", 0) for v in host_views),
        },
        "crashed": crashed[:crashed_limit],
        "crashedTotal": len(crashed),
        "openAlerts": {
            "total": len(open_alerts),
            "critical": sum(1 for a in open_alerts if a.level == "critical"),
            "latest": [
                {"id": a.id, "level": a.level, "title": a.title, "resource": a.resource,
                 "hostId": a.host_id, "createdAt": a.created_at}
                for a in open_alerts[:alerts_limit]
            ],
        },
        "reclaimableBytes": reclaim_total,
        "reclaimable": human_bytes(reclaim_total),
        "perHost": host_views,
    }


FLEET_SCHEMA = {
    "type": "object",
    "properties": {
        "hosts": {"type": "object", "properties": {"total": {"type": "integer"}, "online": {"type": "integer"}}},
        "containers": {"type": "object", "properties": {"running": {"type": "integer"}, "total": {"type": "integer"}}},
        "crashed": {"type": "array", "items": {"type": "object"}},
        "crashedTotal": {"type": "integer"},
        "openAlerts": {"type": "object"},
        "reclaimableBytes": {"type": "integer"},
        "reclaimable": {"type": "string"},
        "perHost": {"type": "array", "items": {"type": "object"}},
    },
    "required": ["hosts", "containers", "crashed", "openAlerts", "reclaimableBytes"],
}


def register(reg: ToolRegistry) -> None:
    @reg.tool(
        "fleet_summary",
        title="Fleet summary",
        description=(
            "What the dashboard shows: hosts online, containers running/total, crashed or unhealthy containers with "
            "exit codes, open alerts and reclaimable disk space per host. The best first call."
        ),
        schema={"properties": {}},
        output_schema=FLEET_SCHEMA,
    )
    async def fleet_summary(ctx, args):
        return await fleet_snapshot(ctx.svc)

    @reg.tool(
        "list_deployments",
        title="List deployments",
        description="List deployments (container or compose stacks) with status, target host and git source.",
        schema={"properties": {
            "status": {"type": "string", "description": "Filter by status, e.g. deployed, stopped, error, pending."},
            "name": {"type": "string"}, "limit": limit_prop(100, 500), "offset": OFFSET_PROP}},
    )
    async def list_deployments(ctx, args):
        names = _host_names(ctx.svc)
        rows = [deployment_row(d, names) for d in authz.filter_deployments(ctx.svc.store.list_deployments())]
        if args.get("status"):
            rows = [r for r in rows if r["status"] == args["status"]]
        if args.get("name"):
            rows = [r for r in rows if args["name"].lower() in r["name"].lower()]
        return page(rows, args, 100, 500)

    @reg.tool(
        "get_deployment",
        title="Get deployment",
        description=(
            "One deployment with its spec or compose file (environment values and webhook secrets hidden) and the "
            "live runtime status of each target host."
        ),
        schema={"properties": {"deployment": DEPLOYMENT_PROP}, "required": ["deployment"]},
    )
    async def get_deployment(ctx, args):
        d = resolve_deployment(ctx.svc, args["deployment"])
        out = sanitize_deployment(d, _host_names(ctx.svc))
        dep_svc = getattr(ctx.svc, "deployments", None)
        if dep_svc is not None:
            try:
                out["targetStatus"] = to_plain(await with_timeout(dep_svc.status(d.id), "deployment status", 20))
            except Exception as err:  # noqa: BLE001
                out["targetStatusError"] = str(err)
        return out

    @reg.tool(
        "list_templates",
        title="List app templates",
        description="List the app-template catalog (built-in and custom) with the inputs each template asks for. Use with deploy_template.",
        schema={"properties": {"category": {"type": "string"}, "query": {"type": "string"},
                               "limit": limit_prop(50, 500), "offset": OFFSET_PROP}},
    )
    async def list_templates(ctx, args):
        from ...templates import builtins

        try:
            custom = ctx.svc.store.list_templates()
        except Exception:  # noqa: BLE001
            custom = []
        rows = []
        for t in list(builtins()) + list(custom):
            rows.append({
                "id": t.id, "name": t.name, "category": t.category, "type": t.type, "builtIn": t.built_in,
                "description": (t.description or "")[:300],
                "inputs": [
                    {k: v for k, v in to_plain(i).items() if k in ("name", "label", "required", "default", "description", "secret")}
                    for i in t.inputs
                ],
            })
        if args.get("category"):
            rows = [r for r in rows if (r["category"] or "").lower() == args["category"].lower()]
        if args.get("query"):
            q = args["query"].lower()
            rows = [r for r in rows if q in r["name"].lower() or q in r["description"].lower()]
        return page(rows, args, 50, 500)

    @reg.tool(
        "list_alerts",
        title="List alerts",
        description="Monitoring alerts, newest first. status=open (default), resolved or all.",
        schema={"properties": {"status": {"type": "string", "enum": ["open", "resolved", "all"]},
                               "level": {"type": "string", "enum": ["info", "warning", "critical"]},
                               "limit": limit_prop(50, 500), "offset": OFFSET_PROP}},
    )
    async def list_alerts(ctx, args):
        status = args.get("status") or "open"
        alerts = authz.filter_host_items(ctx.svc.store.list_alerts(1000))
        names = _host_names(ctx.svc)
        rows = []
        for a in alerts:
            if status == "open" and a.resolved or status == "resolved" and not a.resolved:
                continue
            if args.get("level") and a.level != args["level"]:
                continue
            rows.append({"id": a.id, "level": a.level, "title": a.title, "message": (a.message or "")[:500],
                         "resource": a.resource, "host": names.get(a.host_id, a.host_id),
                         "resolved": a.resolved, "createdAt": a.created_at})
        return page(rows, args, 50, 500)

    @reg.tool(
        "list_workflows",
        title="List workflows",
        description="List automation workflows (id, name, enabled, triggers, step count). Use with run_workflow.",
        schema={"properties": {"limit": limit_prop(100, 500), "offset": OFFSET_PROP}},
    )
    async def list_workflows(ctx, args):
        rows = []
        for w in (ctx.svc.store.list_workflows() if authz.global_allowed() else []):
            rows.append({"id": w.id, "name": w.name, "description": (w.description or "")[:300],
                         "enabled": w.enabled, "steps": len(w.steps),
                         "triggers": sorted({getattr(t, "type", "") for t in w.triggers if getattr(t, "type", "")})})
        return page(rows, args, 100, 500)


def find_workflow(svc: Any, ref: str) -> Any:
    authz.check_global()  # workflows act fleet-wide: needs an all-hosts grant
    try:
        return svc.store.get_workflow(ref)
    except Exception:  # noqa: BLE001
        pass
    return find_by_id_or_name(svc.store.list_workflows(), ref, "workflow")
