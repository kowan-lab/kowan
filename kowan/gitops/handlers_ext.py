"""Kinds backed by newer Kowan subsystems: Schedule and AutoUpdatePolicy.

They register like the built-ins but only work when the store provides the
subsystem (``list_schedules`` / ``list_autoupdate_policies``); otherwise their
current state is empty and a resource of that kind is a plan error. This file
is also the worked example of adding a kind to the registry.

Schedule (order 70)::

    kind: Schedule
    metadata: {name: nightly-stop}
    spec:
      targetType: deployment      # deployment | container | stack
      target: web                 # deployment name, container name or stack project
      host: local                 # required for container / stack
      action: stop
      cron: "0 1 * * *"
      timezone: Europe/Rome       # optional
      enabled: true
      onlyIf: {noExecSession: true}

AutoUpdatePolicy (order 70). ``metadata.name`` IS the target: a deployment
name, or ``<host>/<container>`` for a container policy::

    kind: AutoUpdatePolicy
    metadata: {name: web}
    spec: {target: deployment, mode: auto, window: "", minAge: 24h, scanBlock: ""}
"""

from __future__ import annotations

from typing import Any, Dict

from .handlers import _bool, _now, _str
from .kinds import Context, Current, KindHandler, SpecError, register_kind
from .resources import Resource


class ScheduleHandler(KindHandler):
    kind = "Schedule"
    plural = "schedules"
    order = 70
    description = "A scheduled start/stop/restart of a deployment, container or stack."
    defaults = {"host": "", "timezone": "", "enabled": True, "onlyIf": {}}
    _ALLOWED = {"targetType", "target", "host", "action", "cron", "timezone", "enabled", "onlyIf"}

    @staticmethod
    def available(ctx: Context) -> bool:
        return hasattr(ctx.store, "list_schedules") and hasattr(ctx.store, "save_schedule")

    def _canon(self, ctx: Context, s: Dict[str, Any]) -> Dict[str, Any]:
        tt = s.get("targetType", "")
        target = ctx.deployment_name(s.get("targetId", "")) if tt == "deployment" else s.get("targetId", "")
        return {"targetType": tt, "target": target,
                "host": ctx.host_name(s.get("hostId", "")) if s.get("hostId") else "",
                "action": s.get("action", ""), "cron": s.get("cron", ""), "timezone": s.get("timezone", "") or "",
                "enabled": bool(s.get("enabled", True)), "onlyIf": dict(s.get("onlyIf") or {})}

    def current(self, ctx: Context) -> Dict[str, Current]:
        if not self.available(ctx):
            return {}
        return {s["name"]: Current(s["id"], s["name"], self._canon(ctx, s)) for s in ctx.store.list_schedules()}

    def _body(self, ctx: Context, name: str, spec: Dict[str, Any]) -> Dict[str, Any]:
        tid = spec["target"]
        if spec["targetType"] == "deployment":
            try:
                tid = ctx.store.get_deployment_by_name(tid).id
            except Exception:  # noqa: BLE001
                pass
        host = ctx.resolve_host(spec["host"]) if spec["host"] else None
        return {"name": name, "targetType": spec["targetType"], "targetId": tid,
                "hostId": host.id if host is not None else "", "action": spec["action"], "cron": spec["cron"],
                "timezone": spec["timezone"], "enabled": spec["enabled"], "onlyIf": spec["onlyIf"]}

    async def normalize(self, res: Resource, ctx: Context) -> Dict[str, Any]:
        if not self.available(ctx):
            raise SpecError("schedules are not available on this server")
        from kowan.errors import Validation
        from kowan.schedules.service import validate_schedule

        s = res.spec
        self.check_keys(s, self._ALLOWED)
        host = _str(s, "host")
        if host:
            h = ctx.resolve_host(host)
            if h is None:
                raise SpecError(f"unknown host {host!r}")
            host = h.name or h.id
        spec = {"targetType": _str(s, "targetType"), "target": _str(s, "target").strip(), "host": host,
                "action": _str(s, "action"), "cron": _str(s, "cron").strip(), "timezone": _str(s, "timezone").strip(),
                "enabled": _bool(s, "enabled", True), "onlyIf": dict(s.get("onlyIf") or {})}
        if spec["targetType"] == "deployment":
            spec["target"] = ctx.deployment_name(spec["target"])
        try:
            v = validate_schedule(self._body(ctx, res.name, spec))
        except Validation as err:
            raise SpecError(str(err)) from err
        spec["onlyIf"] = v["onlyIf"]
        return spec

    def _reload(self, ctx: Context) -> None:
        svc = getattr(ctx.svc, "schedules", None)
        if svc is not None:
            try:
                svc.reload()
            except Exception:  # noqa: BLE001
                pass

    async def create(self, ctx: Context, name: str, spec: Dict[str, Any], policies: Dict[str, Any]) -> str:
        from kowan.schedules.service import validate_schedule

        saved = ctx.store.save_schedule(validate_schedule(self._body(ctx, name, spec)))
        self._reload(ctx)
        return saved["id"]

    async def update(self, ctx: Context, cur: Current, spec: Dict[str, Any], policies: Dict[str, Any]) -> None:
        from kowan.schedules.service import validate_schedule

        existing = ctx.store.get_schedule(cur.id)
        s = validate_schedule(self._body(ctx, cur.name, spec), existing)
        s["id"] = cur.id
        ctx.store.save_schedule(s)
        self._reload(ctx)

    async def delete(self, ctx: Context, cur: Current) -> None:
        ctx.store.delete_schedule(cur.id)
        self._reload(ctx)


