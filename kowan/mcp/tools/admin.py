"""Admin tools: destructive removals/prune (confirm-guarded) and creating
deployments (from a spec, a compose file or an app template)."""

from __future__ import annotations

import secrets as _secrets
import uuid
from typing import Any, Dict

from ..registry import ROLE_ADMIN, ToolError, ToolRegistry
from ._common import CONTAINER_PROP, DEPLOYMENT_PROP, HOST_PROP, human_bytes, resolve_host, resolve_ops, with_timeout
from .fleet import _host_names, resolve_deployment, sanitize_deployment


def create_deployment_record(svc: Any, body: Dict[str, Any]) -> Any:
    """Create a deployment record exactly like ``POST /api/v1/deployments``."""
    from ...api import deployments as dapi
    from ...models import Deployment

    try:
        req = dapi._Request(body)
    except Exception as err:  # noqa: BLE001 - pydantic validation of spec/git
        raise ToolError(f"invalid deployment: {err}") from err
    msg = dapi._validate(req)
    if msg:
        raise ToolError(msg)
    try:
        svc.store.get_deployment_by_name(req.name)
        raise ToolError("a deployment with that name already exists")
    except ToolError:
        raise
    except Exception:  # noqa: BLE001 - NotFound => free
        pass
    coll = dapi._check_resource_name_collision(svc, req.name, "")
    if coll:
        raise ToolError(coll)
    now = dapi._now_rfc3339()
    dep = Deployment(
        id=str(uuid.uuid4()), name=req.name, type=req.type, strategy=req.strategy, targets=req.targets,
        replicas=req.replicas, spec=req.spec, compose=req.compose, status=dapi.STATUS_STOPPED,
        createdAt=now, updatedAt=now,
    )
    if req.git is not None:
        req.git.webhook_secret = dapi._generate_redeploy_token() + dapi._generate_redeploy_token()
        dep.git = req.git
        dep.redeploy_token = dapi._generate_redeploy_token()
    from ...auth import authorize as _authz  # team/grant scoping

    _authz.check_deployment(dep, _authz.LEVEL_MANAGE)
    svc.store.create_deployment(dep, source="mcp")
    return dep


async def _maybe_deploy(svc: Any, dep: Any, deploy: bool) -> Dict[str, Any]:
    out: Dict[str, Any] = {"deployed": False}
    if deploy:
        dep_svc = getattr(svc, "deployments", None)
        if dep_svc is None:
            raise ToolError("deployment created but the deployment service is unavailable")
        await dep_svc.dispatch_deploy(dep.id, redeploy=False)
        out = {"deployed": True, "note": "deploy queued in the background; call get_deployment to follow it"}
    return out


