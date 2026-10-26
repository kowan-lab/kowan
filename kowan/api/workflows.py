"""Automation/workflow engine REST API + public run webhook.

Exposes CRUD on
workflows, manual run triggering, run history, per-workflow public webhook token
management, and the trigger-type catalog over the protected ``/api/v1`` surface.
A workflow is a named, ordered list of typed steps
executed sequentially by the workflow engine.

All mutations require at least the ``operator`` role and, as they change the
cron schedule, trigger a scheduler reload.

``register(app, svc)`` also constructs and starts the workflow ENGINE,
SCHEDULER and trigger POLLER and attaches them to ``svc`` so siblings can reach them:

  - ``svc.workflow_engine`` : :class:`kowan.workflow.engine.Engine`
  - ``svc.workflow_scheduler`` : :class:`kowan.workflow.scheduler.Scheduler`
  - ``svc.workflow_poller`` : :class:`kowan.workflow.poller.TriggerPoller`

The scheduler/poller loops are started on the aiohttp app startup signal (so an
event loop exists) and cancelled on cleanup.

Learner notes: the store is synchronous; ``engine.run`` is async (it detaches
the run itself). The public hook mounts at ``/hooks/workflow/{token}`` via
``add_public_route`` so it takes precedence over the generic ``/hooks``
catch-all.
"""

from __future__ import annotations

import asyncio
import logging
import secrets as _secrets
import uuid
from typing import List, Optional

from aiohttp import web

from kowan.auth import authorize as authz
from kowan.auth.middleware import user_from

from kowan import triggers
from kowan.errors import Conflict, Forbidden, NotFound, Validation
from kowan.models import (
    Workflow,
    WorkflowPayload,
    WorkflowStep,
    WorkflowStepBackup,
    WorkflowStepContainer,
    WorkflowStepDeploy,
    WorkflowStepHTTP,
    WorkflowStepNotify,
    WorkflowStepPrune,
    WorkflowStepRunContainer,
    WorkflowStepScript,
    WorkflowTrigger,
    WorkflowTriggerManual,
    WorkflowTriggerWebhook,
)
from kowan.triggers.builtin import webhook_method_matches
from kowan.web import (
    SERVICES_KEY,
    add_public_route,
    api_route,
    error_response,
    json_response,
    now_rfc3339,
    read_body,
    read_json,
    require_role,
)
from kowan.workflow.engine import Engine
from kowan.workflow.policy import script_rights_problem
from kowan.redact import MASK
from kowan.workflow.poller import TriggerPoller
from kowan.workflow.scheduler import Scheduler

log = logging.getLogger("kowan.api.workflows")

# Masked placeholder for a sensitive trigger config value (token, password, ...)
# on every response; an update that sends the mask back (or a blank) keeps the
# stored value.
_MASKED_TRIGGER_VALUE = "********"

# Bounds how much of the inbound webhook body is captured into the run payload.
_MAX_HOOK_BODY = 1 << 20

# The step types accepted by validate.
_VALID_STEP_TYPES = {
    WorkflowStepScript,
    WorkflowStepHTTP,
    WorkflowStepDeploy,
    WorkflowStepContainer,
    WorkflowStepNotify,
    WorkflowStepBackup,
    WorkflowStepRunContainer,
    WorkflowStepPrune,
}


_POLLER_TASK_KEY = web.AppKey("kowan_workflow_poller_task", object)

