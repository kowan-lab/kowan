"""Deployment REST API.

Provides CRUD for deployment records, the lifecycle actions
(deploy/redeploy/stop/start/destroy), status/services reporting, the WebSocket
log stream, the deletion-protection toggle, redeploy-webhook management, and the public
``/hooks/deploy/{token}`` receiver.

Listing and reading are available to any authenticated user; create/update/
delete and all lifecycle actions require at least the ``operator`` role. The
public webhook authenticates by possession of its secret path token.

``register(app, svc)`` constructs the :class:`DeploymentService` once and
attaches it as ``svc.deployments`` so the log/hook handlers can reach it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
import re
from typing import Any, Dict, List, Optional

from aiohttp import web

from kowan.auth import require_role
from kowan.auth.middleware import user_from
from kowan.deployment import envsecrets, previews
from kowan.deployment import promotion
from kowan.deployment import revisions as revs
from kowan.redact import MASK, redact_compose, strip_userinfo
from kowan.auth import authorize as authz
from kowan.deployment.hook import handle_redeploy_hook
from kowan.deployment.logs import handle_logs
from kowan.deployment.service import (
    STATUS_DEPLOYED,
    STATUS_STOPPED,
    DeploymentService,
    _generate_redeploy_token,
    _now_rfc3339,
)
from kowan.errors import Conflict, NotFound, Validation
from kowan.deployment.gitsource import ref_matches as gitsource_ref_matches, validate_repo_url
from kowan.api.compose_validate import deployment_gate
from kowan.models import (
    ContainerSpec,
    Deployment,
    GitSource,
    DeploymentStrategyStandalone,
    DeploymentStrategySwarm,
    DeploymentTypeCompose,
    DeploymentTypeContainer,
)
from kowan.models.deployment import KeyValue
from kowan.web import (
    SERVICES_KEY,
    add_public_route,
    api_route,
    json_response,
    read_json,
    reject_unknown_fields,
)

log = logging.getLogger("kowan.api.deployments")


_RECONCILE_TASK_KEY = web.AppKey("deployment_reconcile_task", object)

# A stack variable name, as compose interpolation reads it.
_VAR_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

def register(app: web.Application, svc) -> None:
    """Mount the deployment routes and attach the DeploymentService singleton.

    Read routes (list/get/status/services/logs) are open to any authenticated
    principal; every mutating/lifecycle route requires ``operator`` (admins are
    always allowed). The redeploy webhook is mounted PUBLIC.
    """
    dep_svc = DeploymentService(svc)
    svc.attach("deployments", dep_svc)

    api_route(app, "GET", "/deployments", _list)
    api_route(app, "GET", "/deployments/{id}", _get)
    api_route(app, "GET", "/deployments/{id}/status", _status)
    api_route(app, "GET", "/deployments/{id}/services", _services)
    api_route(app, "GET", "/deployments/{id}/logs", handle_logs)  # websocket
    api_route(app, "GET", "/deployments/{id}/revisions", _revisions)
    api_route(app, "GET", "/deployments/{id}/revisions/{n}", _revision)
    api_route(app, "GET", "/deployments/{id}/revisions/{n}/diff", _revision_diff)
    api_route(app, "GET", "/deployments/{id}/previews", _list_previews)
    api_route(app, "GET", "/deployments/{id}/promote", _promote_plan)

    op = require_role("operator")
    api_route(app, "POST", "/deployments", op(_create))
    api_route(app, "PUT", "/deployments/{id}", op(_update))
    api_route(app, "PUT", "/deployments/{id}/protection", op(_protection))
    api_route(app, "PUT", "/deployments/{id}/enabled", op(_enabled))
    api_route(app, "DELETE", "/deployments/{id}", op(_delete))

    api_route(app, "POST", "/deployments/{id}/deploy", op(_deploy))
    api_route(app, "POST", "/deployments/{id}/redeploy", op(_deploy))
    api_route(app, "POST", "/deployments/{id}/stop", op(_stop))
    api_route(app, "POST", "/deployments/{id}/start", op(_start))
    api_route(app, "POST", "/deployments/{id}/destroy", op(_destroy))
    api_route(app, "POST", "/deployments/{id}/sync", op(_sync))
    api_route(app, "POST", "/deployments/{id}/revisions/{n}/rollback", op(_rollback))
    api_route(app, "POST", "/deployments/{id}/previews", op(_create_preview))
    api_route(app, "PUT", "/deployments/{id}/promotion", op(_set_promotion))
    api_route(app, "POST", "/deployments/{id}/promote", op(_promote))

    api_route(app, "POST", "/deployments/{id}/webhook", op(_create_webhook))
    api_route(app, "DELETE", "/deployments/{id}/webhook", op(_delete_webhook))

    # Public redeploy hook (token in the path authenticates). Registered as a
    # specific /hooks/deploy prefix so it takes precedence over the generic
    # /hooks catch-all mounted by add_webhooks.
    add_public_route(
        app, "POST", "/hooks/deploy/{token}", handle_redeploy_hook
    )

    # Desired-state reconcile: run a background loop that keeps ENABLED deployments
    # live (redeploys a down/missing target). Started on app startup and cancelled
    # on cleanup so it shares the server's event loop and shuts down cleanly.
    async def _start_reconcile(_app: web.Application) -> None:
        _app[_RECONCILE_TASK_KEY] = asyncio.ensure_future(
            dep_svc.run_reconcile_loop()
        )

    async def _stop_reconcile(_app: web.Application) -> None:
        task = _app.get(_RECONCILE_TASK_KEY)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    app.on_startup.append(_start_reconcile)
    app.on_cleanup.append(_stop_reconcile)

    # Preview environments: created by webhooks or by hand, expired when idle.
    preview_mgr = previews.PreviewManager(svc)
    svc.attach("previews", preview_mgr)

    async def _start_previews(_app: web.Application) -> None:
        preview_mgr.start()

    async def _stop_previews(_app: web.Application) -> None:
        await preview_mgr.stop()

    app.on_startup.append(_start_previews)
    app.on_cleanup.append(_stop_previews)


def _svc(request: web.Request):
    return request.app[SERVICES_KEY]


def _dep_svc(request: web.Request) -> DeploymentService:
    return request.app[SERVICES_KEY].deployments


def _load(svc, dep_id: str) -> Deployment:
    """Fetch a deployment or raise :class:`NotFound` (=> 404)."""
    try:
        dep = svc.store.get_deployment(dep_id)
    except Exception as err:  # store raises store.NotFoundError
        raise NotFound("deployment not found") from err
    # Team/grant scoping: level follows the request (read / operate / manage).
    authz.check_deployment(dep)
    return dep


# ---------------------------------------------------------------------------
# request parsing / validation
# ---------------------------------------------------------------------------


class _Request:
    """The mutable fields accepted by create and update."""

    __slots__ = ("name", "type", "strategy", "targets", "replicas", "spec", "compose", "git", "variables")

    def __init__(self, body: dict) -> None:
        self.name = str(body.get("name") or "").strip()
        self.type = str(body.get("type") or "").strip()
        self.strategy = str(body.get("strategy") or "").strip()
        raw_targets = body.get("targets") or []
        self.targets = [str(t) for t in raw_targets] if isinstance(raw_targets, list) else []
        try:
            self.replicas = int(body.get("replicas") or 0)
        except (TypeError, ValueError):
            self.replicas = 0
        spec_raw = body.get("spec")
        self.spec: Optional[ContainerSpec] = None
        if spec_raw is not None:
            self.spec = ContainerSpec.model_validate(spec_raw)
            self.spec.image = (self.spec.image or "").strip()
        self.compose = str(body.get("compose") or "").strip()
        # Optional git source (compose only). A null / missing / empty-repoUrl git
        # object means "inline YAML".
        git_raw = body.get("git")
        self.git: Optional[GitSource] = None
        if isinstance(git_raw, dict) and str(git_raw.get("repoUrl") or "").strip():
            g = GitSource.model_validate(git_raw)
            g.repo_url = g.repo_url.strip()
            g.ref = (g.ref or "").strip() or "main"
            g.path = (g.path or "").strip().lstrip("/") or "docker-compose.yml"
            g.credential_id = (g.credential_id or "").strip()
            g.webhook_secret = ""  # server-generated, never client-set
            self.git = g
        # Stack variables (compose): rows with an empty name are dropped.
        raw_vars = body.get("variables") or []
        self.variables: List[KeyValue] = []
        if isinstance(raw_vars, list):
            for v in raw_vars:
                if isinstance(v, dict) and str(v.get("key") or "").strip():
                    self.variables.append(KeyValue(key=str(v["key"]).strip(), value=str(v.get("value") or ""),
                                                   secret=v.get("secret") is True))

    def env(self) -> Dict[str, str]:
        return {v.key: v.value for v in self.variables}


def _validate(req: _Request) -> str:
    """Return a non-empty human-readable message when invalid, else ""."""
    if req.name == "":
        return "name is required"
    if req.type not in (DeploymentTypeContainer, DeploymentTypeCompose):
        return 'type must be "container" or "compose"'
    if req.strategy not in (DeploymentStrategyStandalone, DeploymentStrategySwarm):
        return 'strategy must be "standalone" or "swarm"'

    if req.type == DeploymentTypeContainer:
        if req.spec is None or req.spec.image == "":
            return "container deployments require spec.image"
    if req.git is not None:
        if req.type != DeploymentTypeCompose:
            return "a git source is only supported for compose deployments"
        try:
            validate_repo_url(req.git.repo_url)
        except ValueError as err:
            return str(err)
        if ".." in req.git.path.split("/"):
            return "git.path must stay inside the repository"
        if req.git.previews is not None:
            msg = previews.validate_settings(req.git.previews)
            if msg:
                return msg
            if req.git.previews.enabled and req.strategy != DeploymentStrategyStandalone:
                return "preview environments need a standalone deployment"
    if req.type == DeploymentTypeCompose and req.compose == "" and req.git is None:
        return "compose deployments require a compose file or a git source"
    if req.variables:
        if req.type != DeploymentTypeCompose:
            return "stack variables are only for compose deployments; containers have spec.env"
        seen = set()
        for v in req.variables:
            if not _VAR_NAME.match(v.key):
                return f"variable name {v.key!r} must be letters, digits and _ (not starting with a digit)"
            if v.key in seen:
                return f"variable {v.key} is set twice"
            seen.add(v.key)

    # Both strategies run on a single host: standalone on one Docker host,
    # swarm on one manager. Fan-out across many hosts was removed — a named
    # volume/bind mount is host-local, so "same app on N hosts" silently
    # diverges into N independent copies; that footgun is designed out by
    # constraining a deployment to exactly one host.
    if req.strategy == DeploymentStrategyStandalone:
        if len(req.targets) != 1:
            return "standalone deployments require exactly one target host"
    elif req.strategy == DeploymentStrategySwarm:
        if len(req.targets) != 1:
            return "swarm deployments require exactly one target host (the manager)"
    return ""


async def _read_request(request: web.Request) -> _Request:
    body = await read_json(request)
    if not isinstance(body, dict):
        raise Validation("invalid request body")
    return _Request(body)


def _check_resource_name_collision(svc, name: str, exclude_id: str) -> str:
    """Non-empty message when ``name`` sanitizes to the same resource_name() as
    an existing deployment other than ``exclude_id``.

    Distinct names can collapse to the same compose project label (e.g.
    "prod-app" and "prod app"), which would make label-based lookups cross-match.
    """
    want = Deployment(name=name).resource_name()
    try:
        deps = svc.store.list_deployments()
    except Exception as err:  # noqa: BLE001
        log.error("list deployments for resource-name check error=%s", err)
        return ""
    for d in deps:
        if d.id == exclude_id:
            continue
        if d.resource_name() == want:
            return "another deployment resolves to the same resource name"
    return ""


# ---------------------------------------------------------------------------
# read handlers
# ---------------------------------------------------------------------------


def _for_reader(request: web.Request, dep):
    """Hide from viewers what would let them act or read secrets: the redeploy
    token and git push secret (anonymous redeploys) and environment values."""
    if not authz.deployment_allowed(dep, authz.LEVEL_OPERATE):
        dep.redeploy_token = ""
        if dep.git is not None:
            dep.git.webhook_secret = ""
            dep.git.repo_url = strip_userinfo(dep.git.repo_url)
        # Environment values are where passwords live; extra docker run
        # arguments can carry them too (-e KEY=VALUE).
        if dep.spec is not None:
            for kv in dep.spec.env or []:
                kv.value = MASK
            dep.spec.extra_args = [MASK for _ in dep.spec.extra_args or []]
        if dep.compose:
            dep.compose = redact_compose(dep.compose)
        for kv in dep.variables:
            kv.value = MASK
    return dep


async def _list(request: web.Request) -> web.Response:
    """GET /deployments"""
    svc = _svc(request)
    deps = authz.filter_deployments(svc.store.list_deployments())
    return json_response([_for_reader(request, d) for d in deps or []])


async def _get(request: web.Request) -> web.Response:
    """GET /deployments/{id}"""
    dep = _load(_svc(request), request.match_info["id"])
    return json_response(_for_reader(request, dep))


async def _status(request: web.Request) -> web.Response:
    """GET /deployments/{id}/status -> [DeploymentTarget]"""
    svc = _svc(request)
    dep_id = request.match_info["id"]
    _load(svc, dep_id)  # 404 precheck
    targets = await _dep_svc(request).status(dep_id)
    return json_response(targets or [])


async def _services(request: web.Request) -> web.Response:
    """GET /deployments/{id}/services?host={hostID} -> [DeploymentService]"""
    svc = _svc(request)
    dep_id = request.match_info["id"]
    dep = _load(svc, dep_id)

    host_id = request.query.get("host", "")
    if host_id == "":
        if not dep.targets:
            raise Validation("deployment has no target hosts")
        host_id = dep.targets[0]

    services = await _dep_svc(request).services(dep_id, host_id)
    return json_response(services or [])


# ---------------------------------------------------------------------------
# create / update / protection / delete
# ---------------------------------------------------------------------------


def _unknown_target(svc, req: _Request) -> str:
    """A deployment must point at a host that exists, or it only fails at deploy."""
    for host_id in req.targets:
        try:
            svc.store.get_host(host_id)
        except Exception:  # noqa: BLE001 - NotFoundError
            return f"unknown host {host_id!r}"
    return ""


async def _create(request: web.Request) -> web.Response:
    """POST /deployments — creates the record only; it does not deploy."""
    out = await create_from_body(_svc(request), await read_json(request), author=_author(request))
    if isinstance(out, web.Response):
        return out
    return json_response(out, status=201)


async def create_from_body(svc, body: Any, *, author: str, source: str = "create") -> "Deployment | web.Response":
    """Validate a create body and store the deployment (not deployed).

    Returns the deployment, or the compose gate's 400 response. Raises
    Validation / Conflict / Forbidden like the route. Also used by imports.
    """
    if not isinstance(body, dict):
        raise Validation("invalid request body")
    req = _Request(body)
    msg = _validate(req) or _unknown_target(svc, req)
    if msg:
        raise Validation(msg)
    gate = await deployment_gate(svc, type_=req.type, compose=req.compose, has_git=req.git is not None,
                                 targets=req.targets, project=Deployment(name=req.name).resource_name(),
                                 env=req.env())
    if gate is not None:
        return gate

    # Enforce the unique-name constraint up front for a friendly error.
    try:
        svc.store.get_deployment_by_name(req.name)
        raise Conflict("a deployment with that name already exists")
    except Conflict:
        raise
    except Exception:
        pass  # NotFoundError => name is free
    # Two distinct names can sanitize to the same resource_name(); reject that.
    coll = _check_resource_name_collision(svc, req.name, "")
    if coll:
        raise Conflict(coll)

    now = _now_rfc3339()
    dep = Deployment(
        id=str(uuid.uuid4()),
        name=req.name,
        type=req.type,
        strategy=req.strategy,
        targets=req.targets,
        replicas=req.replicas,
        spec=req.spec,
        compose=req.compose,
        variables=req.variables,
        status=STATUS_STOPPED,
        createdAt=now,
        updatedAt=now,
    )
    if req.git is not None:
        # A git deployment gets its webhook ready: the path token (URL) plus the
        # HMAC / GitLab-token secret used to verify push payloads.
        req.git.webhook_secret = _generate_redeploy_token() + _generate_redeploy_token()
        dep.git = req.git
        dep.redeploy_token = _generate_redeploy_token()
    authz.check_deployment(dep, authz.LEVEL_MANAGE)
    envsecrets.apply(svc.store, dep, now)
    try:
        svc.store.create_deployment(dep, author=author, source=source)
    except Exception:
        envsecrets.delete_for(svc.store, dep.id)
        raise
    return dep


def _runtime_signature(dep: Deployment) -> tuple:
    """A tuple of the fields that affect what actually runs (image/compose/targets/
    …). Used to detect drift on update so a config change can force a redeploy."""
    spec = dep.spec.model_dump(by_alias=True) if dep.spec else None
    git = (
        (dep.git.repo_url, dep.git.ref, dep.git.path, dep.git.credential_id)
        if dep.git is not None
        else None
    )
    return (
        dep.type,
        dep.strategy,
        tuple(dep.targets or []),
        # A git deployment's compose mirrors the repo, so the source itself is
        # what matters (an edit of the mirror is not a config change).
        dep.compose if git is None else "",
        json.dumps(spec, sort_keys=True) if spec is not None else "",
        git,
        tuple((v.key, v.value) for v in dep.variables),
    )


async def _update(request: web.Request) -> web.Response:
    """PUT /deployments/{id} — update the record, and if a runtime-affecting field
    changed on an enabled+deployed deployment, force a background redeploy so the
    running state converges to the new config (desired-state 'force on update')."""
    svc = _svc(request)
    dep_id = request.match_info["id"]
    dep = _load(svc, dep_id)

    req = await _read_request(request)
    msg = _validate(req) or _unknown_target(svc, req)
    if msg:
        raise Validation(msg)
    gate = await deployment_gate(svc, type_=req.type, compose=req.compose, has_git=req.git is not None,
                                 targets=req.targets, deployment_id=dep_id, project=dep.resource_name(),
                                 env=req.env())
    if gate is not None:
        return gate

    before = _runtime_signature(dep)

    # A renamed deployment must not collide with another record.
    if req.name != dep.name:
        try:
            existing = svc.store.get_deployment_by_name(req.name)
            if existing.id != dep_id:
                raise Conflict("a deployment with that name already exists")
        except Conflict:
            raise
        except Exception:
            pass
    coll = _check_resource_name_collision(svc, req.name, dep_id)
    if coll:
        raise Conflict(coll)

    dep.name = req.name
    dep.type = req.type
    dep.strategy = req.strategy
    dep.targets = req.targets
    dep.replicas = req.replicas
    dep.spec = req.spec
    dep.variables = req.variables
    had_git = dep.git is not None
    if req.git is not None:
        # Keep the existing push secret; mint one when git is newly added.
        req.git.webhook_secret = (
            dep.git.webhook_secret if dep.git is not None and dep.git.webhook_secret
            else _generate_redeploy_token() + _generate_redeploy_token()
        )
        if not dep.redeploy_token:
            dep.redeploy_token = _generate_redeploy_token()
        # The compose text of a git deployment is a server-side mirror.
        dep.compose = dep.compose if had_git else ""
    else:
        dep.compose = req.compose
        dep.deployed_commit = ""
        dep.last_sync_at = ""
    dep.git = req.git
    dep.updated_at = _now_rfc3339()
    authz.check_deployment(dep, authz.LEVEL_MANAGE)  # new targets must be in scope too
    secrets_changed = envsecrets.apply(svc.store, dep, dep.updated_at)

    svc.store.update_deployment(dep, author=_author(request), source="edit")
    if had_git and dep.git is None:
        _dep_svc(request).remove_git_checkout(dep_id)

    # Force on update: a runtime-affecting change to an enabled, currently-deployed
    # deployment redeploys in the background so the running state converges to the
    # new config. Paused (disabled) or not-yet-deployed records just save.
    drifted = _runtime_signature(dep) != before or secrets_changed
    if drifted and dep.enabled and dep.status == STATUS_DEPLOYED:
        try:
            dep = await _dep_svc(request).dispatch_deploy(dep_id, redeploy=True)
        except Conflict:
            pass  # a deploy is already applying the change
    return json_response(dep)


async def _protection(request: web.Request) -> web.Response:
    """PUT /deployments/{id}/protection — toggle deletion protection."""
    svc = _svc(request)
    dep_id = request.match_info["id"]
    dep = _load(svc, dep_id)

    body = await read_json(request)
    if not isinstance(body, dict):
        raise Validation("invalid request body")
    dep.protected = bool(body.get("protected"))
    dep.updated_at = _now_rfc3339()
    svc.store.update_deployment(dep)
    return json_response(dep)


async def _enabled(request: web.Request) -> web.Response:
    """PUT /deployments/{id}/enabled — toggle the desired-state (keep-live) switch.

    Enabling a deployment that is currently down does NOT auto-deploy it here; the
    reconcile loop picks it up (or the operator deploys manually). Disabling simply
    pauses reconcile for it."""
    svc = _svc(request)
    dep_id = request.match_info["id"]
    dep = _load(svc, dep_id)

    body = await read_json(request)
    if not isinstance(body, dict):
        raise Validation("invalid request body")
    dep.enabled = bool(body.get("enabled"))
    dep.updated_at = _now_rfc3339()
    svc.store.update_deployment(dep)
    return json_response(dep)


async def _delete(request: web.Request) -> web.Response:
    """DELETE /deployments/{id} — best-effort destroy, then remove the record."""
    svc = _svc(request)
    dep_id = request.match_info["id"]
    dep = _load(svc, dep_id)

    if dep.protected:
        raise Conflict("deletion protection is enabled; disable it before deleting")

    try:
        await _dep_svc(request).destroy(dep_id)
    except Exception as derr:  # noqa: BLE001
        # Log but proceed: the record should still be removable even if the
        # runtime teardown failed (e.g. an unreachable host).
        log.warning(
            "destroy before delete failed deployment=%s name=%s err=%s",
            dep_id, dep.name, derr,
        )

    svc.store.delete_deployment(dep_id)
    _dep_svc(request).remove_git_checkout(dep_id)
    # Its previews go with it.
    await svc.previews.remove_children(dep_id)
    return json_response({"status": "deleted"})


# ---------------------------------------------------------------------------
# preview environments
# ---------------------------------------------------------------------------


async def _list_previews(request: web.Request) -> web.Response:
    """GET /deployments/{id}/previews"""
    svc = _svc(request)
    parent = _load(svc, request.match_info["id"])
    return json_response([_for_reader(request, d) for d in svc.previews.list(parent.id)])


async def _create_preview(request: web.Request) -> web.Response:
    """POST /deployments/{id}/previews {branch} — create (or redeploy) the
    preview of a branch."""
    svc = _svc(request)
    parent = _load(svc, request.match_info["id"])
    body = await read_json(request)
    if not isinstance(body, dict):
        raise Validation("invalid request body")
    reject_unknown_fields(body, {"branch"})
    branch = str(body.get("branch") or "").strip()
    if not branch:
        raise Validation("branch is required")
    if parent.preview is not None:
        raise Validation("a preview cannot have previews")
    if parent.git is not None and gitsource_ref_matches(parent.git.ref, branch):
        raise Validation("that is the branch the deployment itself runs")
    dep = await svc.previews.upsert(parent, branch, author=_author(request))
    return json_response(_for_reader(request, dep), status=201)


# ---------------------------------------------------------------------------
# lifecycle actions
# ---------------------------------------------------------------------------


async def _run_lifecycle(request: web.Request, action: str, method_name: str) -> web.Response:
    """Load the deployment, run the lifecycle op, respond with the reloaded record.

    On failure it reloads the (persisted,
    error) record and returns 500 with ``{deployment, error}``; on success it
    returns 200 with ``{deployment}``.
    """
    svc = _svc(request)
    dep_id = request.match_info["id"]
    _load(svc, dep_id)  # 404 precheck

    dep_svc = _dep_svc(request)
    fn = getattr(dep_svc, method_name)
    try:
        await fn(dep_id)
    except Exception as err:  # noqa: BLE001
        log.error(
            "deployment lifecycle failed action=%s deployment=%s err=%s",
            action, dep_id, err,
        )
        dep = _reload(svc, dep_id)
        return json_response({"deployment": dep, "error": str(err)}, status=500)

    dep = _reload(svc, dep_id)
    return json_response({"deployment": dep})


def _reload(svc, dep_id: str) -> Optional[Deployment]:
    try:
        return svc.store.get_deployment(dep_id)
    except Exception:  # noqa: BLE001
        return None


async def _deploy(request: web.Request) -> web.Response:
    """POST /deployments/{id}/deploy and /redeploy.

    Non-blocking: the deploy is QUEUED to run in the background and the request
    returns 202 immediately with the deployment in its ``pending`` state. The
    client polls the record/status (both live-refetch) to watch it go
    pending -> deploying -> deployed/error, so pressing Deploy never blocks on an
    image pull. A deploy already in flight for this deployment yields 409.
    """
    svc = _svc(request)
    dep_id = request.match_info["id"]
    cur = _load(svc, dep_id)  # 404 precheck
    gate = await deployment_gate(svc, type_=cur.type, compose=cur.compose, has_git=cur.git is not None,
                                 targets=cur.targets, deployment_id=dep_id, project=cur.resource_name())
    if gate is not None:
        return gate
    redeploy = request.path.rstrip("/").endswith("redeploy")
    dep = await _dep_svc(request).dispatch_deploy(dep_id, redeploy=redeploy)
    return json_response({"deployment": dep}, status=202)


async def _stop(request: web.Request) -> web.Response:
    """POST /deployments/{id}/stop."""
    return await _run_lifecycle(request, "stop", "stop")


async def _start(request: web.Request) -> web.Response:
    """POST /deployments/{id}/start."""
    return await _run_lifecycle(request, "start", "start")


async def _destroy(request: web.Request) -> web.Response:
    """POST /deployments/{id}/destroy — remove running resources, keep the record."""
    return await _run_lifecycle(request, "destroy", "destroy")


# ---------------------------------------------------------------------------
# webhook management
# ---------------------------------------------------------------------------


async def _create_webhook(request: web.Request) -> web.Response:
    """POST /deployments/{id}/webhook — generate/rotate the redeploy token."""
    svc = _svc(request)
    dep_id = request.match_info["id"]
    dep = _load(svc, dep_id)

    token = _generate_redeploy_token()
    dep.redeploy_token = token
    out = {"token": token, "url": "/hooks/deploy/" + token}
    if dep.git is not None:
        # Rotating the hook also rotates the git push secret.
        dep.git.webhook_secret = _generate_redeploy_token() + _generate_redeploy_token()
        out["secret"] = dep.git.webhook_secret
    dep.updated_at = _now_rfc3339()
    svc.store.update_deployment(dep)
    return json_response(out)


async def _delete_webhook(request: web.Request) -> web.Response:
    """DELETE /deployments/{id}/webhook — clear the redeploy token."""
    svc = _svc(request)
    dep_id = request.match_info["id"]
    dep = _load(svc, dep_id)

    dep.redeploy_token = ""
    dep.updated_at = _now_rfc3339()
    svc.store.update_deployment(dep)
    return json_response({"status": "removed"})


# ---------------------------------------------------------------------------
# git source
# ---------------------------------------------------------------------------


async def _sync(request: web.Request) -> web.Response:
    """POST /deployments/{id}/sync — "Pull & redeploy" for a git-sourced deployment.

    Looks up the latest commit of the configured ref (``git ls-remote``) and queues
    a background redeploy when it differs from the deployed commit (or always with
    ``?force=1``). 202 ``{deployment, changed: true, commit}`` when a redeploy was
    queued, 200 ``{deployment, changed: false, commit}`` when already up to date.
    """
    svc = _svc(request)
    dep_id = request.match_info["id"]
    _load(svc, dep_id)  # 404 precheck
    force = request.query.get("force", "") in ("1", "true", "yes")
    dep, changed, commit = await _dep_svc(request).sync(dep_id, force=force)
    return json_response(
        {"deployment": dep, "changed": changed, "commit": commit},
        status=202 if changed else 200,
    )


# ---------------------------------------------------------------------------
# revisions: history, diff, rollback
# ---------------------------------------------------------------------------


def _author(request: web.Request) -> str:
    u = user_from(request)
    return getattr(u, "username", "") if u is not None else ""


def _revision_number(request: web.Request) -> int:
    try:
        n = int(request.match_info["n"])
    except ValueError:
        raise Validation("revision must be a number") from None
    if n < 1:
        raise Validation("revision must be a number")
    return n


def _readable(dep: Deployment, config: dict) -> dict:
    """Viewers get the snapshot without env values and secrets."""
    return config if authz.deployment_allowed(dep, authz.LEVEL_OPERATE) else revs.redact(config)


def _revision_meta(r: dict, prev: Optional[dict], live: int) -> dict:
    return {
        "number": r["number"],
        "createdAt": r["createdAt"],
        "author": r["author"],
        "source": r["source"],
        "note": r["note"],
        "deployedAt": r["deployedAt"],
        "live": r["number"] == live,
        "changes": revs.changed_keys(prev["config"] if prev else None, r["config"]),
    }


async def _revisions(request: web.Request) -> web.Response:
    """GET /deployments/{id}/revisions -> newest first, with what each changed
    and which one runs (the latest revision a deploy succeeded with)."""
    svc = _svc(request)
    dep = _load(svc, request.match_info["id"])
    rows = svc.store.list_deployment_revisions(dep.id)
    live = next((r["number"] for r in rows if r["deployedAt"]), 0)
    out = [_revision_meta(r, rows[i + 1] if i + 1 < len(rows) else None, live) for i, r in enumerate(rows)]
    return json_response(out)


def _get_revision(svc, dep_id: str, n: int) -> dict:
    try:
        return svc.store.get_deployment_revision(dep_id, n)
    except Exception as err:  # store.NotFoundError
        raise NotFound(f"revision {n} not found") from err


async def _revision(request: web.Request) -> web.Response:
    """GET /deployments/{id}/revisions/{n} -> the snapshot and its YAML."""
    svc = _svc(request)
    dep = _load(svc, request.match_info["id"])
    r = _get_revision(svc, dep.id, _revision_number(request))
    config = _readable(dep, r["config"])
    return json_response({**{k: v for k, v in r.items() if k != "config"}, "config": config, "yaml": revs.to_yaml(config)})


async def _revision_diff(request: web.Request) -> web.Response:
    """GET /deployments/{id}/revisions/{n}/diff?against={m}: line diff from
    revision m (default: the one before n) to n."""
    svc = _svc(request)
    dep = _load(svc, request.match_info["id"])
    n = _revision_number(request)
    to = _get_revision(svc, dep.id, n)
    against = request.query.get("against", "")
    if against:
        try:
            m = int(against)
        except ValueError:
            raise Validation("against must be a revision number") from None
        base: Optional[dict] = _get_revision(svc, dep.id, m)
    else:
        older = [r for r in svc.store.list_deployment_revisions(dep.id) if r["number"] < n]
        base = older[0] if older else None
    before = _readable(dep, base["config"]) if base else None
    after = _readable(dep, to["config"])
    return json_response({
        "from": base["number"] if base else 0,
        "to": n,
        "changes": revs.changed_keys(before, after),
        "lines": revs.line_diff(before, after),
    })


async def _rollback(request: web.Request) -> web.Response:
    """POST /deployments/{id}/revisions/{n}/rollback {deploy?: bool=true}

    Restores what revision n ran as a new revision (history is never
    rewritten) and redeploys it unless ``deploy`` is false. A git revision is
    pinned to the commit it ran, with auto-deploy off, so the next push does not
    undo the rollback.
    """
    svc = _svc(request)
    dep = _load(svc, request.match_info["id"])
    n = _revision_number(request)
    r = _get_revision(svc, dep.id, n)
    body: Any = await read_json(request) if request.content_length else {}
    if not isinstance(body, dict):
        raise Validation("invalid request body")
    deploy = body.get("deploy", True) is not False

    old_name = dep.name
    had_git = dep.git is not None
    pinned = revs.apply(dep, r["config"])
    if dep.name != old_name:
        coll = _check_resource_name_collision(svc, dep.name, dep.id)
        try:
            other = svc.store.get_deployment_by_name(dep.name)
            if other.id != dep.id:
                raise Conflict(f"another deployment is now called {dep.name!r}")
        except Conflict:
            raise
        except Exception:  # noqa: BLE001 - name is free
            pass
        if coll:
            raise Conflict(coll)
    unknown = [t for t in dep.targets if not _host_exists(svc, t)]
    if unknown:
        raise Validation(f"revision {n} targets a host that no longer exists: {', '.join(unknown)}")
    gate = await deployment_gate(svc, type_=dep.type, compose=dep.compose, has_git=dep.git is not None,
                                 targets=dep.targets, deployment_id=dep.id, project=dep.resource_name())
    if gate is not None:
        return gate
    authz.check_deployment(dep, authz.LEVEL_MANAGE)
    dep.updated_at = _now_rfc3339()
    note = f"rollback to #{n}" + (" (pinned to its commit)" if pinned else "")
    svc.store.update_deployment(dep, author=_author(request), source="rollback", note=note)
    if had_git and dep.git is None:
        _dep_svc(request).remove_git_checkout(dep.id)
    if deploy and dep.enabled:
        try:
            dep = await _dep_svc(request).dispatch_deploy(dep.id, redeploy=True)
        except Conflict:
            pass  # a deploy is already running; it picks up the restored config next
    return json_response(_for_reader(request, dep))


def _host_exists(svc, host_id: str) -> bool:
    try:
        svc.store.get_host(host_id)
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# promotion (staging -> production)
# ---------------------------------------------------------------------------


async def _set_promotion(request: web.Request) -> web.Response:
    """PUT /deployments/{id}/promotion {to} — the deployment this one promotes
    to ("" for none)."""
    svc = _svc(request)
    dep = _load(svc, request.match_info["id"])
    body = await read_json(request)
    if not isinstance(body, dict):
        raise Validation("invalid request body")
    reject_unknown_fields(body, {"to"})
    to = str(body.get("to") or "").strip()
    if to:
        target = _load(svc, to)
        promotion.plan(dep, target)  # raises when the two do not fit
    dep.promote_to = to
    dep.updated_at = _now_rfc3339()
    svc.store.update_deployment(dep)
    return json_response(_for_reader(request, dep))


def _promotion_target(request: web.Request, svc, source: Deployment, to: str) -> Deployment:
    to = to or source.promote_to
    if not to:
        raise Validation("pick the deployment to promote to")
    try:
        return svc.store.get_deployment(to)
    except Exception as err:  # noqa: BLE001
        raise NotFound("target deployment not found") from err


async def _promote_plan(request: web.Request) -> web.Response:
    """GET /deployments/{id}/promote?to= — what promoting would change in
    the target (default: the configured one), as changes and a YAML diff."""
    svc = _svc(request)
    source = _load(svc, request.match_info["id"])
    target = _promotion_target(request, svc, source, request.query.get("to", ""))
    authz.check_deployment(target, authz.LEVEL_READ)
    p = promotion.plan(source, target)
    before, after = revs.snapshot(target), revs.snapshot(p.target)
    if not authz.deployment_allowed(target, authz.LEVEL_OPERATE):
        before, after = revs.redact(before), revs.redact(after)
    return json_response({
        "target": {"id": target.id, "name": target.name},
        "changes": [c.as_dict() for c in p.changes],
        "notes": p.notes,
        "pinned": p.pinned,
        "diff": revs.line_diff(before, after),
    })


async def _promote(request: web.Request) -> web.Response:
    """POST /deployments/{id}/promote {to?, deploy?: bool=true} — apply the
    plan to the target as a new revision and redeploy it."""
    svc = _svc(request)
    source = _load(svc, request.match_info["id"])
    body: Any = await read_json(request) if request.content_length else {}
    if not isinstance(body, dict):
        raise Validation("invalid request body")
    reject_unknown_fields(body, {"to", "deploy"})
    target = _promotion_target(request, svc, source, str(body.get("to") or "").strip())
    authz.check_deployment(target, authz.LEVEL_MANAGE)
    p = promotion.plan(source, target)
    if not p.changes:
        raise Conflict(f"{target.name} already runs what {source.name} runs")
    dep = p.target
    gate = await deployment_gate(svc, type_=dep.type, compose=dep.compose, has_git=dep.git is not None,
                                 targets=dep.targets, deployment_id=dep.id, project=dep.resource_name())
    if gate is not None:
        return gate
    dep.updated_at = _now_rfc3339()
    what = ", ".join(f"{c.what} {c.after[:12] if c.what == 'commit' else c.after}" for c in p.changes)
    svc.store.update_deployment(dep, author=_author(request), source="promote", note=f"from {source.name}: {what}")
    if body.get("deploy", True) is not False and dep.enabled:
        try:
            dep = await _dep_svc(request).dispatch_deploy(dep.id, redeploy=True)
        except Conflict:
            pass  # a deploy is already running; it picks up the promoted config next
    return json_response(_for_reader(request, dep))
