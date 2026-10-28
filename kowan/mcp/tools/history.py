"""Deployment history tools: revisions and their diffs, rollback, branch
previews and promotion. Reading follows the REST rules (viewers get env
values and secrets redacted); rollback and promotion reuse the REST code."""

from __future__ import annotations

from typing import Any, Dict, Optional

from ...auth import authorize as authz
from ...deployment import promotion
from ...deployment import revisions as revs
from ..registry import ROLE_OPERATOR, ToolError, ToolRegistry
from ._common import DEPLOYMENT_PROP, OFFSET_PROP, limit_prop, page
from .fleet import _host_names, deployment_row, resolve_deployment

YAML_MAX_CHARS = 16 * 1024
DIFF_MAX_LINES = 400

REVISION_PROP = {"type": "integer", "minimum": 1, "description": "Revision number (see list_revisions)."}


def _author(ctx: Any) -> str:
    return f"{getattr(ctx.user, 'username', '') or 'mcp'} (mcp)"


def _cap_lines(lines: list) -> Dict[str, Any]:
    if len(lines) <= DIFF_MAX_LINES:
        return {"lines": lines}
    return {"lines": lines[:DIFF_MAX_LINES], "linesNote": f"diff cut to {DIFF_MAX_LINES} of {len(lines)} lines"}


def _target(svc: Any, source: Any, to: str) -> Any:
    to = to or source.promote_to
    if not to:
        raise ToolError(f"{source.name} has no promotion target; pass to=<deployment>")
    return resolve_deployment(svc, to)