def register(app: web.Application, svc) -> None:
    """Mount the workflow routes, wire the engine/scheduler/poller, and mount the
    public run hook.

    Read routes (list/get/runs/trigger-types) are open to any authenticated
    principal; all mutations and run triggering require ``operator`` (admins are
    always allowed).
    """
    # Build the engine from the DI container (deployments/notify default from
    # svc). Attach it and the scheduler/poller so siblings and startup hooks can
    # reach them.
    engine = Engine.from_services(svc)
    scheduler = Scheduler(engine, svc.store)
    poller = TriggerPoller(engine, svc.store)
    svc.attach("workflow_engine", engine)
    svc.attach("workflow_scheduler", scheduler)
    svc.attach("workflow_poller", poller)

    # Read routes (viewer+).
    api_route(app, "GET", "/workflows", _handle_list)
    api_route(app, "GET", "/workflow-trigger-types", _handle_trigger_types)
    api_route(app, "GET", "/workflows/{id}", _handle_get)
    # Run history is operator+: it holds script output and HTTP responses,
    # which can carry secrets a viewer must not read.
    api_route(app, "GET", "/workflows/{id}/runs", require_role("operator")(_handle_list_runs))
    api_route(app, "GET", "/workflows/{id}/runs/{runId}", require_role("operator")(_handle_get_run))

    # Mutating routes (operator+).
    op = require_role("operator")
    api_route(app, "POST", "/workflows", op(_handle_create))
    api_route(app, "PUT", "/workflows/{id}", op(_handle_update))
    api_route(app, "PUT", "/workflows/{id}/protection", op(_handle_protection))
    api_route(app, "DELETE", "/workflows/{id}", op(_handle_delete))
    api_route(app, "POST", "/workflows/{id}/run", op(_handle_run))
    api_route(app, "POST", "/workflows/{id}/webhook", op(_handle_create_webhook))
    api_route(app, "DELETE", "/workflows/{id}/webhook", op(_handle_delete_webhook))

    # Public run hook (token in the path authenticates). Registered as a specific
    # /hooks/workflow prefix so it takes precedence over the generic /hooks
    # catch-all mounted by add_webhooks. All of GET/POST/PUT/PATCH/DELETE bind to
    # the same handler; the handler then gates on the webhook trigger's
    # configured method (see _handle_hook).
    for _hook_method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        add_public_route(app, _hook_method, "/hooks/workflow/{token}", _handle_hook)

    # Start the scheduler + poller once the event loop is running, and stop them
    # on shutdown.
    async def _on_startup(_app: web.Application) -> None:
        await scheduler.start()
        poller_task = asyncio.ensure_future(poller.start())
        _app[_POLLER_TASK_KEY] = poller_task

    async def _on_cleanup(_app: web.Application) -> None:
        await scheduler.stop()
        task = _app.get(_POLLER_TASK_KEY)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)


def _svc(request: web.Request):
    return request.app[SERVICES_KEY]


def _load(svc, workflow_id: str) -> Workflow:
    """Fetch a workflow or raise :class:`NotFound` (=> 404)."""
    try:
        wf = svc.store.get_workflow(workflow_id)
    except Exception as err:  # store raises store.NotFoundError
        raise NotFound("workflow not found") from err
    # Workflows act fleet-wide: scoped principals need an all-hosts grant.
    authz.check_global()
    return wf


# ---------------------------------------------------------------------------
# request parsing / normalize / validate
# ---------------------------------------------------------------------------


class _Request:
    """The mutable fields accepted by POST/PUT /workflows. Server-managed fields
    (id, runToken, timestamps) are not accepted."""

    __slots__ = ("name", "description", "steps", "triggers", "variables", "enabled")

    def __init__(self, body: dict) -> None:
        self.name = str(body.get("name") or "")
        self.description = str(body.get("description") or "")
        self.steps: List[WorkflowStep] = [
            WorkflowStep.model_validate(s) for s in (body.get("steps") or [])
        ]
        self.triggers: List[WorkflowTrigger] = [
            WorkflowTrigger.model_validate(t) for t in (body.get("triggers") or [])
        ]
        # Env-style run variables: a string->string map (default empty). Values
        # may hold [[secret]] references resolved at run start by the engine.
        self.variables: dict = {
            str(k): str(v) for k, v in (body.get("variables") or {}).items()
        }
        self.enabled = bool(body.get("enabled") or False)


def _normalize(req: _Request) -> None:
    """Trim whitespace from the workflow's string fields and each step's
    name/type; assign stable trigger ids and mint webhook tokens."""
    req.name = req.name.strip()
    req.description = req.description.strip()
    for step in req.steps:
        step.name = step.name.strip()
        step.type = step.type.strip()
        if step.config is None:
            step.config = {}
    for t in req.triggers:
        t.type = t.type.strip()
        if t.config is None:
            t.config = {}
        # Assign a stable id so the poller/merge can track a trigger across edits.
        if t.id == "":
            t.id = str(uuid.uuid4())
        # A webhook trigger needs a server-generated secret token; mint one the
        # first time (kept stable on subsequent saves).
        if t.type == triggers.TypeWebhook and t.config.get("token", "") == "":
            t.config["token"] = _generate_run_token()


