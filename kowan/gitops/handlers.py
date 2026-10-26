"""Handlers for the kinds that exist in Kowan today.

=====================  =======  ================================================
kind                   order    Kowan resource
=====================  =======  ================================================
Registry               10       image registry credential (no password inline)
NotificationChannel    20       notification channel (secrets as [[refs]])
Template               30       custom app template
Deployment             40       deployment (compose / container, git source)
AlertRule              50       monitor alert rule (channel/host by name)
Workflow               60       workflow (steps, triggers, variables)
=====================  =======  ================================================

Each handler reuses the validation of the corresponding REST module so a
resource accepted from git is exactly as valid as one created in the UI.
Secrets never live in the repository: channels and registries refuse literal
sensitive values and reference Kowan secrets by name (``[[name]]``), workflow
trigger secrets and webhook tokens are omitted from export and preserved on
update, registry passwords are kept as stored.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

from .kinds import Context, Current, KindHandler, SpecError, register_kind
from .resources import Resource, read_repo_file

log = logging.getLogger("kowan.gitops")

_SECRET_REF = re.compile(r"^\s*\[\[\s*[A-Za-z0-9_.:-]+?\s*\]\]\s*$")


def _now() -> str:
    from kowan.web import now_rfc3339

    return now_rfc3339()


def _bool(spec: Dict[str, Any], key: str, default: bool) -> bool:
    v = spec.get(key, default)
    if v is None:
        return default
    if not isinstance(v, bool):
        raise SpecError(f"{key} must be true or false")
    return v


def _str(spec: Dict[str, Any], key: str, default: str = "") -> str:
    v = spec.get(key, default)
    if v is None:
        return default
    if isinstance(v, (dict, list)):
        raise SpecError(f"{key} must be a string")
    return str(v)


def _str_map(v: Any, what: str) -> Dict[str, str]:
    if v is None:
        return {}
    if not isinstance(v, dict):
        raise SpecError(f"{what} must be a mapping")
    return {str(k): "" if val is None else str(val) for k, val in v.items()}


def _prune(d: Dict[str, Any], defaults: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in d.items() if k not in defaults or defaults[k] != v}


# ---------------------------------------------------------------------------
# Deployment
# ---------------------------------------------------------------------------


def _container_defaults() -> Dict[str, Any]:
    from kowan.models import ContainerSpec

    return ContainerSpec().model_dump(by_alias=True)


def canonical_container(spec_obj: Any) -> Dict[str, Any]:
    """A ContainerSpec as a minimal camelCase dict (defaults dropped)."""
    if spec_obj is None:
        return {}
    return _prune(spec_obj.model_dump(by_alias=True), _container_defaults())


class DeploymentHandler(KindHandler):
    kind = "Deployment"
    plural = "deployments"
    order = 40
    description = "A compose or single-container deployment on one host (optionally sourced from git)."
    defaults = {
        "type": "compose", "strategy": "standalone", "replicas": 0, "enabled": True,
        "protected": False, "compose": "", "container": None, "git": None,
    }
    policy_keys = ("policies",)
    _ALLOWED = {"type", "strategy", "host", "replicas", "enabled", "protected", "compose",
                "composeFile", "container", "git"}

    # -- canonical ------------------------------------------------------
    def _git_canonical(self, ctx: Context, git: Any) -> Optional[Dict[str, Any]]:
        if git is None or not git.repo_url:
            return None
        out: Dict[str, Any] = {"repoUrl": git.repo_url, "ref": git.ref or "main",
                               "path": git.path or "docker-compose.yml"}
        if git.credential_id:
            out["credential"] = ctx.credential_name("connection", git.credential_id)
        out["autoDeploy"] = bool(git.auto_deploy)
        return out

    def _canon(self, ctx: Context, dep: Any) -> Dict[str, Any]:
        git = self._git_canonical(ctx, dep.git)
        return {
            "type": dep.type,
            "strategy": dep.strategy,
            "host": ctx.host_name(dep.targets[0]) if dep.targets else "",
            "replicas": int(dep.replicas or 0) if dep.strategy == "swarm" else 0,
            "enabled": bool(dep.enabled),
            "protected": bool(dep.protected),
            "compose": dep.compose if dep.type == "compose" and git is None else "",
            "container": canonical_container(dep.spec) if dep.type == "container" else None,
            "git": git,
        }

    def current(self, ctx: Context) -> Dict[str, Current]:
        return {d.name: Current(d.id, d.name, self._canon(ctx, d)) for d in ctx.store.list_deployments()}

    async def normalize(self, res: Resource, ctx: Context) -> Dict[str, Any]:
        from kowan.models import ContainerSpec

        s = res.spec
        self.check_keys(s, self._ALLOWED)
        typ = _str(s, "type", "compose")
        if typ not in ("compose", "container"):
            raise SpecError('type must be "compose" or "container"')
        strategy = _str(s, "strategy", "standalone")
        if strategy not in ("standalone", "swarm"):
            raise SpecError('strategy must be "standalone" or "swarm"')
        host_ref = _str(s, "host")
        if not host_ref:
            raise SpecError("host is required (a host name or id)")
        host = ctx.resolve_host(host_ref)
        if host is None:
            raise SpecError(f"unknown host {host_ref!r}")
        try:
            replicas = int(s.get("replicas") or 0)
        except (TypeError, ValueError):
            raise SpecError("replicas must be an integer")
        out: Dict[str, Any] = {
            "type": typ, "strategy": strategy, "host": host.name or host.id,
            "replicas": replicas if strategy == "swarm" else 0,
            "enabled": _bool(s, "enabled", True), "protected": _bool(s, "protected", False),
            "compose": "", "container": None, "git": None,
        }
        if typ == "container":
            raw = s.get("container")
            if not isinstance(raw, dict):
                raise SpecError("container deployments require a container spec")
            try:
                cs = ContainerSpec.model_validate(raw)
            except Exception as err:  # noqa: BLE001 - pydantic ValidationError
                raise SpecError(f"container spec is invalid: {err}") from err
            if not cs.image.strip():
                raise SpecError("container.image is required")
            out["container"] = canonical_container(cs)
            if s.get("compose") or s.get("composeFile") or s.get("git"):
                raise SpecError("compose/composeFile/git apply to compose deployments only")
        else:
            git = s.get("git")
            if git is not None:
                if not isinstance(git, dict) or not str(git.get("repoUrl") or "").strip():
                    raise SpecError("git.repoUrl is required")
                from kowan.deployment.gitsource import validate_repo_url

                try:
                    validate_repo_url(str(git["repoUrl"]))
                except ValueError as err:
                    raise SpecError(str(err)) from err
                extra = set(git) - {"repoUrl", "ref", "path", "credential", "autoDeploy"}
                if extra:
                    raise SpecError(f"unknown git field(s): {', '.join(sorted(extra))}")
                g: Dict[str, Any] = {
                    "repoUrl": str(git["repoUrl"]).strip(),
                    "ref": str(git.get("ref") or "main").strip(),
                    "path": str(git.get("path") or "docker-compose.yml").strip().lstrip("/"),
                }
                if ".." in g["path"].split("/"):
                    raise SpecError("git.path must stay inside the repository")
                cred = str(git.get("credential") or "")
                if cred:
                    c = ctx.resolve_credential("connection", cred)
                    if c is None:
                        raise SpecError(f"unknown git credential {cred!r}")
                    g["credential"] = c.name
                g["autoDeploy"] = bool(git.get("autoDeploy", False))
                out["git"] = g
                if s.get("compose") or s.get("composeFile"):
                    raise SpecError("use either git or compose/composeFile, not both")
            else:
                compose = _str(s, "compose")
                cf = _str(s, "composeFile")
                if compose and cf:
                    raise SpecError("use either compose or composeFile, not both")
                if cf:
                    if not ctx.checkout:
                        raise SpecError("composeFile needs a repository checkout")
                    try:
                        compose = read_repo_file(ctx.checkout, res.file, cf)
                    except ValueError as err:
                        raise SpecError(str(err)) from err
                if not compose.strip():
                    raise SpecError("compose deployments require compose, composeFile or git")
                out["compose"] = compose
                await self._check_compose(ctx, compose)
        pol = s.get("policies")
        if pol is not None:
            if not isinstance(pol, dict) or set(pol) - {"deploy"}:
                raise SpecError("policies supports only: deploy")
            if pol.get("deploy", "on-change") not in ("on-change", "never"):
                raise SpecError('policies.deploy must be "on-change" or "never"')
        return out

    async def _check_compose(self, ctx: Context, compose: str) -> None:
        from kowan.compose import validate_text
        from kowan.compose.validate import secrets_from_store

        res = await validate_text(compose, secrets=secrets_from_store(ctx.store), use_cli=True)
        errs = res.errors()
        if errs:
            e = errs[0]
            where = f" (line {e.line})" if e.line else ""
            raise SpecError(f"compose: {e.message}{where}" + (f"; {len(errs) - 1} more error(s)" if len(errs) > 1 else ""))

    # -- writes ---------------------------------------------------------
    def _apply_fields(self, ctx: Context, dep: Any, spec: Dict[str, Any]) -> None:
        from kowan.models import ContainerSpec, GitSource

        host = ctx.resolve_host(spec["host"])
        dep.type = spec["type"]
        dep.strategy = spec["strategy"]
        dep.targets = [host.id] if host is not None else []
        dep.replicas = spec["replicas"]
        dep.enabled = spec["enabled"]
        dep.protected = spec["protected"]
        dep.spec = ContainerSpec.model_validate(spec["container"]) if spec["type"] == "container" else None
        g = spec.get("git")
        if g:
            from kowan.deployment.service import _generate_redeploy_token

            cred = ctx.resolve_credential("connection", g.get("credential", "")) if g.get("credential") else None
            prev_secret = dep.git.webhook_secret if dep.git is not None else ""
            had_git = dep.git is not None
            dep.git = GitSource(
                repoUrl=g["repoUrl"], ref=g["ref"], path=g["path"],
                credentialId=cred.id if cred is not None else "",
                autoDeploy=g["autoDeploy"],
                webhookSecret=prev_secret or (_generate_redeploy_token() + _generate_redeploy_token()),
            )
            if not dep.redeploy_token:
                dep.redeploy_token = _generate_redeploy_token()
            if not had_git:
                dep.compose = ""
        else:
            dep.git = None
            dep.compose = spec["compose"] if spec["type"] == "compose" else ""
            dep.deployed_commit = ""
            dep.last_sync_at = ""

    async def _maybe_deploy(self, ctx: Context, dep_id: str, policies: Dict[str, Any], redeploy: bool) -> None:
        if (policies.get("policies") or {}).get("deploy", "on-change") == "never":
            return
        dep_svc = getattr(ctx.svc, "deployments", None)
        if dep_svc is None:
            return
        try:
            await dep_svc.dispatch_deploy(dep_id, redeploy=redeploy)
        except Exception as err:  # noqa: BLE001 - Conflict (already deploying) etc.
            log.info("gitops: deploy of %s not dispatched: %s", dep_id, err)

    async def create(self, ctx: Context, name: str, spec: Dict[str, Any], policies: Dict[str, Any]) -> str:
        from kowan.models import Deployment

        now = _now()
        dep = Deployment(id=str(uuid.uuid4()), name=name, status="stopped", createdAt=now, updatedAt=now)
        want = dep.resource_name()
        for d in ctx.store.list_deployments():
            if d.resource_name() == want:
                raise SpecError(f"deployment {d.name!r} resolves to the same resource name")
        self._apply_fields(ctx, dep, spec)
        ctx.store.create_deployment(dep, author="gitops", source="gitops")
        if dep.enabled:
            await self._maybe_deploy(ctx, dep.id, policies, redeploy=False)
        return dep.id

    async def update(self, ctx: Context, cur: Current, spec: Dict[str, Any], policies: Dict[str, Any]) -> None:
        from kowan.api.deployments import _runtime_signature

        dep = ctx.store.get_deployment(cur.id)
        before = _runtime_signature(dep)
        had_git = dep.git is not None
        self._apply_fields(ctx, dep, spec)
        dep.updated_at = _now()
        ctx.store.update_deployment(dep, author="gitops", source="gitops")
        dep_svc = getattr(ctx.svc, "deployments", None)
        if had_git and dep.git is None and dep_svc is not None:
            dep_svc.remove_git_checkout(dep.id)
        if _runtime_signature(dep) != before and dep.enabled and dep.status in ("deployed", "error"):
            await self._maybe_deploy(ctx, dep.id, policies, redeploy=True)

    def delete_blocker(self, ctx: Context, cur: Current) -> str:
        return "deletion protection is enabled" if cur.spec.get("protected") else ""

    async def delete(self, ctx: Context, cur: Current) -> None:
        dep_svc = getattr(ctx.svc, "deployments", None)
        if dep_svc is not None:
            try:
                await dep_svc.destroy(cur.id)
            except Exception as err:  # noqa: BLE001 - unreachable host: still remove
                log.warning("gitops: destroy before delete failed for %s: %s", cur.name, err)
        ctx.store.delete_deployment(cur.id)
        if dep_svc is not None:
            dep_svc.remove_git_checkout(cur.id)

    def export_spec(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        out = super().export_spec(spec)
        if spec.get("strategy") != "swarm":
            out.pop("replicas", None)
        return out


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------

_STEP_REF_KEYS = ("deployment", "channel", "host")


class WorkflowHandler(KindHandler):
    kind = "Workflow"
    plural = "workflows"
    order = 60
    description = "A workflow: ordered steps, triggers and variables (trigger secrets are never exported)."
    defaults = {"description": "", "enabled": False, "variables": {}, "steps": [], "triggers": []}
    _ALLOWED = {"description", "enabled", "variables", "steps", "triggers"}

    @staticmethod
    def _hidden_trigger_keys(ttype: str) -> set:
        from kowan import triggers

        return {k for k, v in triggers.sensitive_keys(ttype).items() if v} | {"token"}

    def _step_to_canon(self, ctx: Context, step: Any) -> Dict[str, Any]:
        cfg = dict(step.config or {})
        if cfg.get("deployment"):
            cfg["deployment"] = ctx.deployment_name(cfg["deployment"])
        if cfg.get("channel"):
            cfg["channel"] = ctx.credential_name("channel", cfg["channel"])
        if cfg.get("host"):
            cfg["host"] = ctx.host_name(cfg["host"])
        out: Dict[str, Any] = {"name": step.name, "type": step.type, "config": cfg}
        if step.continue_on_error:
            out["continueOnError"] = True
        return out

    def _trigger_to_canon(self, t: Any) -> Dict[str, Any]:
        hidden = self._hidden_trigger_keys(t.type)
        return {"type": t.type, "enabled": bool(t.enabled),
                "config": {k: v for k, v in (t.config or {}).items() if k not in hidden}}

    def current(self, ctx: Context) -> Dict[str, Current]:
        out = {}
        for wf in ctx.store.list_workflows():
            out[wf.name] = Current(wf.id, wf.name, {
                "description": wf.description or "",
                "enabled": bool(wf.enabled),
                "variables": dict(wf.variables or {}),
                "steps": [self._step_to_canon(ctx, s) for s in wf.steps],
                "triggers": [self._trigger_to_canon(t) for t in wf.triggers],
                "_protected": bool(wf.protected),
            })
        return out

    async def normalize(self, res: Resource, ctx: Context) -> Dict[str, Any]:
        from kowan.api.workflows import _Request, _normalize, _validate

        s = res.spec
        self.check_keys(s, self._ALLOWED)
        steps_raw = s.get("steps") or []
        trig_raw = s.get("triggers") or []
        if not isinstance(steps_raw, list) or not isinstance(trig_raw, list):
            raise SpecError("steps and triggers must be lists")
        steps: List[Dict[str, Any]] = []
        for i, st in enumerate(steps_raw):
            if not isinstance(st, dict):
                raise SpecError(f"step {i + 1} must be a mapping")
            extra = set(st) - {"name", "type", "config", "continueOnError"}
            if extra:
                raise SpecError(f"step {i + 1}: unknown field(s) {', '.join(sorted(extra))}")
            cfg = _str_map(st.get("config"), f"step {i + 1} config")
            if cfg.get("channel"):
                ch = ctx.resolve_credential("channel", cfg["channel"])
                if ch is not None:
                    cfg["channel"] = ch.name
                elif not ctx.planned_name("NotificationChannel", cfg["channel"]):
                    raise SpecError(f"step {i + 1}: unknown channel {cfg['channel']!r}")
            if cfg.get("host"):
                h = ctx.resolve_host(cfg["host"])
                if h is None:
                    raise SpecError(f"step {i + 1}: unknown host {cfg['host']!r}")
                cfg["host"] = h.name or h.id
            if cfg.get("deployment"):
                cfg["deployment"] = ctx.deployment_name(cfg["deployment"])
            step: Dict[str, Any] = {"name": _str(st, "name").strip(), "type": _str(st, "type").strip(), "config": cfg}
            if st.get("continueOnError"):
                step["continueOnError"] = True
            steps.append(step)
        trigs: List[Dict[str, Any]] = []
        for i, t in enumerate(trig_raw):
            if not isinstance(t, dict):
                raise SpecError(f"trigger {i + 1} must be a mapping")
            extra = set(t) - {"type", "enabled", "config"}
            if extra:
                raise SpecError(f"trigger {i + 1}: unknown field(s) {', '.join(sorted(extra))}")
            ttype = _str(t, "type").strip()
            cfg = _str_map(t.get("config"), f"trigger {i + 1} config")
            for k in self._hidden_trigger_keys(ttype):
                v = cfg.get(k, "")
                if k == "token" and k in cfg:
                    raise SpecError(f"trigger {i + 1}: the webhook token is server-generated; remove it")
                if v and not _SECRET_REF.match(v):
                    raise SpecError(f"trigger {i + 1}: {k} is sensitive; reference a Kowan secret ([[name]]) instead of a literal")
            trigs.append({"type": ttype, "enabled": bool(t.get("enabled", True)), "config": cfg})
        out = {
            "description": _str(s, "description").strip(),
            "enabled": _bool(s, "enabled", False),
            "variables": _str_map(s.get("variables"), "variables"),
            "steps": steps,
            "triggers": trigs,
        }
        # Same validation as POST /workflows (step types, trigger fields).
        body = self._body(ctx, res.name, out)
        req = _Request(body)
        _normalize(req)
        msg = _validate(req)
        if msg:
            raise SpecError(msg)
        return out

    def _body(self, ctx: Context, name: str, spec: Dict[str, Any]) -> Dict[str, Any]:
        steps = []
        for st in spec["steps"]:
            cfg = dict(st["config"])
            if cfg.get("channel"):
                c = ctx.resolve_credential("channel", cfg["channel"])
                cfg["channel"] = c.id if c is not None else cfg["channel"]
            if cfg.get("host"):
                h = ctx.resolve_host(cfg["host"])
                cfg["host"] = h.id if h is not None else cfg["host"]
            steps.append({"name": st["name"], "type": st["type"], "config": cfg,
                          "continueOnError": bool(st.get("continueOnError"))})
        return {
            "name": name, "description": spec["description"], "enabled": spec["enabled"],
            "variables": spec["variables"], "steps": steps,
            "triggers": [{"type": t["type"], "enabled": t["enabled"], "config": dict(t["config"])}
                         for t in spec["triggers"]],
        }

    async def _reload(self, ctx: Context) -> None:
        sched = getattr(ctx.svc, "workflow_scheduler", None)
        if sched is not None:
            try:
                await sched.reload()
            except Exception as err:  # noqa: BLE001
                log.warning("gitops: scheduler reload failed: %s", err)

    async def create(self, ctx: Context, name: str, spec: Dict[str, Any], policies: Dict[str, Any]) -> str:
        from kowan.api.workflows import _Request, _normalize, _validate
        from kowan.models import Workflow

        req = _Request(self._body(ctx, name, spec))
        _normalize(req)
        msg = _validate(req)
        if msg:
            raise SpecError(msg)
        now = _now()
        wf = Workflow(id=str(uuid.uuid4()), name=req.name, description=req.description, steps=req.steps,
                      triggers=req.triggers, variables=req.variables, enabled=req.enabled,
                      created_at=now, updated_at=now)
        ctx.store.create_workflow(wf)
        await self._reload(ctx)
        return wf.id

    async def update(self, ctx: Context, cur: Current, spec: Dict[str, Any], policies: Dict[str, Any]) -> None:
        from kowan.api.workflows import _Request, _merge_triggers, _normalize, _validate

        wf = ctx.store.get_workflow(cur.id)
        req = _Request(self._body(ctx, wf.name, spec))
        # Keep trigger identities (and their stored secrets / tokens) by
        # matching position + type with what is stored.
        for i, t in enumerate(req.triggers):
            if i < len(wf.triggers) and wf.triggers[i].type == t.type:
                prev = wf.triggers[i]
                t.id = prev.id
                for k in self._hidden_trigger_keys(t.type):
                    if k not in t.config and k in (prev.config or {}):
                        t.config[k] = prev.config[k]
        _normalize(req)
        msg = _validate(req)
        if msg:
            raise SpecError(msg)
        wf.description = req.description
        wf.steps = req.steps
        wf.triggers = _merge_triggers(wf.triggers, req.triggers)
        wf.variables = req.variables
        wf.enabled = req.enabled
        wf.updated_at = _now()
        ctx.store.update_workflow(wf)
        await self._reload(ctx)

    def delete_blocker(self, ctx: Context, cur: Current) -> str:
        return "deletion protection is enabled" if cur.spec.get("_protected") else ""

    async def delete(self, ctx: Context, cur: Current) -> None:
        ctx.store.delete_workflow(cur.id)
        await self._reload(ctx)


# ---------------------------------------------------------------------------
# AlertRule
# ---------------------------------------------------------------------------


class AlertRuleHandler(KindHandler):
    kind = "AlertRule"
    plural = "alert-rules"
    order = 50
    description = "A monitor alert rule; host and channel are referenced by name."
    defaults = {"operator": "", "threshold": 0.0, "severity": "", "forDuration": 0, "host": "",
                "match": "", "labelKey": "", "labelValue": "", "channel": "", "autoResolve": True,
                "enabled": True}
    _ALLOWED = {"type", "operator", "threshold", "severity", "forDuration", "host", "match",
                "labelKey", "labelValue", "channel", "autoResolve", "enabled"}

    def _canon(self, ctx: Context, r: Any) -> Dict[str, Any]:
        return {
            "type": r.type, "operator": r.operator or "", "threshold": float(r.threshold or 0),
            "severity": r.severity or "", "forDuration": int(r.for_duration or 0),
            "host": ctx.host_name(r.host_id) if r.host_id else "", "match": r.match or "",
            "labelKey": r.label_key or "", "labelValue": r.label_value or "",
            "channel": ctx.credential_name("channel", r.channel_id) if r.channel_id else "",
            "autoResolve": bool(r.auto_resolve), "enabled": bool(r.enabled),
        }

    def current(self, ctx: Context) -> Dict[str, Current]:
        return {r.name: Current(r.id, r.name, self._canon(ctx, r)) for r in ctx.store.list_alert_rules()}

    def _body(self, ctx: Context, name: str, spec: Dict[str, Any]) -> Dict[str, Any]:
        host = ctx.resolve_host(spec["host"]) if spec.get("host") else None
        ch = ctx.resolve_credential("channel", spec["channel"]) if spec.get("channel") else None
        return {
            "name": name, "type": spec["type"], "operator": spec["operator"], "threshold": spec["threshold"],
            "severity": spec["severity"], "forDuration": spec["forDuration"],
            "hostId": host.id if host is not None else "", "match": spec["match"],
            "labelKey": spec["labelKey"], "labelValue": spec["labelValue"],
            "channelId": ch.id if ch is not None else "", "autoResolve": spec["autoResolve"],
            "enabled": spec["enabled"],
        }

    async def normalize(self, res: Resource, ctx: Context) -> Dict[str, Any]:
        from kowan.errors import Validation
        from kowan.models import AlertRule
        from kowan.monitor.api import _apply

        s = res.spec
        self.check_keys(s, self._ALLOWED)
        try:
            threshold = float(s.get("threshold") or 0)
            for_duration = int(s.get("forDuration") or 0)
        except (TypeError, ValueError):
            raise SpecError("threshold must be a number and forDuration an integer")
        host = _str(s, "host")
        if host:
            h = ctx.resolve_host(host)
            if h is None:
                raise SpecError(f"unknown host {host!r}")
            host = h.name or h.id
        channel = _str(s, "channel")
        if channel:
            c = ctx.resolve_credential("channel", channel)
            if c is not None:
                channel = c.name
            elif not ctx.planned_name("NotificationChannel", channel):
                raise SpecError(f"unknown notification channel {channel!r}")
        spec = {
            "type": _str(s, "type"), "operator": _str(s, "operator"), "threshold": threshold,
            "severity": _str(s, "severity"), "forDuration": for_duration, "host": host,
            "match": _str(s, "match").strip(), "labelKey": _str(s, "labelKey").strip(),
            "labelValue": _str(s, "labelValue").strip(), "channel": channel,
            "autoResolve": _bool(s, "autoResolve", True), "enabled": _bool(s, "enabled", True),
        }
        rule = AlertRule()
        try:
            _apply(self._body(ctx, res.name, spec), rule)
        except Validation as err:
            raise SpecError(str(err)) from err
        spec["operator"] = rule.operator  # default filled by the type
        return spec

    async def create(self, ctx: Context, name: str, spec: Dict[str, Any], policies: Dict[str, Any]) -> str:
        from kowan.models import AlertRule
        from kowan.monitor.api import _apply

        rule = AlertRule(id=str(uuid.uuid4()), autoResolve=True, enabled=True, createdAt=_now())
        _apply(self._body(ctx, name, spec), rule)
        ctx.store.create_alert_rule(rule)
        return rule.id

    async def update(self, ctx: Context, cur: Current, spec: Dict[str, Any], policies: Dict[str, Any]) -> None:
        from kowan.monitor.api import _apply

        rule = ctx.store.get_alert_rule(cur.id)
        _apply(self._body(ctx, rule.name, spec), rule)
        ctx.store.update_alert_rule(rule)

    async def delete(self, ctx: Context, cur: Current) -> None:
        ctx.store.delete_alert_rule(cur.id)


# ---------------------------------------------------------------------------
# NotificationChannel
# ---------------------------------------------------------------------------


class ChannelHandler(KindHandler):
    kind = "NotificationChannel"
    plural = "channels"
    order = 20
    description = ("A notification channel. Sensitive config values must reference Kowan secrets "
                   "([[name]] / [[name.field]]); they are resolved when applied.")
    defaults = {"enabled": True, "minLevel": "info", "config": {}}
    _ALLOWED = {"type", "enabled", "minLevel", "config"}

    @staticmethod
    def _sensitive(ctype: str) -> Dict[str, bool]:
        from kowan.notify import provider as notify

        return notify.sensitive_keys(ctype)

    def _canon(self, ctx: Context, c: Any) -> Dict[str, Any]:
        sens = self._sensitive(c.type)
        refs = (c.meta or {}).get("gitopsRefs") or {}
        try:
            data = ctx.store.credential_config(c.id)
        except Exception:  # noqa: BLE001
            data = {}
        cfg: Dict[str, str] = {}
        for k, v in sorted(data.items()):
            if sens.get(k):
                if k in refs:
                    cfg[k] = str(refs[k])
                continue  # literal secret: never exported, never diffed
            cfg[k] = v
        return {"type": c.type, "enabled": bool((c.meta or {}).get("enabled", False)),
                "minLevel": str((c.meta or {}).get("minLevel") or "info"), "config": cfg}

    def current(self, ctx: Context) -> Dict[str, Current]:
        out = {}
        for c in ctx._creds("channel"):
            out[c.name] = Current(c.id, c.name, self._canon(ctx, c))
        return out

    async def normalize(self, res: Resource, ctx: Context) -> Dict[str, Any]:
        from kowan.models.alert import valid_level
        from kowan.notify import provider as notify

        s = res.spec
        self.check_keys(s, self._ALLOWED)
        ctype = _str(s, "type")
        if not ctype:
            raise SpecError("type is required")
        if notify.fields(ctype) is None:
            raise SpecError(f"unknown channel type {ctype!r}")
        ml = _str(s, "minLevel", "info") or "info"
        if not valid_level(ml):
            raise SpecError("minLevel must be one of info, warning, critical")
        cfg = _str_map(s.get("config"), "config")
        sens = self._sensitive(ctype)
        names = {c.name for c in ctx._creds("secret")}
        for k, v in cfg.items():
            if sens.get(k) and v:
                if not _SECRET_REF.match(v):
                    raise SpecError(f"config.{k} is sensitive: reference a Kowan secret ([[name]]) instead of a literal value")
                ref = v.strip()[2:-2].strip().split("::")[-1].split(".")[0]
                if ref not in names:
                    raise SpecError(f"config.{k} references unknown secret {ref!r}")
        return {"type": ctype, "enabled": _bool(s, "enabled", True), "minLevel": ml,
                "config": dict(sorted(cfg.items()))}

    def _resolve(self, ctx: Context, spec: Dict[str, Any]) -> Tuple[Dict[str, str], Dict[str, str]]:
        sens = self._sensitive(spec["type"])
        data: Dict[str, str] = {}
        refs: Dict[str, str] = {}
        for k, v in spec["config"].items():
            if sens.get(k) and v:
                val = ctx.store.resolve_secrets(v)
                if "[[" in val:
                    raise SpecError(f"config.{k}: secret reference {v.strip()} could not be resolved")
                data[k] = val
                refs[k] = v.strip()
            else:
                data[k] = v
        return data, refs

    async def create(self, ctx: Context, name: str, spec: Dict[str, Any], policies: Dict[str, Any]) -> str:
        from kowan.models.credential import Credential

        data, refs = await asyncio.to_thread(self._resolve, ctx, spec)
        now = _now()
        cred = Credential(id=str(uuid.uuid4()), kind="channel", type=spec["type"], name=name,
                          meta={"enabled": spec["enabled"], "minLevel": spec["minLevel"], "gitopsRefs": refs},
                          createdAt=now, updatedAt=now)
        ctx.store.create_credential(cred, data)
        return cred.id

    async def update(self, ctx: Context, cur: Current, spec: Dict[str, Any], policies: Dict[str, Any]) -> None:
        existing = ctx.store.get_credential(cur.id)
        if existing.type != spec["type"]:
            raise SpecError("a channel's type cannot be changed; rename it to replace the channel")
        data, refs = await asyncio.to_thread(self._resolve, ctx, spec)
        sens = self._sensitive(spec["type"])
        stored = ctx.store.credential_config(cur.id)
        # Sensitive keys not mentioned in git keep their stored value.
        for k, v in stored.items():
            if sens.get(k) and k not in spec["config"]:
                data[k] = v
        meta = dict(existing.meta or {})
        meta.update({"enabled": spec["enabled"], "minLevel": spec["minLevel"], "gitopsRefs": refs})
        existing.meta = meta
        existing.updated_at = _now()
        ctx.store.update_credential(existing, data or {"_": ""} if not data else data, replace=True)

    async def delete(self, ctx: Context, cur: Current) -> None:
        ctx.store.delete_credential(cur.id)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class RegistryHandler(KindHandler):
    kind = "Registry"
    plural = "registries"
    order = 10
    description = ("An image registry. Credentials are never inline: the password stays as stored in "
                   "Kowan, or auth comes from a shared connection referenced by name.")
    defaults = {"type": "generic", "username": "", "insecure": False, "connection": ""}
    _ALLOWED = {"type", "url", "username", "insecure", "connection"}

    def _canon(self, ctx: Context, c: Any) -> Dict[str, Any]:
        meta = c.meta or {}
        ref = str(meta.get("connectionRef") or "")
        return {"type": c.type or "generic", "url": str(meta.get("url") or ""),
                "username": str(meta.get("username") or ""), "insecure": bool(meta.get("insecure", False)),
                "connection": ctx.credential_name("connection", ref) if ref else ""}

    def current(self, ctx: Context) -> Dict[str, Current]:
        return {c.name: Current(c.id, c.name, self._canon(ctx, c)) for c in ctx._creds("registry")}

    async def normalize(self, res: Resource, ctx: Context) -> Dict[str, Any]:
        from kowan.models import normalize_registry_type

        s = res.spec
        self.check_keys(s, self._ALLOWED | {"password"})
        if "password" in s:
            raise SpecError("password must not be stored in git: set it in Kowan, or reference a connection")
        url = _str(s, "url").strip()
        if not url:
            raise SpecError("url is required")
        typ = _str(s, "type", "generic") or "generic"
        if normalize_registry_type(typ) != typ:
            raise SpecError(f"unknown registry type {typ!r}")
        conn = _str(s, "connection")
        if conn:
            c = ctx.resolve_credential("connection", conn)
            if c is None:
                raise SpecError(f"unknown connection {conn!r}")
            conn = c.name
        return {"type": typ, "url": url, "username": _str(s, "username"),
                "insecure": _bool(s, "insecure", False), "connection": conn}

    def _meta(self, ctx: Context, spec: Dict[str, Any], base: Dict[str, Any]) -> Dict[str, Any]:
        meta = dict(base)
        meta.update({"url": spec["url"], "username": spec["username"], "insecure": spec["insecure"]})
        conn = ctx.resolve_credential("connection", spec["connection"]) if spec["connection"] else None
        if conn is not None:
            meta["connectionRef"] = conn.id
        else:
            meta.pop("connectionRef", None)
        return meta

    async def create(self, ctx: Context, name: str, spec: Dict[str, Any], policies: Dict[str, Any]) -> str:
        from kowan.models.credential import Credential

        now = _now()
        cred = Credential(id=str(uuid.uuid4()), kind="registry", type=spec["type"], name=name,
                          meta=self._meta(ctx, spec, {"verified": False, "lastError": "", "hasPassword": False}),
                          createdAt=now, updatedAt=now)
        ctx.store.create_credential(cred, {})
        return cred.id

    async def update(self, ctx: Context, cur: Current, spec: Dict[str, Any], policies: Dict[str, Any]) -> None:
        existing = ctx.store.get_credential(cur.id)
        existing.type = spec["type"]
        existing.meta = self._meta(ctx, spec, existing.meta or {})
        existing.updated_at = _now()
        ctx.store.update_registry_credential(existing, password="", config=None)

    async def delete(self, ctx: Context, cur: Current) -> None:
        ctx.store.delete_credential(cur.id)


# ---------------------------------------------------------------------------
# Template
# ---------------------------------------------------------------------------


class TemplateHandler(KindHandler):
    kind = "Template"
    plural = "templates"
    order = 30
    description = "A custom app template (built-in templates are not managed)."
    defaults = {"description": "", "category": "", "icon": "", "spec": None, "compose": "",
                "inputs": [], "website": ""}
    _ALLOWED = {"description", "category", "icon", "type", "spec", "compose", "inputs", "website"}

    def _canon(self, t: Any) -> Dict[str, Any]:
        return {
            "description": t.description or "", "category": t.category or "", "icon": t.icon or "",
            "type": t.type, "spec": t.spec if t.spec else None, "compose": t.compose or "",
            "inputs": [i.model_dump(by_alias=True) for i in t.inputs], "website": t.website or "",
        }

    def current(self, ctx: Context) -> Dict[str, Current]:
        return {t.name: Current(t.id, t.name, self._canon(t)) for t in ctx.store.list_templates()}

    def _model(self, name: str, spec: Dict[str, Any]) -> Any:
        from kowan.templates import Template, TemplateInput

        return Template(name=name, description=spec["description"], category=spec["category"],
                        icon=spec["icon"], type=spec["type"], spec=spec["spec"], compose=spec["compose"],
                        inputs=[TemplateInput.model_validate(i) for i in spec["inputs"]],
                        website=spec["website"])

    async def normalize(self, res: Resource, ctx: Context) -> Dict[str, Any]:
        from kowan.templates import TemplateError, TemplateInput, validate_template

        s = res.spec
        self.check_keys(s, self._ALLOWED)
        raw_spec = s.get("spec")
        if raw_spec is not None and not isinstance(raw_spec, dict):
            raise SpecError("spec must be a mapping")
        inputs = s.get("inputs") or []
        if not isinstance(inputs, list):
            raise SpecError("inputs must be a list")
        try:
            inputs_c = [TemplateInput.model_validate(i).model_dump(by_alias=True) for i in inputs]
        except Exception as err:  # noqa: BLE001
            raise SpecError(f"invalid inputs: {err}") from err
        spec = {
            "description": _str(s, "description"), "category": _str(s, "category"), "icon": _str(s, "icon"),
            "type": _str(s, "type"), "spec": raw_spec or None, "compose": _str(s, "compose"),
            "inputs": inputs_c, "website": _str(s, "website"),
        }
        try:
            validate_template(self._model(res.name, spec))
        except TemplateError as err:
            raise SpecError(str(err)) from err
        return spec

    async def create(self, ctx: Context, name: str, spec: Dict[str, Any], policies: Dict[str, Any]) -> str:
        t = self._model(name, spec)
        t.id = str(uuid.uuid4())
        t.created_at = t.updated_at = _now()
        ctx.store.create_template(t)
        return t.id

    async def update(self, ctx: Context, cur: Current, spec: Dict[str, Any], policies: Dict[str, Any]) -> None:
        old = ctx.store.get_template(cur.id)
        t = self._model(old.name, spec)
        t.id, t.created_at, t.updated_at = old.id, old.created_at, _now()
        ctx.store.update_template(t)

    async def delete(self, ctx: Context, cur: Current) -> None:
        ctx.store.delete_template(cur.id)


def register_builtin_kinds() -> None:
    for h in (RegistryHandler(), ChannelHandler(), TemplateHandler(), DeploymentHandler(),
              AlertRuleHandler(), WorkflowHandler()):
        register_kind(h)


register_builtin_kinds()


def spec_hash(spec: Dict[str, Any]) -> str:
    import hashlib

    return hashlib.sha256(json.dumps(spec, sort_keys=True, default=str).encode()).hexdigest()[:16]
