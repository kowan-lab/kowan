"""Runbook tools: the approved actions an assistant may run on a workload,
instead of arbitrary commands. A run needs the operator role and operate on
every workload the runbook touches, as through the REST API; a runbook that
asks for confirmation needs ``confirm: true`` too."""

from __future__ import annotations

from typing import Any, Dict

from ...auth import authorize as authz
from ..registry import ROLE_OPERATOR, ToolError, ToolRegistry
from ._common import with_timeout
from .workloads import WORKLOAD_PROP, resolve_workload

# The end of each step's output returned to the assistant.
OUTPUT_TAIL = 4000
RUN_TIMEOUT = 900


def _targets(rb: Dict[str, Any]):
    from ...api.runbooks import _targets as targets

    return targets(rb)


def _brief(rb: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": rb["id"], "name": rb["name"], "description": rb.get("description", ""), "source": rb.get("source", ""),
        "confirm": rb.get("confirm", False),
        "params": [{k: p[k] for k in ("name", "label", "type", "choices", "default", "required") if p.get(k) not in (None, "", [])}
                   for p in rb.get("params") or []],
        "steps": [{"title": s.get("title", ""), "workload": s["workload"], "service": s.get("service", ""),
                   "mode": s["mode"], "replicas": s.get("replicas", "one"), "command": s["command"]}
                  for s in rb.get("steps") or []],
    }


def register(reg: ToolRegistry) -> None:
    @reg.tool(
        "list_runbooks",
        title="Runbooks",
        description=(
            "The runbooks (named, approved actions) defined for the workloads you can see, or for one workload: "
            "what each runs, where, and its parameters. Prefer run_runbook over arbitrary commands."
        ),
        schema={"properties": {"workload": {**WORKLOAD_PROP, "description": "Only the runbooks touching this workload."}}},
    )
    async def list_runbooks(ctx, args):
        svc = getattr(ctx.svc, "runbooks", None)
        if svc is None:
            raise ToolError("runbooks are not available on this server")
        items = ctx.svc.store.list_runbooks() + await svc.labelled()
        if args.get("workload"):
            from ...runbooks import model

            wl = (await resolve_workload(ctx.svc, args["workload"]))["id"]
            items = [rb for rb in items if wl in model.workloads_of(rb)]
        items = await authz.TargetAccess(ctx.svc.store, ctx.svc.docker).filter(items, _targets)
        return {"items": [_brief(rb) for rb in items]}

    @reg.tool(
        "run_runbook",
        title="Run a runbook",
        description=(
            "Run a runbook (see list_runbooks) with its parameters and wait for it to end. Returns the status and, "
            "for each step, the container, exit code and the end of its output. A runbook marked confirm needs "
            "confirm: true: ask the user first."
        ),
        schema={"properties": {
            "runbook": {"type": "string", "minLength": 1, "description": "Runbook id."},
            "params": {"type": "object", "additionalProperties": {"type": "string"},
                       "description": "Parameter values by name; the defaults fill the rest."},
            "confirm": {"type": "boolean", "description": "Needed for a runbook marked confirm."},
        }, "required": ["runbook"]},
        role=ROLE_OPERATOR, read_only=False, destructive=False, idempotent=False,
    )
    async def run_runbook(ctx, args):
        svc = getattr(ctx.svc, "runbooks", None)
        if svc is None:
            raise ToolError("runbooks are not available on this server")
        try:
            rb = await svc.get(args["runbook"])
        except Exception as err:  # noqa: BLE001
            raise ToolError(f"runbook {args['runbook']!r} not found; call list_runbooks") from err
        access = authz.TargetAccess(ctx.svc.store, ctx.svc.docker)
        if not await access.allowed_all(_targets(rb)):
            raise ToolError(f"runbook {args['runbook']!r} not found; call list_runbooks")
        await access.check(_targets(rb), authz.LEVEL_OPERATE)
        if rb.get("confirm") and args.get("confirm") is not True:
            raise ToolError(f"runbook {rb['name']!r} asks for confirmation: ask the user, then call again with confirm: true")
        user = getattr(ctx.user, "username", "") or "mcp"
        try:
            run = await with_timeout(svc.run_and_wait(rb, args.get("params") or {}, user, "mcp"), "runbook", RUN_TIMEOUT)
        except ValueError as err:
            raise ToolError(str(err)) from err
        return {
            "runId": run["id"], "status": run["status"],
            "steps": [{"step": r["step"], "title": r["title"], "workload": r["workload"], "service": r["service"],
                       "container": r["container"], "status": r["status"], "exitCode": r["exitCode"],
                       "error": r["error"], "output": r["output"][-OUTPUT_TAIL:]} for r in run["steps"]],
        }