def _validate(req: _Request) -> str:
    """Return a non-empty human-readable message when invalid, else ""."""
    if req.name == "":
        return "name is required"
    for i, step in enumerate(req.steps):
        if step.type not in _VALID_STEP_TYPES:
            return (
                f"step {i + 1}: type must be script, http, deploy, container, "
                "notify, backup, run-container or prune"
            )
    for i, t in enumerate(req.triggers):
        if not triggers.is_registered(t.type):
            return f"trigger {i + 1}: unknown type {t.type!r}"
        for f in triggers.fields(t.type):
            # A blank sensitive field on edit means "keep the stored value", so
            # it is not treated as missing here (mergeTriggers restores it).
            if f.required and not f.sensitive and (t.config.get(f.key) or "").strip() == "":
                return f"trigger {i + 1} ({t.type}): {f.key} is required"
    return ""


# ---------------------------------------------------------------------------
# read handlers
# ---------------------------------------------------------------------------


async def _handle_list(request: web.Request) -> web.StreamResponse:
    """GET /workflows"""
    svc = _svc(request)
    workflows = svc.store.list_workflows() if authz.global_allowed() else []
    for wf in workflows:
        _mask_triggers(wf)
        _hide_run_tokens(request, wf)
    return json_response(workflows)


async def _handle_trigger_types(request: web.Request) -> web.StreamResponse:
    """GET /workflow-trigger-types — the pluggable trigger catalog with each
    type's kind and config field schema, so the UI can render the trigger
    editor."""
    return json_response([s.to_dict() for s in triggers.specs()])


async def _handle_get(request: web.Request) -> web.StreamResponse:
    """GET /workflows/{id}"""
    svc = _svc(request)
    wf = _load(svc, request.match_info["id"])
    _mask_triggers(wf)
    _hide_run_tokens(request, wf)
    return json_response(wf)


async def _handle_list_runs(request: web.Request) -> web.StreamResponse:
    """GET /workflows/{id}/runs?limit=N — the run history, newest first."""
    svc = _svc(request)
    workflow_id = request.match_info["id"]
    _load(svc, workflow_id)  # 404 for an unknown workflow

    limit = 0
    v = request.query.get("limit")
    if v:
        try:
            limit = int(v)
        except ValueError:
            limit = 0

    runs = svc.store.list_runs(workflow_id, limit)
    return json_response(runs)


async def _handle_get_run(request: web.Request) -> web.StreamResponse:
    """GET /workflows/{id}/runs/{runId} — a single run with its per-step
    results."""
    svc = _svc(request)
    workflow_id = request.match_info["id"]
    run_id = request.match_info["runId"]
    try:
        run = svc.store.get_run(run_id)
    except Exception as err:
        raise NotFound("workflow run not found") from err
    # Guard against cross-workflow run id access.
    if run.workflow_id != workflow_id:
        raise NotFound("workflow run not found")
    return json_response(run)


# ---------------------------------------------------------------------------
# mutating handlers
# ---------------------------------------------------------------------------


def _require_script_rights(request: web.Request, *step_lists) -> None:
    """Script steps run bash on the control plane (kowan.workflow.policy)."""
    user = user_from(request)
    problem = script_rights_problem(user.role if user is not None else "", *step_lists)
    if problem:
        raise Forbidden(problem)


async def _handle_create(request: web.Request) -> web.StreamResponse:
    """POST /workflows — create the record and reload the scheduler."""
    svc = _svc(request)
    authz.check_global(authz.LEVEL_MANAGE)
    body = await read_json(request)
    req = _Request(body)
    _normalize(req)
    msg = _validate(req)
    if msg != "":
        raise Validation(msg)
    _require_script_rights(request, req.steps)

    now = now_rfc3339()
    wf = Workflow(
        id=str(uuid.uuid4()),
        name=req.name,
        description=req.description,
        steps=req.steps,
        triggers=req.triggers,
        variables=req.variables,
        enabled=req.enabled,
        created_at=now,
        updated_at=now,
    )
    svc.store.create_workflow(wf)
    await _reload(svc)
    _mask_triggers(wf)
    return json_response(wf, status=201)


