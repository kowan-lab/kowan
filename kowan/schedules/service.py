"""Scheduled start/stop: validation, label discovery, execution, history.

A schedule runs one ``action`` on one target at the times given by a cron
expression in an IANA timezone:

=============  ==========================  ==============================
target type    target id                   actions
=============  ==========================  ==============================
container      container name (stable      start | stop | restart
               across recreate)
deployment     Kowan deployment id          start | stop | restart | deploy
stack          compose project name        start | stop | restart
=============  ==========================  ==============================

Labels. A container may carry ``kowan.schedule.stop``, ``kowan.schedule.start``
and ``kowan.schedule.restart`` (cron values) plus an optional
``kowan.schedule.timezone``. They are discovered by scanning every host's
container list (every :data:`LABEL_SCAN_INTERVAL` seconds and on demand) and
become read-only ``source="label"`` schedules. **Precedence:** a stored
schedule for the same host + container + action, enabled or not, overrides the
label (an operator disabling a schedule must be able to silence a label).

``onlyIf.noExecSession`` (stop/restart of a container only): the run is
skipped when Docker reports exec instances on the container (inspect
``ExecIDs``). Best effort: Docker keeps an exited exec listed for a short
while, so a just-closed console can still cause one skip.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Tuple

from kowan import errors

from .cron import CronParseError, iso, next_run, next_runs, parse_cron, parse_iso, resolve_tz
from .runner import CronEntry, CronRunner

if TYPE_CHECKING:  # pragma: no cover
    from kowan.web import Services

log = logging.getLogger("kowan.schedules")

UTC = timezone.utc

TARGET_CONTAINER = "container"
TARGET_DEPLOYMENT = "deployment"
TARGET_STACK = "stack"

ACTIONS: Dict[str, Tuple[str, ...]] = {
    TARGET_CONTAINER: ("start", "stop", "restart"),
    TARGET_DEPLOYMENT: ("start", "stop", "restart", "deploy"),
    TARGET_STACK: ("start", "stop", "restart"),
}

LABEL_PREFIX = "kowan.schedule."
LABEL_TZ = "kowan.schedule.timezone"
LABEL_ACTIONS = ("start", "stop", "restart")
LABEL_COMPOSE_PROJECT = "com.docker.compose.project"

LABEL_SCAN_INTERVAL = 300.0
RUN_RETENTION_DAYS = 90

STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

_ALLOWED_FIELDS = {"name", "targetType", "targetId", "hostId", "action", "cron", "timezone", "enabled", "onlyIf"}
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def validate_schedule(body: Any, existing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Validate a create/update body into a normalized schedule dict.
    Raises :class:`kowan.errors.Validation`."""
    if not isinstance(body, dict):
        raise errors.Validation("invalid request body")
    unknown = set(body) - _ALLOWED_FIELDS - {"id", "source", "nextRun", "lastRunAt", "lastStatus", "createdAt", "updatedAt"}
    if unknown:
        raise errors.Validation("unknown field(s): " + ", ".join(sorted(unknown)))
    s: Dict[str, Any] = dict(existing or {})
    for k in _ALLOWED_FIELDS:
        if k in body:
            s[k] = body[k]
    tt = str(s.get("targetType") or "")
    if tt not in ACTIONS:
        raise errors.Validation("targetType must be container, deployment or stack")
    tid = str(s.get("targetId") or "").strip().lstrip("/")
    if not tid:
        raise errors.Validation("targetId is required")
    s["targetId"] = tid
    action = str(s.get("action") or "")
    if action not in ACTIONS[tt]:
        raise errors.Validation(f"action for a {tt} must be one of: {', '.join(ACTIONS[tt])}")
    host = str(s.get("hostId") or "").strip()
    if tt != TARGET_DEPLOYMENT and not host:
        raise errors.Validation("hostId is required for container and stack schedules")
    s["hostId"] = host
    cron = str(s.get("cron") or "").strip()
    try:
        parse_cron(cron)
    except CronParseError as err:
        raise errors.Validation(f"invalid cron: {err}")
    s["cron"] = cron
    tz = str(s.get("timezone") or "").strip()
    try:
        resolve_tz(tz)
    except ValueError as err:
        raise errors.Validation(str(err))
    s["timezone"] = tz
    if not isinstance(s.get("enabled", True), bool):
        raise errors.Validation("enabled must be a boolean")
    s["enabled"] = bool(s.get("enabled", True))
    only = s.get("onlyIf") or {}
    if not isinstance(only, dict):
        raise errors.Validation("onlyIf must be an object")
    only = {k: bool(v) for k, v in only.items() if k == "noExecSession"}
    if only.get("noExecSession") and not (tt == TARGET_CONTAINER and action in ("stop", "restart")):
        raise errors.Validation("onlyIf.noExecSession applies to container stop/restart only")
    s["onlyIf"] = only
    name = str(s.get("name") or "").strip()
    s["name"] = name or f"{action} {tid}"
    return s