class AutoUpdatePolicyHandler(KindHandler):
    kind = "AutoUpdatePolicy"
    plural = "auto-update"
    order = 70
    description = ("An image auto-update policy. metadata.name is the target: a deployment name, "
                   "or <host>/<container> for a container.")
    defaults = {"target": "deployment", "window": "", "minAge": "", "scanBlock": ""}
    _ALLOWED = {"target", "mode", "window", "minAge", "scanBlock"}

    @staticmethod
    def available(ctx: Context) -> bool:
        return hasattr(ctx.store, "list_autoupdate_policies") and hasattr(ctx.store, "upsert_autoupdate_policy")

    @staticmethod
    def _rid(kind: str, host_id: str, target: str) -> str:
        return f"{kind}|{host_id}|{target}"

    def current(self, ctx: Context) -> Dict[str, Current]:
        if not self.available(ctx):
            return {}
        out = {}
        for p in ctx.store.list_autoupdate_policies():
            if p["kind"] == "deployment":
                name = ctx.deployment_name(p["target"])
            else:
                name = f"{ctx.host_name(p['hostId'])}/{p['target']}"
            out[name] = Current(self._rid(p["kind"], p["hostId"], p["target"]), name, {
                "target": p["kind"], "mode": p["mode"], "window": p.get("window", ""),
                "minAge": p.get("minAge", ""), "scanBlock": p.get("scanBlock", ""),
            })
        return out

    def _key(self, ctx: Context, name: str, target: str):
        if target == "deployment":
            try:
                dep = ctx.store.get_deployment_by_name(name)
            except Exception as err:  # noqa: BLE001
                raise SpecError(f"unknown deployment {name!r}") from err
            return "deployment", "", dep.id
        host_ref, sep, cname = name.partition("/")
        if not sep or not cname:
            raise SpecError("a container policy is named <host>/<container>")
        h = ctx.resolve_host(host_ref)
        if h is None:
            raise SpecError(f"unknown host {host_ref!r}")
        return "container", h.id, cname

    async def normalize(self, res: Resource, ctx: Context) -> Dict[str, Any]:
        if not self.available(ctx):
            raise SpecError("auto-update policies are not available on this server")
        from kowan.autoupdate.policy import validate_policy_body

        s = res.spec
        self.check_keys(s, self._ALLOWED)
        target = _str(s, "target", "deployment") or "deployment"
        if target not in ("deployment", "container"):
            raise SpecError('target must be "deployment" or "container"')
        if target == "deployment":
            # The deployment may be created by this same plan: only check the
            # name when it already exists; apply re-validates.
            pass
        try:
            p = validate_policy_body({k: s.get(k) for k in ("mode", "window", "minAge", "scanBlock") if k in s})
        except ValueError as err:
            raise SpecError(str(err)) from err
        return {"target": target, **p}

    async def _upsert(self, ctx: Context, name: str, spec: Dict[str, Any]) -> str:
        kind, host_id, target = self._key(ctx, name, spec["target"])
        ctx.store.upsert_autoupdate_policy({"kind": kind, "hostId": host_id, "target": target, "mode": spec["mode"],
                                            "window": spec["window"], "minAge": spec["minAge"],
                                            "scanBlock": spec["scanBlock"], "updatedAt": _now(), "updatedBy": "gitops"})
        return self._rid(kind, host_id, target)

    async def create(self, ctx: Context, name: str, spec: Dict[str, Any], policies: Dict[str, Any]) -> str:
        return await self._upsert(ctx, name, spec)

    async def update(self, ctx: Context, cur: Current, spec: Dict[str, Any], policies: Dict[str, Any]) -> None:
        await self._upsert(ctx, cur.name, spec)

    async def delete(self, ctx: Context, cur: Current) -> None:
        kind, host_id, target = cur.id.split("|", 2)
        ctx.store.delete_autoupdate_policy(kind, host_id, target)


register_kind(ScheduleHandler())
register_kind(AutoUpdatePolicyHandler())