async def _handle_update(request: web.Request) -> web.StreamResponse:
    """PUT /workflows/{id} — update the mutable fields and reload the scheduler.
    The run token, id, and created timestamp are preserved."""
    svc = _svc(request)
    wf = _load(svc, request.match_info["id"])
    body = await read_json(request)
    req = _Request(body)
    _normalize(req)
    msg = _validate(req)
    if msg != "":
        raise Validation(msg)
    _require_script_rights(request, wf.steps, req.steps)

    wf.name = req.name
    wf.description = req.description
    wf.steps = req.steps
    wf.triggers = _merge_triggers(wf.triggers, req.triggers)
    wf.variables = req.variables
    wf.enabled = req.enabled
    wf.updated_at = now_rfc3339()

    svc.store.update_workflow(wf)
    await _reload(svc)
    _mask_triggers(wf)
    return json_response(wf)


async def _handle_protection(request: web.Request) -> web.StreamResponse:
    """PUT /workflows/{id}/protection — toggle deletion protection."""
    svc = _svc(request)
    wf = _load(svc, request.match_info["id"])
    body = await read_json(request)
    wf.protected = bool(body.get("protected") or False)
    wf.updated_at = now_rfc3339()
    svc.store.update_workflow(wf)
    return json_response(wf)


async def _handle_delete(request: web.Request) -> web.StreamResponse:
    """DELETE /workflows/{id} — remove the workflow (and its run history) and
    reload the scheduler."""
    svc = _svc(request)
    wf = _load(svc, request.match_info["id"])
    if wf.protected:
        raise Conflict("deletion protection is enabled; disable it before deleting")
    svc.store.delete_workflow(wf.id)
    await _reload(svc)
    return json_response({"status": "deleted"})


async def _handle_run(request: web.Request) -> web.StreamResponse:
    """POST /workflows/{id}/run — trigger a manual, asynchronous run and return
    the new run id."""
    svc = _svc(request)
    workflow_id = request.match_info["id"]
    wf = _load(svc, workflow_id)  # 404 for an unknown workflow
    _require_script_rights(request, wf.steps)

    run_id = await svc.workflow_engine.run(workflow_id, WorkflowTriggerManual, None)
    return json_response({"runId": run_id}, status=202)


# ---------------------------------------------------------------------------
# per-workflow webhook token management
# ---------------------------------------------------------------------------


async def _handle_create_webhook(request: web.Request) -> web.StreamResponse:
    """POST /workflows/{id}/webhook — generate (or rotate) the workflow's public
    run token and return {token, url} where url is "/hooks/workflow/{token}"."""
    svc = _svc(request)
    wf = _load(svc, request.match_info["id"])
    _require_script_rights(request, wf.steps)
    token = _generate_run_token()
    wf.run_token = token
    wf.updated_at = now_rfc3339()
    svc.store.update_workflow(wf)
    return json_response({"token": token, "url": "/hooks/workflow/" + token})


async def _handle_delete_webhook(request: web.Request) -> web.StreamResponse:
    """DELETE /workflows/{id}/webhook — clear the run token, disabling the public
    run hook."""
    svc = _svc(request)
    wf = _load(svc, request.match_info["id"])
    wf.run_token = ""
    wf.updated_at = now_rfc3339()
    svc.store.update_workflow(wf)
    return json_response({"status": "removed"})


# ---------------------------------------------------------------------------
# public run hook
# ---------------------------------------------------------------------------


async def _handle_hook(request: web.Request) -> web.StreamResponse:
    """GET|POST|PUT|PATCH|DELETE /hooks/workflow/{token} — resolve the workflow
    for the path token and kick off a background run via the engine. Unknown
    tokens return 404.

    All five HTTP methods bind to this handler (see the route registration); the
    matching webhook trigger's configured ``method`` field then decides whether
    the request fires: an empty method defaults to POST, "any"
    accepts every method, otherwise a case-insensitive method match is required.
    A non-matching method returns 405. GET requests simply carry an empty body;
    query params still reach the steps via the captured payload."""
    svc = _svc(request)
    token = request.match_info.get("token", "")
    if token == "":
        raise NotFound("workflow webhook not found")

    try:
        wf = svc.store.get_workflow_by_run_token(token)
    except Exception as err:
        raise NotFound("workflow webhook not found") from err

    # Gate on the matching webhook trigger's configured method. The path token
    # may match a specific webhook trigger's config token or the
    # workflow-level run_token; in the latter case there is no per-trigger method
    # so the default (POST) applies. When it does not match, respond 405.
    configured_method = _webhook_method_for_token(wf, token)
    if not webhook_method_matches(configured_method, request.method):
        return error_response(405, "method not allowed for this webhook")

    # Capture the inbound request so steps can use it (body on stdin +
    # KOWAN_WEBHOOK_* for script steps; {{body}}/{{header.X}} for http steps).
    raw = await read_body(request, _MAX_HOOK_BODY)
    headers = {k: v for k, v in request.headers.items()}
    query = {k: request.query.get(k, "") for k in request.query.keys()}
    payload = WorkflowPayload(
        method=request.method,
        body=raw.decode("utf-8", "replace"),
        headers=headers,
        query=query,
    )

    run_id = await svc.workflow_engine.run(wf.id, WorkflowTriggerWebhook, payload)
    return json_response({"runId": run_id, "workflow": wf.id}, status=202)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _reload(svc) -> None:
    """Trigger a scheduler reload after a workflow change. No-op if no scheduler
    is wired."""
    scheduler: Optional[Scheduler] = getattr(svc, "workflow_scheduler", None)
    if scheduler is not None:
        await scheduler.reload()


