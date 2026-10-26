"""Background auto-updater: periodic image checks + policy actions.

Loop (one task for the app's lifetime):

  * ticks every ``TICK`` seconds (60 s);
  * a host is *due* for a registry check every ``interval`` (default 6 h,
    setting ``autoupdate.settings``), staggered by a stable per-host offset so
    hosts do not all hit their registries in the same minute;
  * between checks, objects with a pending ``apply`` (waiting for their
    maintenance window or minimum age) are re-evaluated against the digest
    remembered in their state, with no registry call.

Per object the policy (labels > stored > defaults, see ``policy.py``) decides:

  notify  alert once per new digest;
  pull    pull the new image once per digest, do not switch;
  apply   when the window is open and the image is old enough: pull, scan gate,
          recreate, verify, roll back on failure (``engine.py``).

Remote digests come from ``kowan.api.updates._CACHE`` (the same TTL cache the
"update available" badge uses), so a check right after a badge refresh is free.
Outcomes go to ``autoupdate_history``; blocked / rolled back / failed updates
and ``notify`` hits are published as ``alert`` events (bell + channels) unless
the container has ``kowan.notify=off``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
import zlib
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from kowan.events import Event

from . import engine
from .policy import (
    LABEL_UPDATE,
    MODE_NOTIFY,
    MODE_OFF,
    MODE_PULL,
    EffectivePolicy,
    age_ok,
    format_duration,
    resolve_policy,
)

log = logging.getLogger("kowan.autoupdate")

TICK = 60.0
DEFAULT_INTERVAL = 6 * 3600
MIN_INTERVAL = 15 * 60
STAGGER_MAX = 600.0
SETTINGS_KEY = "autoupdate.settings"
HISTORY_RETENTION = timedelta(days=90)

COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
COMPOSE_SERVICE_LABEL = "com.docker.compose.service"
LABEL_DEPLOYMENT_ID = "kowan.deployment.id"

DEFAULT_SETTINGS: Dict[str, Any] = {
    "enabled": True,
    "intervalSeconds": DEFAULT_INTERVAL,
    "defaultScanBlock": "never",
    "verifyTimeoutSeconds": 120,
    "stableSeconds": 10,
}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def rfc3339(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_rfc3339(s: Any) -> Optional[datetime]:
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_docker_time(s: Any) -> Optional[datetime]:
    """Docker's ``Created`` (``2024-05-01T10:00:00.123456789Z``)."""
    return parse_rfc3339(s)


def load_settings(store: Any) -> Dict[str, Any]:
    out = dict(DEFAULT_SETTINGS)
    try:
        raw = store.get_setting(SETTINGS_KEY)
        if raw:
            data = json.loads(raw)
            if isinstance(data, dict):
                out.update({k: v for k, v in data.items() if k in DEFAULT_SETTINGS})
    except Exception:  # noqa: BLE001 - defaults on any problem
        pass
    try:
        out["intervalSeconds"] = max(MIN_INTERVAL, int(out["intervalSeconds"]))
    except (TypeError, ValueError):
        out["intervalSeconds"] = DEFAULT_INTERVAL
    return out


def stagger_offset(host_id: str, interval: float) -> float:
    """Stable per-host delay in [0, min(interval, 10 min))."""
    span = min(float(interval), STAGGER_MAX)
    return (zlib.crc32(host_id.encode()) % 1000) / 1000.0 * span


def container_name(c: Any) -> str:
    names = getattr(c, "names", None) or []
    return (names[0] if names else getattr(c, "id", "")[:12]).lstrip("/")


def tracked_ref(config_image: str, state: Dict[str, Any]) -> str:
    """The ref to follow: the container's own image ref, unless it is pinned
    to the digest our rollback put there, in which case the original tag."""
    tr = state.get("trackedRef") or ""
    pin = state.get("rollbackRef") or ""
    if tr and pin and config_image == pin:
        return tr
    return config_image


