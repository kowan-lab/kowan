"""GitOps REST API (admin only) + the public sync webhook.

Syncs
  GET    /gitops/kinds                      registered resource kinds
  GET    /gitops/syncs                      list (status, last commit, summary)
  POST   /gitops/syncs                      create -> includes webhookSecret once
  GET    /gitops/syncs/{id}                 one sync
  PUT    /gitops/syncs/{id}                 update settings
  DELETE /gitops/syncs/{id}                 delete (managed resources are KEPT,
                                            their markers released)
  POST   /gitops/syncs/{id}/refresh         fetch + plan -> sync with plan
  GET    /gitops/syncs/{id}/plan            latest plan
  POST   /gitops/syncs/{id}/apply           {commit?} apply the plan -> run
  GET    /gitops/syncs/{id}/runs            run history (no plan bodies)
  GET    /gitops/syncs/{id}/runs/{runId}    one run with plan + result
  GET    /gitops/syncs/{id}/managed         resources managed by the sync
  POST   /gitops/syncs/{id}/webhook         rotate token + secret

Export
  GET    /gitops/resources                  exportable resources (+ managedBy)
  POST   /gitops/export                     {resources?: [{kind,id}],
                                             format: "zip"|"json",
                                             push?: {syncId, branch, message}}

Public
  POST   /hooks/gitops/{token}              push webhook (HMAC / GitLab token)
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Dict, List, Optional, Tuple

from aiohttp import web

from kowan.auth import require_role
from kowan.deployment import gitsource
from kowan.errors import Conflict, NotFound, Validation
from kowan.gitops import kinds as all_kinds
from kowan.gitops.export import export_files, list_exportable, push_files, zip_bytes
from kowan.gitops.kinds import Context
from kowan.gitops.service import MIN_INTERVAL, GitOpsService, new_token
from kowan.web import SERVICES_KEY, add_public_route, api_route, json_response, read_json
from kowan.web.httpx import read_body

log = logging.getLogger("kowan.api.gitops")

_FIELDS = {"name", "repoUrl", "ref", "path", "credentialId", "intervalSec", "autoApply", "prune", "enabled"}


def validate_url(url: str) -> str:
    """Repo URL check (indirection so tests can allow file:// repos)."""
    return gitsource.validate_repo_url(url)


_LOOP_KEY = web.AppKey("gitops_loop", object)

def register(app: web.Application, svc: Any) -> None:
    gs = GitOpsService(svc)
    svc.attach("gitops", gs)
    admin = require_role("admin")
    api_route(app, "GET", "/gitops/kinds", admin(_kinds))
    api_route(app, "GET", "/gitops/syncs", admin(_list))
    api_route(app, "POST", "/gitops/syncs", admin(_create))
    api_route(app, "GET", "/gitops/syncs/{id}", admin(_get))
    api_route(app, "PUT", "/gitops/syncs/{id}", admin(_update))
    api_route(app, "DELETE", "/gitops/syncs/{id}", admin(_delete))
    api_route(app, "POST", "/gitops/syncs/{id}/refresh", admin(_refresh))
    api_route(app, "GET", "/gitops/syncs/{id}/plan", admin(_plan))
    api_route(app, "POST", "/gitops/syncs/{id}/apply", admin(_apply))
    api_route(app, "GET", "/gitops/syncs/{id}/runs", admin(_runs))
    api_route(app, "GET", "/gitops/syncs/{id}/runs/{runId}", admin(_run))
    api_route(app, "GET", "/gitops/syncs/{id}/managed", admin(_managed))
    api_route(app, "POST", "/gitops/syncs/{id}/webhook", admin(_rotate))
    api_route(app, "GET", "/gitops/resources", admin(_resources))
    api_route(app, "POST", "/gitops/export", admin(_export))
    add_public_route(app, "POST", "/hooks/gitops/{token}", _hook)

    async def _start(_app: web.Application) -> None:
        _app[_LOOP_KEY] = asyncio.ensure_future(gs.run_loop())

    async def _stop(_app: web.Application) -> None:
        t = _app.get(_LOOP_KEY)
        if t is not None:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass

    app.on_startup.append(_start)
    app.on_cleanup.append(_stop)


def _gs(request: web.Request) -> GitOpsService:
    return request.app[SERVICES_KEY].gitops


# ---------------------------------------------------------------------------
# serialisation
# ---------------------------------------------------------------------------


def sync_json(s: Dict[str, Any], *, secret: bool = False) -> Dict[str, Any]:
    plan = s.get("plan") or {}
    out = {
        "id": s["id"], "name": s["name"], "repoUrl": s["repo_url"], "ref": s.get("ref") or "main",
        "path": s.get("path") or "", "credentialId": s.get("credential_id") or "",
        "intervalSec": int(s.get("interval_sec") or 0), "autoApply": bool(s.get("auto_apply")),
        "prune": bool(s.get("prune")), "enabled": bool(s.get("enabled")),
        "webhookUrl": "/hooks/gitops/" + s["webhook_token"] if s.get("webhook_token") else "",
        "status": s.get("status") or "", "lastCommit": s.get("last_commit") or "",
        "lastCheckedAt": s.get("last_checked_at") or "", "lastAppliedCommit": s.get("last_applied_commit") or "",
        "lastAppliedAt": s.get("last_applied_at") or "", "lastError": s.get("last_error") or "",
        "summary": plan.get("summary") or None, "planErrors": len(plan.get("errors") or []),
        "createdAt": s.get("created_at") or "", "updatedAt": s.get("updated_at") or "",
    }
    if secret:
        out["webhookSecret"] = s.get("webhook_secret") or ""
    return out


def run_json(r: Dict[str, Any], *, full: bool = False) -> Dict[str, Any]:
    plan = r.get("plan") or {}
    out = {
        "id": r["id"], "syncId": r["sync_id"], "trigger": r["trigger"], "action": r["action"],
        "status": r["status"], "commit": r.get("commit_sha") or "", "error": r.get("error") or "",
        "startedAt": r["started_at"], "finishedAt": r.get("finished_at") or "",
        "summary": plan.get("summary") or None,
    }
    if full:
        out["plan"] = r.get("plan")
        out["result"] = r.get("result")
    return out


# ---------------------------------------------------------------------------
# body parsing
# ---------------------------------------------------------------------------


def _apply_body(s: Dict[str, Any], body: Dict[str, Any], *, create: bool) -> None:
    extra = set(body) - _FIELDS
    if extra:
        raise Validation(f"unknown field(s): {', '.join(sorted(extra))}")
    if create or "name" in body:
        name = str(body.get("name") or "").strip()
        if not name:
            raise Validation("name is required")
        s["name"] = name
    if create or "repoUrl" in body:
        try:
            s["repo_url"] = validate_url(str(body.get("repoUrl") or ""))
        except ValueError as err:
            raise Validation(str(err).replace("git.repoUrl", "repoUrl")) from err
    if create or "ref" in body:
        s["ref"] = str(body.get("ref") or "").strip() or "main"
    if create or "path" in body:
        path = str(body.get("path") or "").strip().strip("/")
        if ".." in path.split("/"):
            raise Validation("path must stay inside the repository")
        s["path"] = path
    if create or "credentialId" in body:
        s["credential_id"] = str(body.get("credentialId") or "").strip()
    if create or "intervalSec" in body:
        try:
            iv = int(body.get("intervalSec") or 0)
        except (TypeError, ValueError):
            raise Validation("intervalSec must be an integer")
        if iv < 0 or (0 < iv < MIN_INTERVAL):
            raise Validation(f"intervalSec must be 0 (manual/webhook only) or at least {MIN_INTERVAL}")
        s["interval_sec"] = iv
    for key, col, default in (("autoApply", "auto_apply", False), ("prune", "prune", True),
                              ("enabled", "enabled", True)):
        if create or key in body:
            v = body.get(key, default)
            s[col] = default if v is None else bool(v)


async def _body(request: web.Request) -> Dict[str, Any]:
    body = await read_json(request)
    if not isinstance(body, dict):
        raise Validation("invalid request body")
    return body


def _now() -> str:
    from kowan.web import now_rfc3339

    return now_rfc3339()


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------


async def _kinds(request: web.Request) -> web.Response:
    return json_response([h.describe() for h in all_kinds()])


async def _list(request: web.Request) -> web.Response:
    return json_response([sync_json(s) for s in _gs(request).store.list_gitops_syncs()])


async def _get(request: web.Request) -> web.Response:
    return json_response(sync_json(_gs(request).get(request.match_info["id"])))


def _name_taken(gs: GitOpsService, name: str, exclude: str = "") -> bool:
    return any(s["name"] == name and s["id"] != exclude for s in gs.store.list_gitops_syncs())


async def _create(request: web.Request) -> web.Response:
    gs = _gs(request)
    body = await _body(request)
    now = _now()
    s: Dict[str, Any] = {"id": str(uuid.uuid4()), "created_at": now, "updated_at": now,
                         "webhook_token": new_token(), "webhook_secret": new_token() + new_token()}
    _apply_body(s, body, create=True)
    if _name_taken(gs, s["name"]):
        raise Conflict("a sync with that name already exists")
    gs.store.create_gitops_sync(s)
    return json_response(sync_json(gs.get(s["id"]), secret=True), status=201)


async def _update(request: web.Request) -> web.Response:
    gs = _gs(request)
    s = gs.get(request.match_info["id"])
    body = await _body(request)
    before = (s["repo_url"], s.get("ref"), s.get("path"))
    _apply_body(s, body, create=False)
    if _name_taken(gs, s["name"], s["id"]):
        raise Conflict("a sync with that name already exists")
    if (s["repo_url"], s.get("ref"), s.get("path")) != before:
        # Source changed: the cached plan no longer describes this sync.
        s["plan"], s["status"], s["last_commit"] = None, "", ""
        gs.remove_checkout(s["id"])
    s["updated_at"] = _now()
    gs.store.update_gitops_sync(s)
    return json_response(sync_json(gs.get(s["id"])))


async def _delete(request: web.Request) -> web.Response:
    gs = _gs(request)
    s = gs.get(request.match_info["id"])
    gs.store.delete_gitops_sync(s["id"])
    gs.remove_checkout(s["id"])
    return json_response({"status": "deleted"})


async def _refresh(request: web.Request) -> web.Response:
    gs = _gs(request)
    s = await gs.refresh(request.match_info["id"], trigger="manual")
    out = sync_json(s)
    out["plan"] = s.get("plan")
    return json_response(out)


async def _plan(request: web.Request) -> web.Response:
    s = _gs(request).get(request.match_info["id"])
    return json_response({"sync": sync_json(s), "plan": s.get("plan")})


async def _apply(request: web.Request) -> web.Response:
    gs = _gs(request)
    body: Dict[str, Any] = {}
    if request.can_read_body:
        raw = await read_json(request)
        body = raw if isinstance(raw, dict) else {}
    run = await gs.apply(request.match_info["id"], trigger="manual",
                         expected_commit=str(body.get("commit") or ""))
    return json_response({"run": run_json(run, full=True), "sync": sync_json(gs.get(request.match_info["id"]))})


async def _runs(request: web.Request) -> web.Response:
    gs = _gs(request)
    s = gs.get(request.match_info["id"])
    try:
        limit = int(request.query.get("limit") or 50)
    except ValueError:
        limit = 50
    return json_response([run_json(r) for r in gs.store.list_gitops_runs(s["id"], limit)])


async def _run(request: web.Request) -> web.Response:
    gs = _gs(request)
    try:
        r = gs.store.get_gitops_run(request.match_info["runId"])
    except Exception as err:  # noqa: BLE001
        raise NotFound("run not found") from err
    if r["sync_id"] != request.match_info["id"]:
        raise NotFound("run not found")
    return json_response(run_json(r, full=True))


async def _managed(request: web.Request) -> web.Response:
    gs = _gs(request)
    s = gs.get(request.match_info["id"])
    return json_response([
        {"kind": m["kind"], "resourceId": m["resource_id"], "name": m["name"], "hash": m["applied_hash"],
         "commit": m["commit_sha"], "updatedAt": m["updated_at"]}
        for m in gs.store.list_gitops_managed(s["id"])
    ])


async def _rotate(request: web.Request) -> web.Response:
    gs = _gs(request)
    s = gs.get(request.match_info["id"])
    s["webhook_token"] = new_token()
    s["webhook_secret"] = new_token() + new_token()
    s["updated_at"] = _now()
    gs.store.update_gitops_sync(s)
    return json_response(sync_json(s, secret=True))


async def _resources(request: web.Request) -> web.Response:
    svc = request.app[SERVICES_KEY]
    return json_response(list_exportable(Context(svc=svc)))


async def _export(request: web.Request) -> web.StreamResponse:
    svc = request.app[SERVICES_KEY]
    body = await _body(request)
    sel_raw = body.get("resources")
    selection: Optional[List[Tuple[str, str]]] = None
    if sel_raw is not None:
        if not isinstance(sel_raw, list):
            raise Validation("resources must be a list of {kind, id}")
        selection = []
        for item in sel_raw:
            if not isinstance(item, dict) or not item.get("kind") or not item.get("id"):
                raise Validation("resources must be a list of {kind, id}")
            selection.append((str(item["kind"]), str(item["id"])))
    files, warnings = export_files(Context(svc=svc), selection)
    push = body.get("push")
    if isinstance(push, dict):
        gs: GitOpsService = svc.gitops
        sync = gs.get(str(push.get("syncId") or ""))
        branch = str(push.get("branch") or "").strip()
        if not branch:
            raise Validation("push.branch is required")
        try:
            out = await push_files(
                repo_url=sync["repo_url"], base_ref=sync.get("ref") or "main", branch=branch,
                path=sync.get("path") or "", files=files,
                message=str(push.get("message") or "") or f"kowan: export {len(files)} resource(s)",
                auth=await gs.git_auth(sync),
            )
        except (ValueError, gitsource.GitError) as err:
            raise Validation(f"push failed: {err}") from err
        out.update({"files": len(files), "warnings": warnings})
        return json_response(out)
    fmt = str(body.get("format") or "zip")
    if fmt == "json":
        return json_response({"files": files, "warnings": warnings})
    if fmt != "zip":
        raise Validation('format must be "zip" or "json"')
    data = zip_bytes(files, prefix="kowan-export")
    return web.Response(body=data, content_type="application/zip",
                        headers={"Content-Disposition": 'attachment; filename="kowan-export.zip"',
                                 "X-Kowan-Export-Warnings": str(len(warnings))})


async def _hook(request: web.Request) -> web.Response:
    svc = request.app[SERVICES_KEY]
    gs: GitOpsService = svc.gitops
    token = request.match_info.get("token", "")
    try:
        sync = gs.store.get_gitops_sync_by_token(token)
    except Exception:  # noqa: BLE001
        return json_response({"error": "not found"}, status=404)
    body = await read_body(request, 1 << 20)
    d = gs.evaluate_webhook(sync, dict(request.headers), body)
    if d.action == "unauthorized":
        return json_response({"error": "signature mismatch"}, status=401)
    if d.action == "pong":
        return json_response({"status": "pong"})
    if not sync.get("enabled", True):
        return json_response({"status": "ignored", "reason": "sync is disabled"})
    if d.action == "ignore":
        return json_response({"status": "ignored", "reason": d.reason})
    gs.spawn(gs.refresh_and_maybe_apply(sync["id"], "webhook"))
    return json_response({"status": "queued", "sync": sync["id"]}, status=202)
