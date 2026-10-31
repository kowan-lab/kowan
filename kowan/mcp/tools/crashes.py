"""Crash snapshot tools: what crashed containers looked like when they died
(kowan/crashes), so an assistant can diagnose a crash after the container was
replaced."""

from __future__ import annotations

from ...auth import authorize as authz
from ..registry import ToolError, ToolRegistry
from ._common import limit_prop
from .workloads import WORKLOAD_PROP, resolve_workload

LOG_TAIL = 6000


def register(reg: ToolRegistry) -> None:
    @reg.tool(
        "crash_snapshots",
        title="Crash snapshots",
        description=(
            "Containers that crashed (non-zero exit, not stopped on purpose), newest first, with exit code, OOM and how "
            "many times they crashed again. With id: one snapshot with the end of its logs, its configuration, its "
            "environment (secrets hidden) and the workload's resources before the crash."
        ),
        schema={"properties": {
            "workload": {**WORKLOAD_PROP, "description": "Only this workload's crashes."},
            "id": {"type": "string", "description": "One snapshot, in full."},
            "limit": limit_prop(20, 200),
        }},
    )
    async def crash_snapshots(ctx, args):
        from ...api.crashes import _targets

        store = ctx.svc.store
        access = authz.TargetAccess(store, ctx.svc.docker)
        if args.get("id"):
            try:
                snap = store.get_crash_snapshot(args["id"])
            except Exception as err:  # noqa: BLE001
                raise ToolError(f"crash snapshot {args['id']!r} not found") from err
            if not await access.allowed_all(_targets(snap)):
                raise ToolError(f"crash snapshot {args['id']!r} not found")
            snap["data"]["logs"] = snap["data"].get("logs", "")[-LOG_TAIL:]
            return snap
        workload = (await resolve_workload(ctx.svc, args["workload"]))["id"] if args.get("workload") else ""
        items = store.list_crash_snapshots(workload, "", int(args.get("limit") or 20))
        return {"items": await access.filter(items, _targets)}