class AutoUpdater:
    def __init__(self, svc: Any, *, tick: float = TICK) -> None:
        self.svc = svc
        self.tick = tick
        self._task: Optional[asyncio.Task] = None
        self._next_check: Dict[str, float] = {}
        self._busy: set = set()
        self._started_at = 0.0
        self.last_tick_at = ""
        self._loop_time = None

    # -- lifecycle ------------------------------------------------------------
    @property
    def store(self) -> Any:
        return self.svc.store

    def settings(self) -> Dict[str, Any]:
        return load_settings(self.store)

    def start(self) -> None:
        if self._task is None:
            self._started_at = asyncio.get_running_loop().time()
            self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        self._task = None

    async def _run(self) -> None:
        # First pass a minute after startup (plus the per-host stagger).
        await asyncio.sleep(min(self.tick, 60.0))
        pruned_day = ""
        while True:
            try:
                await self.tick_once()
                day = now_utc().strftime("%Y-%m-%d")
                if day != pruned_day:
                    pruned_day = day
                    self.store.prune_autoupdate_history(rfc3339(now_utc() - HISTORY_RETENTION))
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - never kill the loop
                log.error("autoupdate: tick failed: %s", err)
            await asyncio.sleep(self.tick)

    def next_check_at(self, host_id: str) -> Optional[str]:
        t = self._next_check.get(host_id)
        if t is None:
            return None
        delta = t - asyncio.get_event_loop().time()
        return rfc3339(now_utc() + timedelta(seconds=max(0.0, delta)))

    async def tick_once(self) -> None:
        cfg = self.settings()
        self.last_tick_at = rfc3339(now_utc())
        if not cfg.get("enabled", True):
            return
        interval = float(cfg["intervalSeconds"])
        loop_now = asyncio.get_running_loop().time()
        pending_hosts = self._pending_hosts()
        hosts = self.store.list_hosts()
        ids = {h.id for h in hosts}
        for hid in [h for h in self._next_check if h not in ids]:
            del self._next_check[hid]
        for host in hosts:
            due_at = self._next_check.get(host.id)
            if due_at is None:
                due_at = self._started_at + stagger_offset(host.id, interval)
                self._next_check[host.id] = due_at
            due = loop_now >= due_at
            if not due and host.id not in pending_hosts:
                continue
            if due:
                self._next_check[host.id] = loop_now + interval
            try:
                await self.process_host(host, check_registry=due)
            except Exception as err:  # noqa: BLE001
                log.warning("autoupdate: host %s: %s", host.id, err)

    def _pending_hosts(self) -> set:
        out = set()
        for st in self.store.list_autoupdate_states():
            if st["data"].get("pending"):
                if st["kind"] == "container":
                    out.add(st["hostId"])
                else:
                    out.add(st["data"].get("hostId", ""))
        return out

    # -- host pass -------------------------------------------------------------
    def _ops(self, host: Any) -> Any:
        return self.svc.docker.ops(host)

    def defaults(self) -> Dict[str, Any]:
        cfg = self.settings()
        return {"scanBlock": cfg.get("defaultScanBlock") or "never"}

    async def process_host(self, host: Any, *, check_registry: bool, only: Optional[Tuple[str, str]] = None,
                           trigger: str = "auto") -> List[Dict[str, Any]]:
        """Evaluate every policy-bearing object on ``host``. ``only`` limits the
        pass to one ``(kind, target)``. Returns per-object summaries."""
        ops = self._ops(host)
        containers = await ops.list_containers(True)
        defaults = self.defaults()
        results: List[Dict[str, Any]] = []
        if only is None:
            self._forget_missing(host.id, {container_name(c) for c in containers})

        deployments = []
        dep_policies: Dict[str, Dict[str, Any]] = {}
        try:
            for d in self.store.list_deployments():
                if host.id in (d.targets or []) and getattr(d, "strategy", "") != "swarm":
                    p = self.store.get_autoupdate_policy("deployment", "", d.id)
                    if p and p.get("mode", MODE_OFF) != MODE_OFF:
                        deployments.append(d)
                        dep_policies[d.id] = p
        except Exception as err:  # noqa: BLE001
            log.debug("autoupdate: list deployments: %s", err)
        dep_by_project = {d.resource_name(): d.id for d in deployments}
        dep_ids = set(dep_policies)

        def owning_dep(c: Any) -> str:
            labels = c.labels or {}
            if labels.get(LABEL_DEPLOYMENT_ID) in dep_ids:
                return labels[LABEL_DEPLOYMENT_ID]
            return dep_by_project.get(labels.get(COMPOSE_PROJECT_LABEL, ""), "")

        for c in containers:
            labels = c.labels or {}
            if labels.get("kowan.support") == "true":
                continue
            name = container_name(c)
            if only and only != ("container", name):
                continue
            if owning_dep(c) and LABEL_UPDATE not in labels:
                continue  # the deployment's policy covers it
            stored = self.store.get_autoupdate_policy("container", host.id, name)
            eff = resolve_policy(labels, stored, defaults)
            if eff.mode == MODE_OFF:
                continue
            try:
                results.append(await self.process_container(host, ops, c, eff, check_registry=check_registry,
                                                            trigger=trigger))
            except Exception as err:  # noqa: BLE001
                log.warning("autoupdate: container %s on %s: %s", name, host.id, err)
                self._save_state("container", host.id, name, {"lastResult": "error", "lastMessage": str(err)})

        for d in deployments:
            if only and only != ("deployment", d.id):
                continue
            eff = resolve_policy({}, dep_policies[d.id], defaults)
            members = [c for c in containers if owning_dep(c) == d.id]
            try:
                results.append(await self.process_deployment(host, ops, d, members, eff,
                                                             check_registry=check_registry, trigger=trigger))
            except Exception as err:  # noqa: BLE001
                log.warning("autoupdate: deployment %s: %s", d.id, err)
                self._save_state("deployment", "", d.id, {"lastResult": "error", "lastMessage": str(err),
                                                          "hostId": host.id})
        return results

    def _forget_missing(self, host_id: str, names: set) -> None:
        """Drop state rows whose object is gone: containers no longer on ``host_id``
        and deployments that were deleted. A stored policy is kept (a container
        recreated under the same name picks it up again); only its state goes.
        Without this a failed one-off update of a since-removed container stays
        listed on the Auto-update page forever."""
        try:
            dep_ids = {d.id for d in self.store.list_deployments()}
        except Exception:  # noqa: BLE001
            return
        for st in self.store.list_autoupdate_states():
            kind, target = st["kind"], st["target"]
            if kind == "container" and st["hostId"] == host_id and target not in names:
                self.store.delete_autoupdate_state(kind, host_id, target)
            elif kind == "deployment" and target not in dep_ids:
                self.store.delete_autoupdate_state(kind, st["hostId"], target)

    # -- state / history / alerts ---------------------------------------------
    def _state(self, kind: str, host_id: str, target: str) -> Dict[str, Any]:
        return self.store.get_autoupdate_state(kind, host_id, target)

    def _save_state(self, kind: str, host_id: str, target: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        st = self._state(kind, host_id, target)
        st.update(patch)
        self.store.put_autoupdate_state(kind, host_id, target, st, rfc3339(now_utc()))
        return st

    def record(self, *, kind: str, host_id: str, target: str, target_name: str, image: str, mode: str,
               trigger: str, outcome: str, message: str, from_digest: str = "", to_digest: str = "",
               scan: Optional[Dict[str, Any]] = None, started: Optional[datetime] = None) -> Dict[str, Any]:
        h = {
            "id": str(uuid.uuid4()), "kind": kind, "hostId": host_id, "target": target,
            "targetName": target_name, "image": image, "fromDigest": from_digest, "toDigest": to_digest,
            "mode": mode, "trigger": trigger, "outcome": outcome, "message": message, "scan": scan,
            "startedAt": rfc3339(started or now_utc()), "finishedAt": rfc3339(now_utc()),
        }
        try:
            self.store.add_autoupdate_history(h)
        except Exception as err:  # noqa: BLE001
            log.error("autoupdate: record history: %s", err)
        return h

    def alert(self, *, level: str, title: str, message: str, host_id: str, resource: str) -> None:
        bus = getattr(self.svc, "bus", None)
        if bus is None:
            return
        bus.publish(Event(type="alert", source="autoupdate", payload={
            "id": str(uuid.uuid4()), "ruleId": "", "hostId": host_id, "level": level, "title": title,
            "message": message, "resource": resource, "channelId": "", "resolved": False,
            "createdAt": rfc3339(now_utc()),
        }))

    # -- scanning hook -----------------------------------------------------------
    def scan_fn(self, host_id: str, ops: Any):
        scans = getattr(self.svc, "scans", None)
        if scans is None:
            return None

        async def run(image_insp: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            return await scans.scan(host_id, str(image_insp.get("Id") or ""), ops=ops, image_insp=image_insp)
        return run

    def verify_options(self) -> engine.VerifyOptions:
        cfg = self.settings()
        stable = cfg.get("stableSeconds")
        # 0 is a real choice (no stability wait), not "unset".
        return engine.VerifyOptions(timeout=float(cfg.get("verifyTimeoutSeconds") or 120),
                                    stable=float(10 if stable is None else stable))

    # -- digests ---------------------------------------------------------------
    async def _remote_digest(self, ref: Any, refresh: bool) -> Tuple[str, str]:
        from kowan.api import updates

        async def fetch() -> str:
            try:
                return await updates._remote_digest(self.store, ref)
            except Exception as err:  # noqa: BLE001
                raise ValueError(updates.friendly_registry_error(ref, err)) from err

        entry = await updates._CACHE.resolve(ref.key, fetch, refresh=refresh)
        return entry.digest, entry.error

    async def _pull(self, ops: Any, ref: Any) -> None:
        from kowan.api.updates import _drain_pull
        from kowan.registry import docker_auth_for

        auth = await docker_auth_for(self.store, ref.pull_ref)
        await _drain_pull(await ops.pull_image(ref.pull_ref, auth))

    # -- container ---------------------------------------------------------------
    async def process_container(self, host: Any, ops: Any, c: Any, eff: EffectivePolicy, *,
                                check_registry: bool, trigger: str = "auto") -> Dict[str, Any]:
        from kowan.api.updates import compare_digests, parse_image_ref

        name = container_name(c)
        key = ("container", host.id, name)
        if key in self._busy:
            return {"kind": "container", "target": name, "result": "busy"}
        self._busy.add(key)
        try:
            return await self._process_container(host, ops, c, eff, check_registry, trigger, name,
                                                 compare_digests, parse_image_ref)
        finally:
            self._busy.discard(key)

    async def _process_container(self, host, ops, c, eff, check_registry, trigger, name,
                                 compare_digests, parse_image_ref) -> Dict[str, Any]:
        kind, hid = "container", host.id
        state = self._state(kind, hid, name)
        now = now_utc()
        insp = await ops.inspect_container(c.id) or {}
        cfg_image = str((insp.get("Config") or {}).get("Image") or c.image or "")
        ref_str = tracked_ref(cfg_image, state)
        base = {"mode": eff.mode, "policy": eff.to_dict(), "image": ref_str, "lastCheckAt": rfc3339(now),
                "containerId": c.id, "hostId": hid}
        try:
            ref = parse_image_ref(ref_str)
        except ValueError as err:
            self._save_state(kind, hid, name, {**base, "lastResult": "unsupported", "lastMessage": str(err),
                                                "available": False, "pending": False})
            return {"kind": kind, "target": name, "result": "unsupported"}
        if ref.digest:
            self._save_state(kind, hid, name, {**base, "lastResult": "unsupported",
                                                "lastMessage": "image pinned by digest", "available": False,
                                                "pending": False})
            return {"kind": kind, "target": name, "result": "unsupported"}
        try:
            img = await ops.inspect_image(insp.get("Image") or cfg_image) or {}
        except Exception as err:  # noqa: BLE001
            img = {}
            log.debug("autoupdate: inspect image of %s: %s", name, err)
        repo_digests = img.get("RepoDigests") or []
        if not repo_digests:
            self._save_state(kind, hid, name, {**base, "lastResult": "unsupported",
                                                "lastMessage": "local image (no registry digest)",
                                                "available": False, "pending": False})
            return {"kind": kind, "target": name, "result": "unsupported"}

        remote = state.get("remoteDigest", "") if not check_registry else ""
        if check_registry or not remote:
            remote, err = await self._remote_digest(ref, refresh=trigger == "manual")
            if err or not remote:
                self._save_state(kind, hid, name, {**base, "lastResult": "error",
                                                    "lastMessage": err or "registry returned no digest"})
                return {"kind": kind, "target": name, "result": "error"}
        local, avail = compare_digests(repo_digests, ref, remote)
        patch: Dict[str, Any] = {**base, "currentDigest": local, "remoteDigest": remote, "available": avail}
        if remote != state.get("remoteDigest") or not state.get("firstSeenAt"):
            patch["firstSeenAt"] = rfc3339(now)
        first_seen = parse_rfc3339(patch.get("firstSeenAt") or state.get("firstSeenAt"))
        if not avail:
            patch.update(lastResult="up_to_date", lastMessage="running the latest image", pending=False)
            self._save_state(kind, hid, name, patch)
            return {"kind": kind, "target": name, "result": "up_to_date"}
        if remote == state.get("badDigest"):
            patch.update(lastResult="skipped", pending=False,
                         lastMessage="this image version was blocked or rolled back before; waiting for a newer one")
            self._save_state(kind, hid, name, patch)
            return {"kind": kind, "target": name, "result": "skipped"}

        alerts = eff.notify
        if eff.mode == MODE_NOTIFY:
            if state.get("notifiedDigest") != remote:
                msg = f"A newer image is available for {name} ({ref_str})"
                self.record(kind=kind, host_id=hid, target=name, target_name=name, image=ref_str, mode=eff.mode,
                            trigger=trigger, outcome="notified", message=msg, from_digest=local, to_digest=remote)
                if alerts:
                    self.alert(level="info", title="Update available", message=msg, host_id=hid, resource=name)
                patch["notifiedDigest"] = remote
            patch.update(lastResult="available", lastMessage="newer image available", pending=False)
            self._save_state(kind, hid, name, patch)
            return {"kind": kind, "target": name, "result": "available"}

        if eff.mode == MODE_PULL:
            if state.get("pulledDigest") != remote:
                started = now_utc()
                try:
                    await self._pull(ops, ref)
                    patch["pulledDigest"] = remote
                    msg = f"pulled {ref.pull_ref}; the container still runs the previous image"
                    self.record(kind=kind, host_id=hid, target=name, target_name=name, image=ref_str,
                                mode=eff.mode, trigger=trigger, outcome="pulled", message=msg, from_digest=local,
                                to_digest=remote, started=started)
                    if alerts:
                        self.alert(level="info", title="Update pulled", message=f"{name}: {msg}",
                                   host_id=hid, resource=name)
                except Exception as err:  # noqa: BLE001
                    patch.update(lastResult="error", lastMessage=f"pull failed: {err}")
                    self._save_state(kind, hid, name, patch)
                    return {"kind": kind, "target": name, "result": "error"}
            patch.update(lastResult="pulled", lastMessage="new image pulled, not applied", pending=False)
            self._save_state(kind, hid, name, patch)
            return {"kind": kind, "target": name, "result": "pulled"}

        # MODE_APPLY
        win = eff.window_obj
        local_now = datetime.now()
        if win is not None and not win.is_open(local_now):
            nxt = win.next_open(local_now)
            patch.update(lastResult="waiting", pending=True,
                         lastMessage="waiting for maintenance window" + (f" (opens {nxt:%a %H:%M})" if nxt else ""))
            self._save_state(kind, hid, name, patch)
            return {"kind": kind, "target": name, "result": "waiting"}
        min_age = eff.min_age_td
        if min_age is not None:
            created = None
            try:
                new_img = await ops.inspect_image(ref.pull_ref) or {}
                if remote in [d.partition("@")[2] for d in new_img.get("RepoDigests") or []]:
                    created = parse_docker_time(new_img.get("Created"))
            except Exception:  # noqa: BLE001 - not pulled yet
                pass
            ok, age = age_ok(min_age, first_seen, created, now)
            if not ok:
                patch.update(lastResult="waiting", pending=True,
                             lastMessage=f"waiting: new image age {format_duration(age) or 'unknown'} < {eff.min_age}")
                self._save_state(kind, hid, name, patch)
                return {"kind": kind, "target": name, "result": "waiting"}

        started = now_utc()
        out = await engine.apply_container_update(
            ops, c.id, image_ref=ref_str, pull=lambda: self._pull(ops, ref), scan=self.scan_fn(hid, ops),
            gate_policy=eff.scan_block, verify=self.verify_options(), insp=insp,
        )
        return self._finish_container(host, name, ref_str, eff, trigger, out, patch, started, local, remote, alerts)

    def _finish_container(self, host, name, ref_str, eff, trigger, out: engine.Outcome, patch, started, local,
                          remote, alerts) -> Dict[str, Any]:
        kind, hid = "container", host.id
        patch["pending"] = False
        patch["lastResult"] = out.outcome
        patch["lastMessage"] = out.message
        if out.outcome == engine.OUTCOME_UPDATED or out.outcome == engine.OUTCOME_CURRENT:
            patch.update(available=False, badDigest="", trackedRef="", rollbackRef="", containerId=out.container_id,
                         currentDigest=out.to_digest or remote)
        elif out.outcome in (engine.OUTCOME_BLOCKED, engine.OUTCOME_ROLLED_BACK, engine.OUTCOME_FAILED):
            patch["badDigest"] = remote
            if out.outcome == engine.OUTCOME_ROLLED_BACK:
                patch.update(trackedRef=ref_str, rollbackRef=out.rollback_ref, containerId=out.container_id)
        self._save_state(kind, hid, name, patch)
        if out.outcome != engine.OUTCOME_CURRENT:
            self.record(kind=kind, host_id=hid, target=name, target_name=name, image=ref_str, mode=eff.mode,
                        trigger=trigger, outcome=out.outcome, message=out.message, from_digest=local,
                        to_digest=out.to_digest or remote, scan=out.scan, started=started)
        if alerts:
            if out.outcome == engine.OUTCOME_BLOCKED:
                self.alert(level="warning", title="Update blocked", message=f"{name}: {out.message}",
                           host_id=hid, resource=name)
            elif out.outcome == engine.OUTCOME_ROLLED_BACK:
                self.alert(level="warning", title="Update rolled back", message=f"{name}: {out.message}",
                           host_id=hid, resource=name)
            elif out.outcome == engine.OUTCOME_FAILED:
                self.alert(level="critical", title="Update failed", message=f"{name}: {out.message}",
                           host_id=hid, resource=name)
        return {"kind": kind, "target": name, "result": out.outcome, "message": out.message}

    # -- deployment ----------------------------------------------------------------
    async def process_deployment(self, host: Any, ops: Any, dep: Any, members: List[Any], eff: EffectivePolicy, *,
                                 check_registry: bool, trigger: str = "auto") -> Dict[str, Any]:
        key = ("deployment", "", dep.id)
        if key in self._busy:
            return {"kind": "deployment", "target": dep.id, "result": "busy"}
        self._busy.add(key)
        try:
            return await self._process_deployment(host, ops, dep, members, eff, check_registry, trigger)
        finally:
            self._busy.discard(key)

    async def _process_deployment(self, host, ops, dep, members, eff, check_registry, trigger) -> Dict[str, Any]:
        from kowan.api.updates import compare_digests, parse_image_ref

        kind, target = "deployment", dep.id
        state = self._state(kind, "", target)
        now = now_utc()
        base = {"mode": eff.mode, "policy": eff.to_dict(), "lastCheckAt": rfc3339(now), "hostId": host.id,
                "name": dep.name}
        changed: List[Dict[str, Any]] = []
        errors: List[str] = []
        digests = dict(state.get("remoteDigests") or {})
        pins = dict(state.get("rollbackPins") or {})  # name -> {trackedRef, rollbackRef}
        for c in members:
            insp = await ops.inspect_container(c.id) or {}
            cfg_image = str((insp.get("Config") or {}).get("Image") or c.image or "")
            cfg_image = tracked_ref(cfg_image, pins.get(container_name(c)) or {})
            try:
                ref = parse_image_ref(cfg_image)
            except ValueError:
                continue
            if ref.digest:
                continue
            try:
                img = await ops.inspect_image(insp.get("Image") or cfg_image) or {}
            except Exception:  # noqa: BLE001
                img = {}
            if not img.get("RepoDigests"):
                continue
            remote = "" if check_registry else digests.get(ref.key, "")
            if not remote:
                remote, err = await self._remote_digest(ref, refresh=trigger == "manual")
                if err or not remote:
                    errors.append(f"{container_name(c)}: {err or 'no digest'}")
                    continue
            digests[ref.key] = remote
            local, avail = compare_digests(img.get("RepoDigests") or [], ref, remote)
            if avail:
                changed.append({"c": c, "insp": insp, "ref": ref, "ref_str": cfg_image, "local": local,
                                "remote": remote, "name": container_name(c)})
        sig = ",".join(sorted(f"{x['ref'].key}@{x['remote']}" for x in changed))
        patch: Dict[str, Any] = {**base, "remoteDigests": digests, "available": bool(changed),
                                 "changed": [x["name"] for x in changed]}
        if sig and sig != state.get("signature"):
            patch["signature"] = sig
            patch["firstSeenAt"] = rfc3339(now)
        first_seen = parse_rfc3339(patch.get("firstSeenAt") or state.get("firstSeenAt"))
        if not changed:
            patch.update(lastResult="error" if errors else "up_to_date", pending=False,
                         lastMessage="; ".join(errors) if errors else "all services run the latest images")
            self._save_state(kind, "", target, patch)
            return {"kind": kind, "target": target, "result": patch["lastResult"]}
        if sig == state.get("badSignature"):
            patch.update(lastResult="skipped", pending=False,
                         lastMessage="these image versions were blocked or rolled back before")
            self._save_state(kind, "", target, patch)
            return {"kind": kind, "target": target, "result": "skipped"}
        names = ", ".join(x["name"] for x in changed)
        images = ", ".join(sorted({x["ref_str"] for x in changed}))
        if eff.mode == MODE_NOTIFY:
            if state.get("notifiedSignature") != sig:
                msg = f"Newer images are available for deployment {dep.name} ({names})"
                self.record(kind=kind, host_id=host.id, target=target, target_name=dep.name, image=images,
                            mode=eff.mode, trigger=trigger, outcome="notified", message=msg)
                self.alert(level="info", title="Update available", message=msg, host_id=host.id, resource=dep.name)
                patch["notifiedSignature"] = sig
            patch.update(lastResult="available", lastMessage=f"newer images for {names}", pending=False)
            self._save_state(kind, "", target, patch)
            return {"kind": kind, "target": target, "result": "available"}
        if eff.mode == MODE_PULL:
            if state.get("pulledSignature") != sig:
                try:
                    for ref in {x["ref"].key: x["ref"] for x in changed}.values():
                        await self._pull(ops, ref)
                    patch["pulledSignature"] = sig
                    self.record(kind=kind, host_id=host.id, target=target, target_name=dep.name, image=images,
                                mode=eff.mode, trigger=trigger, outcome="pulled",
                                message=f"pulled new images for {names}; not redeployed")
                except Exception as err:  # noqa: BLE001
                    patch.update(lastResult="error", lastMessage=f"pull failed: {err}")
                    self._save_state(kind, "", target, patch)
                    return {"kind": kind, "target": target, "result": "error"}
            patch.update(lastResult="pulled", lastMessage="new images pulled, not applied", pending=False)
            self._save_state(kind, "", target, patch)
            return {"kind": kind, "target": target, "result": "pulled"}

        win = eff.window_obj
        local_now = datetime.now()
        if win is not None and not win.is_open(local_now):
            nxt = win.next_open(local_now)
            patch.update(lastResult="waiting", pending=True,
                         lastMessage="waiting for maintenance window" + (f" (opens {nxt:%a %H:%M})" if nxt else ""))
            self._save_state(kind, "", target, patch)
            return {"kind": kind, "target": target, "result": "waiting"}
        ok, age = age_ok(eff.min_age_td, first_seen, None, now)
        if not ok:
            patch.update(lastResult="waiting", pending=True,
                         lastMessage=f"waiting: new images age {format_duration(age) or 'unknown'} < {eff.min_age}")
            self._save_state(kind, "", target, patch)
            return {"kind": kind, "target": target, "result": "waiting"}

        started = now_utc()
        outcome, message, scan, new_pins = await self._apply_deployment(host, ops, dep, changed, eff)
        patch.update(lastResult=outcome, lastMessage=message, pending=False)
        if outcome in (engine.OUTCOME_BLOCKED, engine.OUTCOME_ROLLED_BACK, engine.OUTCOME_FAILED):
            patch["badSignature"] = sig
            if new_pins:
                pins.update(new_pins)
                patch["rollbackPins"] = pins
        else:
            patch.update(available=False, badSignature="", changed=[], rollbackPins={})
        self._save_state(kind, "", target, patch)
        self.record(kind=kind, host_id=host.id, target=target, target_name=dep.name, image=images, mode=eff.mode,
                    trigger=trigger, outcome=outcome, message=message, scan=scan, started=started)
        level = {"blocked": "warning", "rolled_back": "warning", "failed": "critical"}.get(outcome)
        if level:
            self.alert(level=level, title=f"Deployment update {outcome.replace('_', ' ')}",
                       message=f"{dep.name}: {message}", host_id=host.id, resource=dep.name)
        return {"kind": kind, "target": target, "result": outcome, "message": message}

    async def _apply_deployment(self, host, ops, dep, changed, eff) -> Tuple[str, str, Optional[Dict[str, Any]], Dict[str, Any]]:
        """Pull changed images, gate them, redeploy via the deployment service,
        verify every changed container, roll those back on failure."""
        from kowan.scan.gate import BLOCK_NEVER, gate_decision
        from kowan.scan.trivy import sum_counts

        refs = {x["ref"].key: x["ref"] for x in changed}
        try:
            for ref in refs.values():
                await self._pull(ops, ref)
        except Exception as err:  # noqa: BLE001
            return engine.OUTCOME_FAILED, f"pull failed: {err}", None, {}
        scan_counts: Optional[Dict[str, Any]] = None
        scan = self.scan_fn(host.id, ops)
        if eff.scan_block != BLOCK_NEVER and scan is not None:
            all_counts = []
            for x in changed:
                try:
                    new_img = await ops.inspect_image(x["ref"].pull_ref) or {}
                    new_rec = await scan(new_img)
                    cur_rec = None
                    if eff.scan_block == "worse":
                        try:
                            cur_rec = await scan(await ops.inspect_image(x["insp"].get("Image")) or {})
                        except Exception:  # noqa: BLE001
                            cur_rec = None
                    new_c = new_rec.get("counts") if new_rec and new_rec.get("status") == "ok" else None
                    cur_c = cur_rec.get("counts") if cur_rec and cur_rec.get("status") == "ok" else None
                except Exception:  # noqa: BLE001
                    new_c, cur_c = None, None
                all_counts.append(new_c)
                blocked, reason = gate_decision(eff.scan_block, new_c, cur_c)
                if blocked:
                    return (engine.OUTCOME_BLOCKED, f"{x['name']}: blocked by scan policy '{eff.scan_block}': {reason}",
                            sum_counts(all_counts), {})
            scan_counts = sum_counts(all_counts)

        old = {x["name"]: x for x in changed}
        deployments = getattr(self.svc, "deployments", None)
        if deployments is None:
            return engine.OUTCOME_FAILED, "deployment service unavailable", scan_counts, {}
        try:
            await deployments.redeploy(dep.id)
        except Exception as err:  # noqa: BLE001
            redeploy_err = str(err)
        else:
            redeploy_err = ""
        # Verify every changed service container (found again by name).
        failures: List[str] = []
        current = {container_name(c): c for c in await ops.list_containers(True)}
        opts = self.verify_options()
        if redeploy_err:
            failures.append(f"redeploy: {redeploy_err}")
        else:
            for nm, x in old.items():
                c = current.get(nm)
                if c is None:
                    failures.append(f"{nm}: container missing after redeploy")
                    continue
                if not (x["insp"].get("State") or {}).get("Running"):
                    continue
                ok, reason = await engine.verify_container(ops, c.id, opts)
                if not ok:
                    failures.append(f"{nm}: {reason}")
        if not failures:
            return engine.OUTCOME_UPDATED, f"redeployed {', '.join(old)} and verified", scan_counts, {}
        # Roll back every changed container to its previous image.
        from kowan.api.updates import recreate_with_image

        notes = []
        pins: Dict[str, Any] = {}
        current = {container_name(c): c for c in await ops.list_containers(True)}
        for nm, x in old.items():
            try:
                old_img = await ops.inspect_image(x["insp"].get("Image")) or {}
            except Exception:  # noqa: BLE001
                old_img = {}
            rb_ref = engine.rollback_ref_for(old_img, x["ref"]) if old_img else str(x["insp"].get("Image") or "")
            c = current.get(nm)
            try:
                if c is None:
                    raise RuntimeError("container missing")
                insp = dict(await ops.inspect_container(c.id) or {})
                if insp.get("Image") == x["insp"].get("Image") and (insp.get("State") or {}).get("Running"):
                    continue
                # Come back up like the original did, even if the failed one is down.
                was_running = bool((x["insp"].get("State") or {}).get("Running"))
                insp["State"] = {**(insp.get("State") or {}), "Running": was_running}
                await recreate_with_image(ops, c.id, insp, rb_ref)
                pins[nm] = {"trackedRef": x["ref_str"], "rollbackRef": rb_ref}
            except Exception as err:  # noqa: BLE001
                notes.append(f"{nm}: rollback failed: {err}")
        msg = "verification failed (" + "; ".join(failures) + "); rolled back changed services to their previous images"
        if notes:
            return engine.OUTCOME_FAILED, msg + " - " + "; ".join(notes), scan_counts, pins
        return engine.OUTCOME_ROLLED_BACK, msg + " (a manual redeploy would use the new images again)", scan_counts, pins