def register(reg: ToolRegistry) -> None:
    destructive = dict(role=ROLE_ADMIN, read_only=False, destructive=True, idempotent=True, requires_confirm=True)

    @reg.tool(
        "remove_container",
        title="Remove container",
        description="Remove a container (force=true also removes a running one). Destructive: requires confirm=true.",
        schema={"properties": {"host": HOST_PROP, "container": CONTAINER_PROP, "force": {"type": "boolean"}},
                "required": ["container"]},
        **destructive,
    )
    async def remove_container(ctx, args):
        host, ops = resolve_ops(ctx.svc, args.get("host"))
        await with_timeout(ops.remove_container(args["container"], bool(args.get("force"))), "remove container", 120)
        return {"container": args["container"], "host": host.name, "status": "removed"}

    @reg.tool(
        "remove_image",
        title="Remove image",
        description="Remove an image by id or tag (force=true removes it even if tagged multiple times). Requires confirm=true.",
        schema={"properties": {"host": HOST_PROP, "image": {"type": "string", "minLength": 1}, "force": {"type": "boolean"}},
                "required": ["image"]},
        **destructive,
    )
    async def remove_image(ctx, args):
        host, ops = resolve_ops(ctx.svc, args.get("host"))
        await with_timeout(ops.remove_image(args["image"], bool(args.get("force"))), "remove image", 120)
        return {"image": args["image"], "host": host.name, "status": "removed"}

    @reg.tool(
        "remove_volume",
        title="Remove volume",
        description="Remove a volume and ALL its data. Requires confirm=true.",
        schema={"properties": {"host": HOST_PROP, "volume": {"type": "string", "minLength": 1}, "force": {"type": "boolean"}},
                "required": ["volume"]},
        **destructive,
    )
    async def remove_volume(ctx, args):
        host, ops = resolve_ops(ctx.svc, args.get("host"))
        await with_timeout(ops.remove_volume(args["volume"], bool(args.get("force"))), "remove volume", 120)
        return {"volume": args["volume"], "host": host.name, "status": "removed"}

    @reg.tool(
        "prune",
        title="Prune unused objects",
        description=(
            "Prune unused Docker objects of one kind on a host: stopped containers, dangling images (all_images=true: "
            "every unused image), unused volumes (data is lost) or unused networks. Requires confirm=true."
        ),
        schema={"properties": {"host": HOST_PROP,
                               "kind": {"type": "string", "enum": ["containers", "images", "volumes", "networks"]},
                               "all_images": {"type": "boolean"}},
                "required": ["kind"]},
        **destructive,
    )
    async def prune(ctx, args):
        host, ops = resolve_ops(ctx.svc, args.get("host"))
        kind = args["kind"]
        reclaimed = 0
        if kind == "containers":
            reclaimed = await with_timeout(ops.prune_containers(), "prune", 300)
        elif kind == "images":
            reclaimed = await with_timeout(ops.prune_images(bool(args.get("all_images"))), "prune", 300)
        elif kind == "volumes":
            reclaimed = await with_timeout(ops.prune_volumes(), "prune", 300)
        else:
            await with_timeout(ops.prune_networks(), "prune", 300)
        return {"kind": kind, "host": host.name, "spaceReclaimed": reclaimed or 0,
                "spaceReclaimedHuman": human_bytes(reclaimed or 0)}

    @reg.tool(
        "destroy_deployment",
        title="Destroy deployment",
        description=(
            "Remove a deployment's running containers/stack, keeping the record (delete_record=true also deletes the "
            "record; refused while deletion protection is on). Requires confirm=true."
        ),
        schema={"properties": {"deployment": DEPLOYMENT_PROP, "delete_record": {"type": "boolean"}},
                "required": ["deployment"]},
        **destructive,
    )
    async def destroy_deployment(ctx, args):
        dep_svc = getattr(ctx.svc, "deployments", None)
        if dep_svc is None:
            raise ToolError("deployment service unavailable")
        d = resolve_deployment(ctx.svc, args["deployment"])
        if args.get("delete_record"):
            if d.protected:
                raise ToolError("deletion protection is enabled; disable it before deleting")
            err_msg = ""
            try:
                await with_timeout(dep_svc.destroy(d.id), "destroy", 300)
            except Exception as err:  # noqa: BLE001 - mirror REST: record still removable
                err_msg = str(err)
            ctx.svc.store.delete_deployment(d.id)
            try:
                dep_svc.remove_git_checkout(d.id)
            except Exception:  # noqa: BLE001
                pass
            out = {"deployment": d.name, "status": "deleted"}
            if err_msg:
                out["destroyWarning"] = err_msg
            return out
        await with_timeout(dep_svc.destroy(d.id), "destroy", 300)
        return {"deployment": d.name, "status": "destroyed", "note": "record kept; deployment_action deploy recreates it"}

    @reg.tool(
        "create_deployment",
        title="Create deployment",
        description=(
            "Create a deployment on one host from a compose YAML (type=compose) or a container spec (type=container, "
            "spec in the Kowan ContainerSpec shape: image, env [{key,value}], ports [{hostPort,containerPort,protocol}], "
            "volumes [{source,target,readOnly}], restartPolicy, ...). deploy=true also deploys it right away."
        ),
        schema={
            "properties": {
                "name": {"type": "string", "minLength": 1, "maxLength": 128},
                "type": {"type": "string", "enum": ["compose", "container"]},
                "host": HOST_PROP,
                "strategy": {"type": "string", "enum": ["standalone", "swarm"]},
                "compose": {"type": "string", "description": "docker-compose YAML (type=compose)."},
                "spec": {"type": "object", "description": "Container spec (type=container)."},
                "deploy": {"type": "boolean", "description": "Deploy immediately (default false)."},
            },
            "required": ["name", "type"],
        },
        role=ROLE_ADMIN, read_only=False, destructive=False, idempotent=False,
    )
    async def create_deployment(ctx, args):
        host = resolve_host(ctx.svc, args.get("host"))
        body = {
            "name": args["name"], "type": args["type"], "strategy": args.get("strategy") or "standalone",
            "targets": [host.id], "compose": args.get("compose") or "", "spec": args.get("spec"),
        }
        dep = create_deployment_record(ctx.svc, body)
        out = {"deployment": sanitize_deployment(dep, _host_names(ctx.svc))}
        out.update(await _maybe_deploy(ctx.svc, dep, bool(args.get("deploy"))))
        return out

    @reg.tool(
        "deploy_template",
        title="Deploy app template",
        description=(
            "Create (and by default deploy) a deployment from an app template. inputs maps template input names to "
            "values (see list_templates); secret inputs left empty get a random value."
        ),
        schema={
            "properties": {
                "template": {"type": "string", "minLength": 1, "description": "Template id (e.g. builtin:nginx)."},
                "name": {"type": "string", "minLength": 1, "maxLength": 128, "description": "Deployment name (default: template name)."},
                "host": HOST_PROP,
                "inputs": {"type": "object", "additionalProperties": {"type": ["string", "integer", "number", "boolean"]}},
                "deploy": {"type": "boolean", "description": "Deploy immediately (default true)."},
            },
            "required": ["template"],
        },
        role=ROLE_ADMIN, read_only=False, destructive=False, idempotent=False,
    )
    async def deploy_template(ctx, args):
        from ...templates import TemplateError, find_builtin, render

        tid = args["template"]
        t = find_builtin(tid)
        if t is None:
            try:
                t = ctx.svc.store.get_template(tid)
            except Exception:  # noqa: BLE001
                raise ToolError(f"template {tid!r} not found; call list_templates") from None
        host = resolve_host(ctx.svc, args.get("host"))
        values = {k: ("" if v is None else str(v)) for k, v in (args.get("inputs") or {}).items()}
        generated = []
        for inp in t.inputs:
            if inp.secret and not values.get(inp.name) and not inp.default:
                values[inp.name] = _secrets.token_urlsafe(18)
                generated.append(inp.name)
        name = (args.get("name") or t.name).strip()
        try:
            rendered = render(t, values, name=name)
        except TemplateError as err:
            raise ToolError(str(err)) from err
        body = {"name": name, "type": rendered["type"], "strategy": "standalone", "targets": [host.id],
                "spec": rendered.get("spec"), "compose": rendered.get("compose") or ""}
        dep = create_deployment_record(ctx.svc, body)
        out: Dict[str, Any] = {"deployment": sanitize_deployment(dep, _host_names(ctx.svc)), "template": t.id}
        if generated:
            out["generatedSecrets"] = generated
            out["generatedNote"] = "random values were generated for these secret inputs; they are not shown"
        out.update(await _maybe_deploy(ctx.svc, dep, args.get("deploy", True) is not False))
        return out
