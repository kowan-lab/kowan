"""Operator tools: container lifecycle, deployment lifecycle, image pull,
container update and manual workflow runs. Each call is audited."""

from __future__ import annotations

from typing import Any

from ..registry import ROLE_OPERATOR, ToolError, ToolRegistry, to_plain
from ._common import CONTAINER_PROP, DEPLOYMENT_PROP, HOST_PROP, resolve_ops, short_id, with_timeout
from .fleet import _host_names, deployment_row, find_workflow, resolve_deployment

PULL_TIMEOUT = 900.0
ACTION_TIMEOUT = 120.0

_LIFECYCLE = {
    "start": ("start_container", "started"),
    "stop": ("stop_container", "stopped"),
    "restart": ("restart_container", "restarted"),
    "pause": ("pause_container", "paused"),
    "unpause": ("unpause_container", "unpaused"),
}


async def _kill(ops: Any, cid: str, signal: str) -> None:
    """Kill a container. The Ops contract has no kill op yet, so use one when a
    client provides it, else talk to the local Engine API directly."""
    fn = getattr(ops, "kill_container", None)
    if callable(fn):
        await fn(cid, signal)
        return
    inner = getattr(ops, "inner", ops)
    from ...docker.local import LocalClient

    if isinstance(inner, LocalClient):
        await inner._request("POST", f"/containers/{cid}/kill", params={"signal": signal})
        return
    raise ToolError("kill is not supported on remote hosts by this agent version; use action=stop")


async def pull(svc: Any, ops: Any, ref: str, registry_id: str = "") -> None:
    from ... import registry
    from ...api.updates import _drain_pull

    auth = await registry.docker_auth_for(svc.store, ref, registry_id)
    stream = await with_timeout(ops.pull_image(ref, auth), f"pull {ref}", 60)
    await with_timeout(_drain_pull(stream), f"pull {ref}", PULL_TIMEOUT)