def register(reg: ToolRegistry) -> None:
    @reg.tool(
        "list_revisions",
        title="List deployment revisions",
        description=(
            "A deployment's revisions, newest first: who changed it, when, from where (edit, rollback, promote, git), "
            "which settings changed and which revision is live (the last one a deploy succeeded with)."
        ),
        schema={"properties": {"deployment": DEPLOYMENT_PROP, "limit": limit_prop(20, 200), "offset": OFFSET_PROP},
                "required": ["deployment"]},
    )
    async def list_revisions(ctx, args):
        from ...api.deployments import _revision_meta

        d = resolve_deployment(ctx.svc, args["deployment"])
        rows = ctx.svc.store.list_deployment_revisions(d.id)
        live = next((r["number"] for r in rows if r["deployedAt"]), 0)
        metas = [_revision_meta(r, rows[i + 1] if i + 1 < len(rows) else None, live) for i, r in enumerate(rows)]
        out = page(metas, args, 20, 200)
        out["deployment"] = d.name
        out["live"] = live
        return out

    @reg.tool(
        "get_revision",
        title="Get deployment revision",
        description=(
            "One revision of a deployment as YAML plus the line diff from another revision (default: the one before "
            "it). Environment values and secrets are hidden unless you may operate the deployment."
        ),
        schema={"properties": {"deployment": DEPLOYMENT_PROP, "revision": REVISION_PROP,
                               "against": {**REVISION_PROP, "description": "Revision to diff from (default: the previous one)."}},
                "required": ["deployment", "revision"]},
    )
    async def get_revision(ctx, args):
        from ...api.deployments import _readable

        d = resolve_deployment(ctx.svc, args["deployment"])
        n = int(args["revision"])
        rows = ctx.svc.store.list_deployment_revisions(d.id)
        by_n = {r["number"]: r for r in rows}
        if n not in by_n:
            raise ToolError(f"revision {n} not found; call list_revisions")
        to = by_n[n]
        base: Optional[dict]
        if args.get("against"):
            base = by_n.get(int(args["against"]))
            if base is None:
                raise ToolError(f"revision {args['against']} not found")
        else:
            older = [r for r in rows if r["number"] < n]
            base = older[0] if older else None
        before = _readable(d, base["config"]) if base else None
        after = _readable(d, to["config"])
        y = revs.to_yaml(after)
        out: Dict[str, Any] = {
            "deployment": d.name, "number": n, "createdAt": to["createdAt"], "author": to["author"],
            "source": to["source"], "note": to["note"], "deployedAt": to["deployedAt"],
            "yaml": y[:YAML_MAX_CHARS],
            "diffFrom": base["number"] if base else 0,
            "changes": revs.changed_keys(before, after),
            **_cap_lines(revs.line_diff(before, after)),
        }
        if len(y) > YAML_MAX_CHARS:
            out["yamlNote"] = f"YAML cut to {YAML_MAX_CHARS} of {len(y)} characters"
        return out

    @reg.tool(
        "rollback_deployment",
        title="Roll back a deployment",
        description=(
            "Restore what an earlier revision ran, as a new revision (history is kept), and redeploy it unless "
            "deploy=false. A git deployment is pinned to that revision's commit with auto-deploy off, so the next "
            "push does not undo the rollback. Check get_revision first."
        ),
        schema={"properties": {"deployment": DEPLOYMENT_PROP, "revision": REVISION_PROP,
                               "deploy": {"type": "boolean", "description": "Redeploy right away (default true)."}},
                "required": ["deployment", "revision"]},
        role=ROLE_OPERATOR, read_only=False, destructive=False, idempotent=False,
    )
    async def rollback_deployment(ctx, args):
        from ...api.deployments import rollback_deployment as rollback

        d = resolve_deployment(ctx.svc, args["deployment"])
        n = int(args["revision"])
        dep = await rollback(ctx.svc, d, n, author=_author(ctx), deploy=args.get("deploy", True) is not False)
        return {"deployment": dep.name, "rolledBackTo": n, "deploying": args.get("deploy", True) is not False and dep.enabled,
                "state": deployment_row(dep, _host_names(ctx.svc)),
                "note": "the deploy runs in the background; call get_deployment to follow it"}

    @reg.tool(
        "list_previews",
        title="List branch previews",
        description="The preview deployments of a git deployment, one per branch or pull request, with their status.",
        schema={"properties": {"deployment": DEPLOYMENT_PROP}, "required": ["deployment"]},
    )
    async def list_previews(ctx, args):
        d = resolve_deployment(ctx.svc, args["deployment"])
        previews = getattr(ctx.svc, "previews", None)
        if previews is None:
            raise ToolError("previews are not available on this server")
        names = _host_names(ctx.svc)
        rows = []
        for p in previews.list(d.id):
            if not authz.deployment_allowed(p, authz.LEVEL_READ):
                continue
            row = deployment_row(p, names)
            if p.preview is not None:
                row["preview"] = {k: v for k, v in p.preview.model_dump(by_alias=True).items()
                                  if k in ("ref", "pullRequest", "title", "url", "lastActivityAt") and v}
            rows.append(row)
        return {"deployment": d.name, "items": rows}

    @reg.tool(
        "promotion_plan",
        title="Promotion plan",
        description=(
            "What promoting a deployment (staging) to another (production) would change in the target: image tags, "
            "git commit and the line diff. to defaults to the deployment's configured promotion target."
        ),
        schema={"properties": {"deployment": DEPLOYMENT_PROP,
                               "to": {"type": "string", "description": "Target deployment id or name."}},
                "required": ["deployment"]},
    )
    async def promotion_plan(ctx, args):
        source = resolve_deployment(ctx.svc, args["deployment"])
        target = _target(ctx.svc, source, args.get("to") or "")
        try:
            p = promotion.plan(source, target)
        except Exception as err:  # noqa: BLE001 - the two do not fit
            raise ToolError(str(err)) from err
        before, after = revs.snapshot(target), revs.snapshot(p.target)
        if not authz.deployment_allowed(target, authz.LEVEL_OPERATE):
            before, after = revs.redact(before), revs.redact(after)
        return {
            "from": source.name, "to": target.name,
            "changes": [c.as_dict() for c in p.changes], "notes": p.notes, "pinned": p.pinned,
            "upToDate": not p.changes,
            **_cap_lines(revs.line_diff(before, after)),
        }

    @reg.tool(
        "promote_deployment",
        title="Promote a deployment",
        description=(
            "Apply the promotion plan to the target deployment as a new revision and redeploy it unless deploy=false. "
            "Show the user promotion_plan first."
        ),
        schema={"properties": {"deployment": DEPLOYMENT_PROP,
                               "to": {"type": "string", "description": "Target deployment id or name."},
                               "deploy": {"type": "boolean", "description": "Redeploy right away (default true)."}},
                "required": ["deployment"]},
        role=ROLE_OPERATOR, read_only=False, destructive=False, idempotent=False,
    )
    async def promote_deployment(ctx, args):
        from ...api.deployments import promote_deployment as promote

        source = resolve_deployment(ctx.svc, args["deployment"])
        target = _target(ctx.svc, source, args.get("to") or "")
        deploy = args.get("deploy", True) is not False
        dep = await promote(ctx.svc, source, target, author=_author(ctx), deploy=deploy)
        return {"from": source.name, "to": dep.name, "deploying": deploy and dep.enabled,
                "state": deployment_row(dep, _host_names(ctx.svc))}