# ---------------------------------------------------------------------------
# labels
# ---------------------------------------------------------------------------


def label_id(host_id: str, container: str, action: str) -> str:
    return f"label:{host_id}:{container}:{action}"


def schedules_from_labels(host_id: str, container: str, labels: Dict[str, str]) -> List[Dict[str, Any]]:
    """Derive read-only schedules from a container's ``kowan.schedule.*`` labels.
    Invalid cron values / timezones are returned with ``error`` set (never
    scheduled) so the UI can show why a label is ignored."""
    out: List[Dict[str, Any]] = []
    labels = labels or {}
    tz = (labels.get(LABEL_TZ) or "").strip()
    for action in LABEL_ACTIONS:
        spec = (labels.get(LABEL_PREFIX + action) or "").strip()
        if not spec:
            continue
        err = ""
        try:
            parse_cron(spec)
            resolve_tz(tz)
        except (CronParseError, ValueError) as e:
            err = str(e)
        out.append(
            {
                "id": label_id(host_id, container, action),
                "name": f"{action} {container} (label)",
                "targetType": TARGET_CONTAINER,
                "targetId": container,
                "hostId": host_id,
                "action": action,
                "cron": spec,
                "timezone": tz,
                "enabled": err == "",
                "onlyIf": {},
                "source": "label",
                "error": err,
                "lastRunAt": "",
                "lastStatus": "",
                "createdAt": "",
                "updatedAt": "",
            }
        )
    return out


