"""Operations tools: uptime checks, guided disk cleanup, scheduled
start/stop, volume backups, log retention and the audit log. Reads follow
the REST rules; actions reuse the same services."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from ...auth import authorize as authz
from ..registry import ROLE_ADMIN, ROLE_OPERATOR, ToolError, ToolRegistry, role_allows
from ._common import HOST_PROP, OFFSET_PROP, human_bytes, limit_prop, resolve_host, resolve_ops, with_timeout
from .fleet import _host_names
from .workloads import WORKLOAD_PROP, resolve_workload

ITEMS_PER_CATEGORY = 50
TEXT_MAX = 1000

_CLEANUP_KEYS = {"containers": "containers", "dangling": "images", "images": "images", "volumes": "volumes"}


def _cut(v: Any) -> Any:
    return v[:TEXT_MAX] + " [cut]" if isinstance(v, str) and len(v) > TEXT_MAX else v


def _find_check(store: Any, ref: str) -> Dict[str, Any]:
    try:
        return store.get_uptime_check(ref)
    except Exception:  # noqa: BLE001 - fall back to the name
        pass
    named = [c for c in store.list_uptime_checks() if c["name"].lower() == ref.lower()]
    if len(named) == 1:
        return named[0]
    if len(named) > 1:
        raise ToolError(f"uptime check name {ref!r} is ambiguous; use the id")
    raise ToolError(f"uptime check {ref!r} not found; call uptime_status")


def register(reg: ToolRegistry) -> None:
    # -- uptime ----------------------------------------------------------------

    @reg.tool(
        "uptime_status",
        title="Uptime checks",
        description=(
            "HTTP, TCP and other uptime checks with their current status (up, down, paused), uptime over 24 h, 7 d "
            "and 30 d, latency, the last error and certificate expiry, plus the most recent down periods."
        ),
        schema={"properties": {"status": {"type": "string", "enum": ["up", "down", "paused", "pending"]},
                               "incidents": limit_prop(10, 100)}},
    )
    async def uptime_status(ctx, args):
        from ...api.uptime import _out

        store = ctx.svc.store
        operator = role_allows(ctx.role, ROLE_OPERATOR)
        checks = [_out(store, c, operator=operator, recent=False) for c in store.list_uptime_checks()]
        if args.get("status"):
            checks = [c for c in checks if c["status"] == args["status"]]
        names = {c["id"]: c["name"] for c in store.list_uptime_checks()}
        incidents = store.list_uptime_incidents(None, limit=int(args.get("incidents") or 10))
        for i in incidents:
            i["check"] = names.get(i["checkId"], i["checkId"])
            i["error"] = _cut(i.get("error"))
        return {
            "checks": checks,
            "down": sum(1 for c in checks if c["status"] == "down"),
            "incidents": incidents,
        }

    @reg.tool(
        "run_uptime_check",
        title="Run uptime check now",
        description="Probe an uptime check right away and return the result (ok, latency, status code, error).",
        schema={"properties": {"check": {"type": "string", "minLength": 1, "description": "Check id or name."}},
                "required": ["check"]},
        role=ROLE_OPERATOR, read_only=False, destructive=False, idempotent=True, open_world=True,
    )
    async def run_uptime_check(ctx, args):
        runner = getattr(ctx.svc, "uptime", None)
        if runner is None:
            raise ToolError("uptime checks are not available on this server")
        c = _find_check(ctx.svc.store, args["check"])
        res = await with_timeout(runner.run(c["id"]), "uptime check", 90)
        return {"check": c["name"], "ok": res.ok, "latencyMs": round(res.latency_ms, 1), "code": res.code,
                "error": _cut(res.error)}

    # -- cleanup -----------------------------------------------------------------

    @reg.tool(
        "cleanup_plan",
        title="Disk cleanup plan",
        description=(
            "What can be removed from a host to free disk space, by category (stopped containers, untagged images, "
            "unused images, unused volumes, build cache), with sizes and the items Kowan suggests removing. "
            "Nothing is removed; run_cleanup does that."
        ),
        schema={"properties": {"host": HOST_PROP}},
    )
    async def cleanup_plan(ctx, args):
        from ... import cleanup

        host, ops = resolve_ops(ctx.svc, args.get("host"))
        du = await with_timeout(ops.disk_usage(), "disk usage", 60)
        plan = cleanup.plan(du, host.id, ctx.svc.store)
        cats = []
        for c in plan["categories"]:
            items = [{"id": i["id"], "name": i["name"], "size": human_bytes(i["size"]), "detail": i["detail"],
                      "suggested": i["selected"], **({"note": i["note"]} if i["note"] else {})}
                     for i in c["items"]]
            cat = {"category": c["id"], "title": c["title"], "help": c["help"], "count": len(items),
                   "total": human_bytes(c["total"]), "suggestedTotal": human_bytes(c["selectedTotal"]),
                   "items": items[:ITEMS_PER_CATEGORY]}
            if len(items) > ITEMS_PER_CATEGORY:
                cat["itemsNote"] = f"first {ITEMS_PER_CATEGORY} of {len(items)}"
            cats.append(cat)
        return {"host": host.name, "total": human_bytes(plan["total"]),
                "suggestedTotal": human_bytes(plan["selectedTotal"]), "categories": cats,
                "schedule": cleanup.get_policy(ctx.svc.store, host.id),
                "lastRun": cleanup.last_run(ctx.svc.store, host.id)}

    @reg.tool(
        "run_cleanup",
        title="Run disk cleanup",
        description=(
            "Remove what cleanup_plan lists from a host. picks=suggested removes the items Kowan suggests (never "
            "named volumes, nor images pulled in the last days, nor stopped containers of a deployment); "
            "picks=listed removes exactly the ids given in containers, images and volumes (and the build cache "
            "with build_cache=true). Removed volumes lose their data."
        ),
        schema={"properties": {
            "host": HOST_PROP,
            "picks": {"type": "string", "enum": ["suggested", "listed"]},
            "containers": {"type": "array", "items": {"type": "string", "minLength": 1}, "maxItems": 500},
            "images": {"type": "array", "items": {"type": "string", "minLength": 1}, "maxItems": 500},
            "volumes": {"type": "array", "items": {"type": "string", "minLength": 1}, "maxItems": 500},
            "build_cache": {"type": "boolean"},
        }, "required": ["picks"]},
        role=ROLE_ADMIN, read_only=False, destructive=True, idempotent=False, requires_confirm=True,
    )
    async def run_cleanup(ctx, args):
        from ... import cleanup

        host, ops = resolve_ops(ctx.svc, args.get("host"))
        if args["picks"] == "suggested":
            du = await with_timeout(ops.disk_usage(), "disk usage", 60)
            plan = cleanup.plan(du, host.id, ctx.svc.store)
            picked = cleanup.Picked()
            for c in plan["categories"]:
                chosen = [i["id"] for i in c["items"] if i["selected"]]
                if c["id"] == "build":
                    picked.build_cache = bool(chosen)
                else:
                    getattr(picked, _CLEANUP_KEYS[c["id"]]).extend(chosen)
        else:
            picked = cleanup.Picked(list(args.get("containers") or []), list(args.get("images") or []),
                                    list(args.get("volumes") or []), args.get("build_cache") is True)
        if not (picked.containers or picked.images or picked.volumes or picked.build_cache):
            return {"host": host.name, "freed": "0 B", "note": "nothing to remove"}
        res = await with_timeout(cleanup.run(ops, picked), "cleanup", 600)
        return {"host": host.name, "freed": human_bytes(res["freed"]), "removed": res["removed"],
                "problems": res["problems"][:50]}

    # -- schedules -----------------------------------------------------------------

    @reg.tool(
        "list_schedules",
        title="List schedules",
        description=(
            "Scheduled start/stop of containers, stacks and deployments (cron, timezone, next run, last result), "
            "both those set in Kowan and those from kowan.schedule.* container labels, plus recent runs."
        ),
        schema={"properties": {"host": {**HOST_PROP, "description": "Only this host (default: every host)."},
                               "runs": limit_prop(10, 100)}},
    )
    async def list_schedules(ctx, args):
        svc = getattr(ctx.svc, "schedules", None)
        if svc is None:
            raise ToolError("schedules are not available on this server")
        from ...api.schedules import _targets

        host_id = resolve_host(ctx.svc, args["host"]).id if args.get("host") else ""
        items = await authz.TargetAccess(ctx.svc.store, ctx.svc.docker).filter(
            [s for s in svc.all_schedules() if not host_id or s.get("hostId") == host_id], _targets)
        ids = {s["id"] for s in items}
        runs = [r for r in ctx.svc.store.list_schedule_runs("", 200) if r.get("scheduleId") in ids]
        names = _host_names(ctx.svc)
        for s in items:
            if s.get("hostId"):
                s["host"] = names.get(s["hostId"], s["hostId"])
        for r in runs:
            r["message"] = _cut(r.get("message"))
        return {"items": items, "runs": runs[: int(args.get("runs") or 10)]}

    @reg.tool(
        "run_schedule",
        title="Run schedule now",
        description="Run a schedule's action (start or stop its target) right away. Returns the run.",
        schema={"properties": {"schedule": {"type": "string", "minLength": 1, "description": "Schedule id."}},
                "required": ["schedule"]},
        role=ROLE_OPERATOR, read_only=False, destructive=False, idempotent=False,
    )
    async def run_schedule(ctx, args):
        svc = getattr(ctx.svc, "schedules", None)
        if svc is None:
            raise ToolError("schedules are not available on this server")
        try:
            s = svc.find(args["schedule"])
        except Exception as err:  # noqa: BLE001
            raise ToolError(f"schedule {args['schedule']!r} not found; call list_schedules") from err
        from ...api.schedules import _targets

        access = authz.TargetAccess(ctx.svc.store, ctx.svc.docker)
        if not await access.allowed(_targets(s)[0]):
            raise ToolError(f"schedule {args['schedule']!r} not found; call list_schedules")
        await access.check(_targets(s), authz.LEVEL_OPERATE)
        return await with_timeout(svc.execute(s, trigger="manual"), "schedule", 300)

    # -- volume backups ------------------------------------------------------------

    @reg.tool(
        "list_backups",
        title="Volume backups",
        description=(
            "Volume backup jobs (what they back up, where, schedule, next run, last result) and recent backup runs. "
            "Filter by host, job or volume."
        ),
        schema={"properties": {"host": {**HOST_PROP, "description": "Only this host (default: every host)."},
                               "job": {"type": "string", "description": "Job id: only its runs."},
                               "volume": {"type": "string"},
                               "runs": limit_prop(20, 200)}},
    )
    async def list_backups(ctx, args):
        svc = getattr(ctx.svc, "volbackups", None)
        if svc is None:
            raise ToolError("volume backups are not available on this server")
        from ...api.volbackups import job_targets, run_targets

        host_id = resolve_host(ctx.svc, args["host"]).id if args.get("host") else ""
        access = authz.TargetAccess(ctx.svc.store, ctx.svc.docker)
        jobs = await access.filter([j for j in ctx.svc.store.list_backup_jobs()
                                    if not host_id or j.get("hostId") == host_id], job_targets)
        runs = await access.filter(ctx.svc.store.list_backup_runs(
            job_id=args.get("job") or "", volume=args.get("volume") or "", host_id=host_id,
            limit=int(args.get("runs") or 20)), run_targets)
        for r in runs:
            for k in ("log", "error", "message"):
                if k in r:
                    r[k] = _cut(r[k])
        return {"jobs": [svc.job_view(j) for j in jobs], "runs": runs}

    @reg.tool(
        "run_backup",
        title="Run backup job now",
        description="Start a volume backup job right away (in the background). Follow it with list_backups.",
        schema={"properties": {"job": {"type": "string", "minLength": 1, "description": "Backup job id or name."}},
                "required": ["job"]},
        role=ROLE_OPERATOR, read_only=False, destructive=False, idempotent=False, open_world=True,
    )
    async def run_backup(ctx, args):
        svc = getattr(ctx.svc, "volbackups", None)
        if svc is None:
            raise ToolError("volume backups are not available on this server")
        jobs = ctx.svc.store.list_backup_jobs()
        job = next((j for j in jobs if j["id"] == args["job"]), None)
        if job is None:
            named = [j for j in jobs if (j.get("name") or "").lower() == args["job"].lower()]
            if len(named) > 1:
                raise ToolError(f"backup job name {args['job']!r} is ambiguous; use the id")
            job = named[0] if named else None
        from ...api.volbackups import job_targets

        access = authz.TargetAccess(ctx.svc.store, ctx.svc.docker)
        if job is None or not await access.allowed_all(job_targets(job)):
            raise ToolError(f"backup job {args['job']!r} not found; call list_backups")
        await access.check(job_targets(job), authz.LEVEL_OPERATE)
        run = svc.start_backup(job, trigger="manual")
        return {"job": job.get("name") or job["id"], "run": run, "note": "runs in the background"}

    # -- log retention ---------------------------------------------------------------

    @reg.tool(
        "set_log_retention",
        title="Keep a workload's logs",
        description=(
            "Keep a workload's logs in the searchable archive for N days (1 to 365), or days=0 to stop keeping them "
            "and delete what is stored. Lines are kept from now on, not retroactively."
        ),
        schema={"properties": {"workload": WORKLOAD_PROP, "host": HOST_PROP,
                               "days": {"type": "integer", "minimum": 0, "maximum": 365}},
                "required": ["workload", "days"]},
        role=ROLE_OPERATOR, read_only=False, destructive=False, idempotent=True,
    )
    async def set_log_retention(ctx, args):
        from ...workloads import parse_workload

        logs = getattr(ctx.svc, "logs", None)
        collector = getattr(ctx.svc, "log_collector", None)
        if logs is None or collector is None:
            raise ToolError("the log archive is not available on this server")
        ref = args["workload"]
        try:
            parse_workload(ref)
            wid = ref
        except ValueError:
            wid = (await resolve_workload(ctx.svc, ref, args.get("host")))["id"]
        days = int(args["days"])
        if days == 0:
            removed = logs.delete_retention(wid)
            await collector.sync()
            return {"workload": wid, "kept": False, "linesDeleted": removed}
        logs.set_retention(wid, days, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                           getattr(ctx.user, "username", "") or "mcp")
        await collector.sync()
        return {"workload": wid, "kept": True, "days": days}

    # -- audit ------------------------------------------------------------------------

    @reg.tool(
        "audit_log",
        title="Audit log",
        description=(
            "Who did what, newest first: every change made through the console, the API, the CLI or MCP. Filter by "
            "user, a path substring (e.g. a deployment id) and since (RFC3339)."
        ),
        schema={"properties": {"user": {"type": "string"}, "path": {"type": "string"},
                               "since": {"type": "string", "description": "RFC3339 time."},
                               "limit": limit_prop(50, 500), "offset": OFFSET_PROP}},
        role=ROLE_ADMIN,
    )
    async def audit_log(ctx, args):
        from ...store.audit import AuditListOptions

        limit = max(1, min(int(args.get("limit") or 50), 500))
        items, total = ctx.svc.store.list_audit(AuditListOptions(
            limit=limit, offset=int(args.get("offset") or 0), user=args.get("user") or "",
            since=args.get("since") or "", path=args.get("path") or ""))
        rows: List[Dict[str, Any]] = []
        for e in items or []:
            d = e.model_dump(by_alias=True)
            d["detail"] = _cut(d.get("detail"))
            rows.append(d)
        offset = int(args.get("offset") or 0)
        out: Dict[str, Any] = {"items": rows, "total": total, "returned": len(rows)}
        if offset + len(rows) < total:
            out["nextOffset"] = offset + len(rows)
        return out