def register(reg: ToolRegistry) -> None:
    @reg.tool(
        "container_action",
        title="Container lifecycle action",
        description="Start, stop, restart, pause, unpause or kill a container.",
        schema={
            "properties": {
                "host": HOST_PROP,
                "container": CONTAINER_PROP,
                "action": {"type": "string", "enum": [*_LIFECYCLE, "kill"]},
                "signal": {"type": "string", "description": "Signal for kill (default SIGKILL).", "maxLength": 16},
            },
            "required": ["container", "action"],
        },
        role=ROLE_OPERATOR, read_only=False, destructive=False, idempotent=False,
    )
    async def container_action(ctx, args):
        host, ops = resolve_ops(ctx.svc, args.get("host"))
        cid, action = args["container"], args["action"]
        if action == "kill":
            await with_timeout(_kill(ops, cid, args.get("signal") or "SIGKILL"), "kill", ACTION_TIMEOUT)
            word = "killed"
        else:
            op, word = _LIFECYCLE[action]
            await with_timeout(getattr(ops, op)(cid), action, ACTION_TIMEOUT)
        return {"container": cid, "host": host.name, "action": action, "status": word}

    @reg.tool(
        "deployment_action",
        title="Deployment lifecycle action",
        description=(
            "Run a deployment action: deploy / redeploy (queued in the background; poll get_deployment), start, stop, "
            "or sync (git deployments: pull the latest commit and redeploy when it changed; force=true always redeploys)."
        ),
        schema={
            "properties": {
                "deployment": DEPLOYMENT_PROP,
                "action": {"type": "string", "enum": ["deploy", "redeploy", "start", "stop", "sync"]},
                "force": {"type": "boolean", "description": "sync only: redeploy even if the commit is unchanged."},
            },
            "required": ["deployment", "action"],
        },
        role=ROLE_OPERATOR, read_only=False, destructive=False, idempotent=False, open_world=True,
    )
    async def deployment_action(ctx, args):
        dep_svc = getattr(ctx.svc, "deployments", None)
        if dep_svc is None:
            raise ToolError("deployment service unavailable")
        d = resolve_deployment(ctx.svc, args["deployment"])
        action = args["action"]
        out: dict = {"deployment": d.name, "action": action}
        if action in ("deploy", "redeploy"):
            dep = await dep_svc.dispatch_deploy(d.id, redeploy=action == "redeploy")
            out["queued"] = True
            out["note"] = "deploy runs in the background; call get_deployment to follow its status"
        elif action == "sync":
            dep, changed, commit = await with_timeout(dep_svc.sync(d.id, force=bool(args.get("force"))), "sync", 120)
            out.update({"changed": changed, "commit": commit})
        else:
            await with_timeout(getattr(dep_svc, action)(d.id), action, 300)
            dep = ctx.svc.store.get_deployment(d.id)
        out["state"] = deployment_row(dep, _host_names(ctx.svc)) if dep is not None else None
        return out

    @reg.tool(
        "pull_image",
        title="Pull image",
        description="Pull an image on a host using the stored registry credentials (registry_id picks one explicitly).",
        schema={"properties": {"host": HOST_PROP, "ref": {"type": "string", "minLength": 1, "description": "Image reference, e.g. nginx:1.27"},
                               "registry_id": {"type": "string"}}, "required": ["ref"]},
        role=ROLE_OPERATOR, read_only=False, destructive=False, idempotent=True, open_world=True,
    )
    async def pull_image(ctx, args):
        host, ops = resolve_ops(ctx.svc, args.get("host"))
        await pull(ctx.svc, ops, args["ref"], args.get("registry_id") or "")
        return {"ref": args["ref"], "host": host.name, "status": "pulled"}

    @reg.tool(
        "update_container",
        title="Update container image",
        description=(
            "Pull the container's image and recreate the container with the same configuration when a newer image is "
            "available (no-op when already up to date unless force=true). The container gets a new id."
        ),
        schema={"properties": {"host": HOST_PROP, "container": CONTAINER_PROP, "force": {"type": "boolean"}},
                "required": ["container"]},
        role=ROLE_OPERATOR, read_only=False, destructive=False, idempotent=True, open_world=True,
    )
    async def update_container(ctx, args):
        from ...api import updates as up

        host, ops = resolve_ops(ctx.svc, args.get("host"))
        cid = args["container"]
        insp = await with_timeout(ops.inspect_container(cid), "inspect")
        image = ((insp or {}).get("Config") or {}).get("Image") or ""
        try:
            ref = up.parse_image_ref(image)
        except ValueError as err:
            raise ToolError(f"cannot update {image or cid}: {err}") from err
        if ref.digest:
            raise ToolError("image is pinned by digest; change the reference to update")

        if not args.get("force"):
            img = await with_timeout(ops.inspect_image((insp or {}).get("Image") or image), "inspect image")
            repo_digests = (img or {}).get("RepoDigests") or []
            if repo_digests:
                async def fetch() -> str:
                    try:
                        return await up._remote_digest(ctx.svc.store, ref)
                    except Exception as err:  # noqa: BLE001
                        raise ValueError(up.friendly_registry_error(ref, err)) from err

                entry = await with_timeout(up._CACHE.resolve(ref.key, fetch, refresh=True), "registry lookup", 60)
                if entry.error:
                    raise ToolError(f"update check failed: {entry.error} (use force=true to pull anyway)")
                _local, avail = up.compare_digests(repo_digests, ref, entry.digest)
                if not avail:
                    return {"container": cid, "host": host.name, "image": image, "updated": False,
                            "status": "already up to date"}

        await pull(ctx.svc, ops, ref.pull_ref)
        new_id = await with_timeout(up.recreate_with_image(ops, cid, insp, image), "recreate", 300)
        return {"container": cid, "host": host.name, "image": image, "updated": True, "newId": short_id(new_id)}

    @reg.tool(
        "run_workflow",
        title="Run workflow",
        description="Trigger a manual run of a workflow (asynchronous). Returns the run id.",
        schema={"properties": {"workflow": {"type": "string", "minLength": 1, "description": "Workflow id or name."}},
                "required": ["workflow"]},
        role=ROLE_OPERATOR, read_only=False, destructive=False, idempotent=False, open_world=True,
    )
    async def run_workflow(ctx, args):
        from ...models import WorkflowTriggerManual
        from ...workflow.policy import script_rights_problem

        engine = getattr(ctx.svc, "workflow_engine", None)
        if engine is None:
            raise ToolError("workflow engine unavailable")
        wf = find_workflow(ctx.svc, args["workflow"])
        problem = script_rights_problem(getattr(ctx.user, "role", ""), wf.steps)
        if problem:
            raise ToolError(problem)
        run_id = await engine.run(wf.id, WorkflowTriggerManual, None)
        return {"workflow": wf.name, "workflowId": wf.id, "runId": run_id, "status": "queued"}