def apply_precedence(manual: Iterable[Dict[str, Any]], labeled: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop label schedules overridden by a stored schedule for the same host,
    container and action (stored wins, enabled or not)."""
    taken = {
        (m.get("hostId"), m.get("targetId"), m.get("action"))
        for m in manual
        if m.get("targetType") == TARGET_CONTAINER
    }
    return [l for l in labeled if (l["hostId"], l["targetId"], l["action"]) not in taken]


def _container_name(c: Any) -> str:
    names = getattr(c, "names", None) or []
    return (names[0] if names else getattr(c, "id", "")).lstrip("/")


# ---------------------------------------------------------------------------
# service
# ---------------------------------------------------------------------------


class ScheduleService:
    def __init__(self, svc: "Services", *, clock=_now) -> None:
        self.svc = svc
        self.store = svc.store
        self._clock = clock
        # host id -> list of label schedules from the last successful scan.
        self._labels: Dict[str, List[Dict[str, Any]]] = {}
        self.runner = CronRunner("schedules", self._load_entries, self._fire, self._on_skip, clock=clock)
        self._scan_task: Optional[asyncio.Task] = None

    # -- lifecycle --------------------------------------------------------
    async def start(self) -> None:
        await self.runner.start()
        self._scan_task = asyncio.ensure_future(self._scan_loop())

    async def stop(self) -> None:
        if self._scan_task is not None:
            self._scan_task.cancel()
            try:
                await self._scan_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._scan_task = None
        await self.runner.stop()

    async def _scan_loop(self) -> None:
        while True:
            try:
                await self.scan_labels()
            except Exception as err:  # noqa: BLE001
                log.warning("schedules: label scan: %s", err)
            try:
                before = iso(self._clock() - timedelta(days=RUN_RETENTION_DAYS))
                self.store.prune_schedule_runs(before)
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(LABEL_SCAN_INTERVAL)

    # -- label discovery --------------------------------------------------
    async def scan_labels(self) -> None:
        try:
            hosts = self.store.list_hosts()
        except Exception as err:  # noqa: BLE001
            log.warning("schedules: list hosts: %s", err)
            return
        changed = False
        for h in hosts:
            try:
                ops = self.svc.docker.ops(h)
                containers = await asyncio.wait_for(ops.list_containers(True), timeout=20)
            except Exception:  # noqa: BLE001 - offline host: keep the previous scan
                continue
            found: List[Dict[str, Any]] = []
            for c in containers:
                labels = getattr(c, "labels", None) or {}
                if not any(k.startswith(LABEL_PREFIX) for k in labels):
                    continue
                found.extend(schedules_from_labels(h.id, _container_name(c), labels))
            if found != self._labels.get(h.id, []):
                changed = True
            self._labels[h.id] = found
        known = {h.id for h in hosts}
        for hid in list(self._labels):
            if hid not in known:
                self._labels.pop(hid, None)
                changed = True
        if changed:
            self.runner.reload()

    def label_schedules(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for v in self._labels.values():
            out.extend(v)
        return out

    # -- listing ----------------------------------------------------------
    def all_schedules(self) -> List[Dict[str, Any]]:
        manual = self.store.list_schedules()
        labeled = apply_precedence(manual, self.label_schedules())
        out = []
        for s in manual + labeled:
            s = dict(s)
            if s.get("source") == "label":
                last = self.store.last_schedule_run(s["id"])
                if last:
                    s["lastRunAt"] = last.get("scheduledFor") or last.get("startedAt")
                    s["lastStatus"] = last.get("status", "")
            due = self.runner.next_due(s["id"]) if s.get("enabled") else None
            if due is None and s.get("enabled") and not s.get("error"):
                try:
                    due = next_run(s["cron"], self._clock(), s.get("timezone", ""))
                except (CronParseError, ValueError):
                    due = None
            s["nextRun"] = iso(due)
            out.append(s)
        return out

    def find(self, sid: str) -> Dict[str, Any]:
        for s in self.all_schedules():
            if s["id"] == sid:
                return s
        raise errors.NotFound("schedule not found")

    # -- runner glue ------------------------------------------------------
    def _load_entries(self) -> List[CronEntry]:
        manual = self.store.list_schedules()
        labeled = apply_precedence(manual, self.label_schedules())
        entries: List[CronEntry] = []
        for s in manual:
            if not s["enabled"]:
                continue
            entries.append(
                CronEntry(
                    key=s["id"],
                    spec=s["cron"],
                    tz=s["timezone"],
                    last_run=parse_iso(s.get("lastRunAt", "")),
                    anchor=parse_iso(s.get("updatedAt") or s.get("createdAt", "")),
                    payload=s,
                )
            )
        for s in labeled:
            if not s["enabled"]:
                continue
            last = self.store.last_schedule_run(s["id"])
            entries.append(
                CronEntry(
                    key=s["id"],
                    spec=s["cron"],
                    tz=s["timezone"],
                    last_run=parse_iso((last or {}).get("scheduledFor", "")),
                    anchor=None,  # discovered now: first slot is the next future one
                    payload=s,
                )
            )
        return entries

    def reload(self) -> None:
        self.runner.reload()

    async def _fire(self, entry: CronEntry, due: datetime) -> None:
        await self.execute(entry.payload, trigger="schedule", scheduled_for=due)

    def _on_skip(self, entry: CronEntry, due: datetime, reason: str) -> None:
        s = entry.payload
        self._record(s, "schedule", due, STATUS_SKIPPED, reason, started=self._clock())

    def _record(self, s: Dict[str, Any], trigger: str, due: Optional[datetime], status: str,
                message: str, started: datetime) -> Dict[str, Any]:
        run = self.store.add_schedule_run(
            {
                "scheduleId": s["id"],
                "scheduleName": s.get("name", ""),
                "targetType": s["targetType"],
                "targetId": s["targetId"],
                "hostId": s.get("hostId", ""),
                "action": s["action"],
                "trigger": trigger,
                "status": status,
                "message": message,
                "scheduledFor": iso(due),
                "startedAt": iso(started),
                "finishedAt": iso(self._clock()),
            }
        )
        if s.get("source") != "label" and trigger == "schedule":
            try:
                self.store.mark_schedule_run(s["id"], iso(due), status)
            except Exception:  # noqa: BLE001 - deleted meanwhile
                pass
        elif s.get("source") != "label":
            try:
                self.store.mark_schedule_run(s["id"], s.get("lastRunAt", "") or "", status)
            except Exception:  # noqa: BLE001
                pass
        if status == STATUS_FAILED:
            self._notice(s, message)
        return run

    def _notice(self, s: Dict[str, Any], message: str) -> None:
        bus = getattr(self.svc, "bus", None)
        if bus is None:
            return
        from kowan.events import Event

        bus.publish(
            Event(
                type="notice",
                source="schedules",
                payload={
                    "level": "warning",
                    "title": f"Schedule failed: {s.get('name', '')}",
                    "body": message,
                    "hostId": s.get("hostId", ""),
                    "source": "schedules",
                    "refId": s["id"],
                },
            )
        )

    # -- execution --------------------------------------------------------
    async def execute(self, s: Dict[str, Any], *, trigger: str, scheduled_for: Optional[datetime] = None) -> Dict[str, Any]:
        started = self._clock()
        try:
            status, message = await self._do(s)
        except Exception as err:  # noqa: BLE001
            status, message = STATUS_FAILED, str(err) or err.__class__.__name__
        log.info("schedule %s (%s %s %s): %s %s", s["id"], s["action"], s["targetType"], s["targetId"], status, message)
        return self._record(s, trigger, scheduled_for, status, message, started)

    def _ops(self, host_id: str):
        try:
            host = self.store.get_host(host_id)
        except Exception:  # noqa: BLE001
            raise errors.NotFound(f"host {host_id!r} not found")
        return self.svc.docker.ops(host)

    async def _do(self, s: Dict[str, Any]) -> Tuple[str, str]:
        tt, action, tid = s["targetType"], s["action"], s["targetId"]
        if tt == TARGET_DEPLOYMENT:
            deps = getattr(self.svc, "deployments", None)
            if deps is None:
                raise errors.Validation("deployment service unavailable")
            if action == "start":
                await deps.start(tid)
            elif action == "stop":
                await deps.stop(tid)
            elif action == "restart":
                await deps.stop(tid)
                await deps.start(tid)
            elif action == "deploy":
                await deps.redeploy(tid)
            return STATUS_SUCCESS, f"deployment {action} done"

        ops = self._ops(s["hostId"])
        containers = await ops.list_containers(True)
        if tt == TARGET_CONTAINER:
            targets = [c for c in containers if _container_name(c) == tid]
            if not targets:
                return STATUS_FAILED, f"container {tid!r} not found on host"
        else:
            targets = [c for c in containers if (getattr(c, "labels", None) or {}).get(LABEL_COMPOSE_PROJECT) == tid]
            if not targets:
                return STATUS_FAILED, f"stack {tid!r} has no containers on host"

        if (s.get("onlyIf") or {}).get("noExecSession") and action in ("stop", "restart"):
            for c in targets:
                info = await ops.inspect_container(c.id)
                if info.get("ExecIDs"):
                    return STATUS_SKIPPED, "skipped: an exec session is open on the container"

        done: List[str] = []
        for c in targets:
            state = (getattr(c, "state", "") or "").lower()
            name = _container_name(c)
            if action == "start":
                if state == "running":
                    continue
                await ops.start_container(c.id)
            elif action == "stop":
                if state not in ("running", "restarting", "paused"):
                    continue
                await ops.stop_container(c.id)
            else:
                await ops.restart_container(c.id)
            done.append(name)
        if not done:
            return STATUS_SUCCESS, f"nothing to {action}: already in the desired state"
        return STATUS_SUCCESS, f"{action}: {', '.join(done)}"

    # -- previews ---------------------------------------------------------
    def preview(self, cron: str, tz: str, count: int = 5) -> List[str]:
        try:
            parse_cron(cron)
        except CronParseError as err:
            raise errors.Validation(f"invalid cron: {err}")
        try:
            resolve_tz(tz)
        except ValueError as err:
            raise errors.Validation(str(err))
        return [iso(t) for t in next_runs(cron, self._clock(), tz, max(1, min(count, 20)))]