def _webhook_method_for_token(wf: Workflow, token: str) -> str:
    """The configured HTTP method of the webhook trigger the path ``token``
    resolves to, or "" when the token matched the workflow-level run token (no
    per-trigger method) or no enabled webhook trigger carries it.

    An empty result defaults to POST in ``webhook_method_matches``."""
    for t in wf.triggers:
        if (
            t.enabled
            and t.type == triggers.TypeWebhook
            and t.config.get("token") == token
        ):
            return t.config.get("method", "")
    return ""


def _generate_run_token() -> str:
    """Random 32-character hex token for a workflow's public run webhook.
    16 random bytes, hex-encoded."""
    return _secrets.token_hex(16)


def _hide_run_tokens(request: web.Request, wf: Workflow) -> None:
    """Callers who cannot operate workflows never see the public run tokens
    (with one they could run the workflow anonymously through
    /hooks/workflow/{token}), run variables or typed HTTP headers."""
    if authz.global_allowed(authz.LEVEL_OPERATE):
        return
    wf.run_token = ""
    for t in wf.triggers:
        if "token" in t.config:
            t.config["token"] = ""
    # Literal values a viewer must not read: run variables and HTTP headers
    # (an Authorization header typed into a step).
    wf.variables = {k: MASK for k in wf.variables or {}}
    for st in wf.steps or []:
        if st.type == WorkflowStepHTTP and st.config.get("headers"):
            st.config["headers"] = "\n".join(
                line.split(":", 1)[0] + ": " + MASK if ":" in line else line
                for line in st.config["headers"].split("\n")
            )


def _mask_triggers(wf: Workflow) -> None:
    """Replace every sensitive trigger config value in ``wf`` with a masked
    placeholder so the API never serializes a stored credential. Mutates ``wf``
    in place (safe: the store returns a fresh object per read)."""
    for t in wf.triggers:
        sensitive = triggers.sensitive_keys(t.type)
        for k in list(t.config.keys()):
            if sensitive.get(k) and t.config[k] != "":
                t.config[k] = _MASKED_TRIGGER_VALUE


def _merge_triggers(
    existing: List[WorkflowTrigger], incoming: List[WorkflowTrigger]
) -> List[WorkflowTrigger]:
    """Reconcile the incoming triggers from an update with what is stored,
    matched by trigger id. Preserves provider-owned state (last-seen value,
    lastFired, lastError) and restores any sensitive field the client sent back
    blank or as the mask placeholder. When a non-sensitive config field of a
    poll trigger changes, its state is reset so it re-seeds against the new
    source instead of comparing to a stale value."""
    by_id = {t.id: t for t in existing}
    out: List[WorkflowTrigger] = []
    for t in incoming:
        prev = by_id.get(t.id)
        if prev is not None:
            sensitive = triggers.sensitive_keys(t.type)
            for k, v in list(t.config.items()):
                if sensitive.get(k) and (v == "" or v == _MASKED_TRIGGER_VALUE):
                    t.config[k] = prev.config.get(k, "")
            # Carry over provider bookkeeping.
            t.last_fired = prev.last_fired
            t.last_error = prev.last_error
            if _same_config(prev.config, t.config) and prev.type == t.type:
                t.state = prev.state
            else:
                t.state = ""  # source changed: re-seed without firing
        out.append(t)
    return out


def _same_config(a: dict, b: dict) -> bool:
    """Whether two trigger config maps are identical."""
    if len(a) != len(b):
        return False
    for k, v in a.items():
        if b.get(k) != v:
            return False
    return True
