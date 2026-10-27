"""Volume backup service: repositories, jobs, selection, hooks, backup/restore,
snapshot browsing, run history, failure alerts and the cron schedule.

Execution model (see docs/volume-backups.md):

1. Resolve the job's volumes on its host (by name, by deployment, by compose
   stack, or by the ``kowan.backup=true`` label on volumes/containers).
2. Pre hooks: ``exec`` a command in a container (e.g. ``pg_dump`` into the
   volume); optionally stop the running containers that mount the volumes.
3. One short-lived ``restic/restic`` container on the job's host, volumes
   mounted read-only under ``/data/<name>``: open the repository (``init`` on
   first use), one ``restic backup --json`` per volume (one snapshot per
   volume, tagged ``kowan-volume=<name>`` and ``kowan-job=<id>``), then
   ``restic forget --prune`` with the job's retention.
4. Restart stopped containers, post hooks, record the run (status, stats from
   the JSON summaries, duration, redacted log); a failure raises an alert that
   flows to the notification channels, the next success resolves it.

Mutating restic operations on one repository are serialized with a per-repo
lock (``forget --prune`` needs restic's exclusive lock). Listing/browsing uses
``--no-lock``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from kowan import errors
from kowan.schedules.cron import CronParseError, iso, next_run, parse_cron, parse_iso, resolve_tz
from kowan.schedules.runner import CronEntry, CronRunner
from kowan.store import NotFoundError
from kowan.store._common import MASK_VALUE

from . import restic
from .executor import HelperResult, run_helper

if TYPE_CHECKING:  # pragma: no cover
    from kowan.web import Services

log = logging.getLogger("kowan.volbackup")

UTC = timezone.utc

LABEL_BACKUP = "kowan.backup"
LABEL_DEPLOYMENT_ID = "kowan.deployment.id"
LABEL_COMPOSE_PROJECT = "com.docker.compose.project"

SELECTOR_MODES = ("names", "deployment", "stack", "label")

STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_WARNING = "warning"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

RUN_RETENTION_DAYS = 180
SNAPSHOT_CACHE_TTL = 30.0
LIST_TIMEOUT = 300.0
BACKUP_TIMEOUT = 12 * 3600.0

_HOOK_EXIT_RE = re.compile(r"@@exit=(\d+)\s*$")
_TRUE = ("true", "1", "yes", "on")


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# validation (pure)
# ---------------------------------------------------------------------------


def validate_repo(body: Any, existing: Optional[Dict[str, Any]] = None) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """Validate a repository create/update body -> (record, incoming secrets)."""
    if not isinstance(body, dict):
        raise errors.Validation("invalid request body")
    unknown = set(body) - {"name", "type", "hostId", "config", "secrets", "id", "initialized", "createdAt", "updatedAt"}
    if unknown:
        raise errors.Validation("unknown field(s): " + ", ".join(sorted(unknown)))
    repo = dict(existing or {})
    repo.pop("secrets", None)
    for k in ("name", "type", "hostId", "config"):
        if k in body:
            repo[k] = body[k]
    name = str(repo.get("name") or "").strip()
    if not name or len(name) > 100:
        raise errors.Validation("name is required (max 100 characters)")
    repo["name"] = name
    t = str(repo.get("type") or "")
    if t not in restic.REPO_TYPES:
        raise errors.Validation("type must be one of: " + ", ".join(restic.REPO_TYPES))
    if existing and existing.get("type") != t:
        raise errors.Validation("the repository type cannot be changed")
    cfg = repo.get("config") or {}
    if not isinstance(cfg, dict):
        raise errors.Validation("config must be an object")
    allowed = set(restic.CONFIG_KEYS[t]) | set(restic.COMMON_CONFIG_KEYS)
    bad = set(cfg) - allowed
    if bad:
        raise errors.Validation(f"unknown config key(s) for {t}: " + ", ".join(sorted(bad)))
    repo["config"] = {k: str(v).strip() for k, v in cfg.items() if v not in (None, "")}
    repo["hostId"] = str(repo.get("hostId") or "").strip()
    if t == "local" and not repo["hostId"]:
        raise errors.Validation("a local repository needs the host whose disk holds it (hostId)")
    if t != "local":
        repo["hostId"] = ""
    secrets = body.get("secrets") or {}
    if not isinstance(secrets, dict):
        raise errors.Validation("secrets must be an object")
    bad = set(secrets) - set(restic.SECRET_KEYS[t])
    if bad:
        raise errors.Validation(f"unknown secret key(s) for {t}: " + ", ".join(sorted(bad)))
    secrets = {k: str(v) for k, v in secrets.items() if v is not None}
    return repo, secrets


def _hook_list(v: Any, field: str) -> List[Dict[str, str]]:
    if v in (None, ""):
        return []
    if not isinstance(v, list) or len(v) > 10:
        raise errors.Validation(f"hooks.{field} must be a list (max 10)")
    out = []
    for i, h in enumerate(v):
        if not isinstance(h, dict):
            raise errors.Validation(f"hooks.{field}[{i}] must be an object")
        c = str(h.get("container") or "").strip().lstrip("/")
        cmd = str(h.get("command") or "").strip()
        if not c or not cmd:
            raise errors.Validation(f"hooks.{field}[{i}] needs container and command")
        if len(cmd) > 4000:
            raise errors.Validation(f"hooks.{field}[{i}].command is too long")
        out.append({"container": c, "command": cmd})
    return out


def validate_job(body: Any, existing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not isinstance(body, dict):
        raise errors.Validation("invalid request body")
    fields = {"name", "hostId", "repoId", "selector", "schedule", "timezone", "retention", "hooks", "tags", "enabled"}
    unknown = set(body) - fields - {"id", "alertId", "lastRunAt", "createdAt", "updatedAt", "nextRun", "lastRun"}
    if unknown:
        raise errors.Validation("unknown field(s): " + ", ".join(sorted(unknown)))
    job = dict(existing or {})
    for k in fields:
        if k in body:
            job[k] = body[k]
    name = str(job.get("name") or "").strip()
    if not name or len(name) > 100:
        raise errors.Validation("name is required (max 100 characters)")
    job["name"] = name
    for k in ("hostId", "repoId"):
        job[k] = str(job.get(k) or "").strip()
        if not job[k]:
            raise errors.Validation(f"{k} is required")
    sel = job.get("selector") or {}
    if not isinstance(sel, dict):
        raise errors.Validation("selector must be an object")
    mode = str(sel.get("mode") or "names")
    if mode not in SELECTOR_MODES:
        raise errors.Validation("selector.mode must be one of: " + ", ".join(SELECTOR_MODES))
    norm: Dict[str, Any] = {"mode": mode}
    if mode == "names":
        vols = sel.get("volumes") or []
        if not isinstance(vols, list) or not vols:
            raise errors.Validation("selector.volumes must list at least one volume")
        for v in vols:
            if not restic.valid_volume_name(str(v)):
                raise errors.Validation(f"invalid volume name {v!r}")
        norm["volumes"] = sorted({str(v) for v in vols})
    elif mode == "deployment":
        d = str(sel.get("deploymentId") or "").strip()
        if not d:
            raise errors.Validation("selector.deploymentId is required")
        norm["deploymentId"] = d
    elif mode == "stack":
        st = str(sel.get("stack") or "").strip()
        if not st:
            raise errors.Validation("selector.stack is required")
        norm["stack"] = st
    exclude = sel.get("exclude") or []
    if not isinstance(exclude, list):
        raise errors.Validation("selector.exclude must be a list")
    if exclude:
        norm["exclude"] = sorted({str(v) for v in exclude})
    job["selector"] = norm
    sched = str(job.get("schedule") or "").strip()
    if sched:
        try:
            parse_cron(sched)
        except CronParseError as err:
            raise errors.Validation(f"invalid schedule: {err}")
    job["schedule"] = sched
    tz = str(job.get("timezone") or "").strip()
    try:
        resolve_tz(tz)
    except ValueError as err:
        raise errors.Validation(str(err))
    job["timezone"] = tz
    try:
        job["retention"] = restic.normalize_retention(job.get("retention"))
    except ValueError as err:
        raise errors.Validation(str(err))
    hooks = job.get("hooks") or {}
    if not isinstance(hooks, dict):
        raise errors.Validation("hooks must be an object")
    bad = set(hooks) - {"stopContainers", "pre", "post"}
    if bad:
        raise errors.Validation("unknown hook key(s): " + ", ".join(sorted(bad)))
    job["hooks"] = {
        "stopContainers": bool(hooks.get("stopContainers", False)),
        "pre": _hook_list(hooks.get("pre"), "pre"),
        "post": _hook_list(hooks.get("post"), "post"),
    }
    tags = job.get("tags") or []
    if not isinstance(tags, list) or len(tags) > 20:
        raise errors.Validation("tags must be a list (max 20)")
    for t in tags:
        if not restic.valid_tag(str(t)) or str(t).startswith(("kowan-volume=", "kowan-job=")) or t == "kowan":
            raise errors.Validation(f"invalid tag {t!r} (letters, digits and _.:=@/+-, max 64)")
    job["tags"] = [str(t) for t in tags]
    if not isinstance(job.get("enabled", True), bool):
        raise errors.Validation("enabled must be a boolean")
    job["enabled"] = bool(job.get("enabled", True))
    return job


def parse_hook_output(out: str) -> Tuple[bool, str]:
    """Split ``<output>\\n@@exit=N`` (see :func:`hook_command`)."""
    lines = (out or "").rstrip("\n").splitlines()
    code = None
    if lines:
        m = _HOOK_EXIT_RE.search(lines[-1])
        if m:
            code = int(m.group(1))
            lines = lines[:-1]
    text = "\n".join(lines)
    return code == 0, text if code is not None else (text + "\n(no exit status)").strip()


def hook_command(command: str) -> List[str]:
    return ["sh", "-c", command + '\necho "@@exit=$?"']


def _container_name(c: Any) -> str:
    names = getattr(c, "names", None) or []
    return (names[0] if names else getattr(c, "id", "")).lstrip("/")


def _volume_mounts(info: Dict[str, Any]) -> List[str]:
    return [
        str(m.get("Name"))
        for m in (info.get("Mounts") or [])
        if m.get("Type") == "volume" and m.get("Name")
    ]


async def resolve_volumes(ops, selector: Dict[str, Any], *, deployment_project: str = "") -> List[str]:
    """The volume names a selector picks on a host (sorted, unique)."""
    mode = selector.get("mode", "names")
    exclude = set(selector.get("exclude") or [])
    found: set = set()
    if mode == "names":
        found = set(selector.get("volumes") or [])
    else:
        vols = await ops.list_volumes()
        containers = await ops.list_containers(True)

        def vol_labels(v) -> Dict[str, str]:
            return (getattr(v, "labels", None) or {}) if not isinstance(v, dict) else (v.get("labels") or v.get("Labels") or {})

        def vol_name(v) -> str:
            return getattr(v, "name", "") if not isinstance(v, dict) else (v.get("name") or v.get("Name") or "")

        picked = []
        if mode == "deployment":
            dep_id = selector.get("deploymentId", "")
            picked = [c for c in containers if (c.labels or {}).get(LABEL_DEPLOYMENT_ID) == dep_id]
            if deployment_project:
                picked += [c for c in containers if (c.labels or {}).get(LABEL_COMPOSE_PROJECT) == deployment_project]
                found |= {vol_name(v) for v in vols if vol_labels(v).get(LABEL_COMPOSE_PROJECT) == deployment_project}
        elif mode == "stack":
            st = selector.get("stack", "")
            picked = [c for c in containers if (c.labels or {}).get(LABEL_COMPOSE_PROJECT) == st]
            found |= {vol_name(v) for v in vols if vol_labels(v).get(LABEL_COMPOSE_PROJECT) == st}
        elif mode == "label":
            picked = [c for c in containers if str((c.labels or {}).get(LABEL_BACKUP, "")).lower() in _TRUE]
            found |= {vol_name(v) for v in vols if str(vol_labels(v).get(LABEL_BACKUP, "")).lower() in _TRUE}
        seen = set()
        for c in picked:
            if c.id in seen:
                continue
            seen.add(c.id)
            info = await ops.inspect_container(c.id)
            found |= set(_volume_mounts(info or {}))
    return sorted(v for v in found if v and v not in exclude and restic.valid_volume_name(v))


async def containers_using(ops, volumes: List[str]) -> List[Tuple[str, str]]:
    """Running containers that mount any of ``volumes`` -> [(id, name)]."""
    want = set(volumes)
    out = []
    for c in await ops.list_containers(False):
        info = await ops.inspect_container(c.id)
        if want & set(_volume_mounts(info or {})):
            out.append((c.id, _container_name(c)))
    return out


def summarize_backup(parsed: restic.ParsedRun, volumes: List[str], exit_code: int) -> Dict[str, Any]:
    """Turn the parsed helper log into status/stats/snapshots/error."""
    status = STATUS_SUCCESS
    errs: List[str] = []
    snapshots: List[Dict[str, str]] = []
    per: Dict[str, Any] = {}
    totals = {"filesNew": 0, "filesChanged": 0, "filesTotal": 0, "bytesAdded": 0, "bytesTotal": 0}
    for sect in ("open", "init"):
        s = parsed.section(sect)
        if s is not None and s.rc not in (None, 0):
            detail = " ".join(l for l in s.lines if l.strip())[-500:]
            return {"status": STATUS_FAILED, "error": f"repository {sect} failed (exit {s.rc}): {detail}".strip(),
                    "snapshots": [], "stats": {}, "bytesAdded": 0}
    for v in volumes:
        s = parsed.section("backup", v)
        if s is None or s.rc is None:
            status = STATUS_FAILED
            errs.append(f"{v}: backup did not run (helper exit {exit_code})")
            continue
        summary, verrs = restic.backup_summary(s.lines)
        if s.rc == 0 or (s.rc == 3 and summary):
            if summary:
                st = restic.stats_from_summary(summary)
                per[v] = st
                for k in totals:
                    totals[k] += st.get(k, 0)
                if st.get("snapshotId"):
                    snapshots.append({"volume": v, "id": st["snapshotId"]})
            if s.rc == 3:
                status = STATUS_WARNING if status != STATUS_FAILED else status
                errs.append(f"{v}: some files could not be read: " + "; ".join(verrs[:5]))
        else:
            status = STATUS_FAILED
            detail = "; ".join(verrs[:5]) or " ".join(l for l in s.lines if l.strip() and not l.startswith("{"))[-300:]
            errs.append(f"{v}: restic backup exit {s.rc}: {detail}")
    f = parsed.section("forget")
    if f is not None and f.rc not in (None, 0):
        if status == STATUS_SUCCESS:
            status = STATUS_WARNING
        errs.append(f"retention (forget --prune) failed with exit {f.rc}")
    return {
        "status": status,
        "error": "\n".join(errs),
        "snapshots": snapshots,
        "stats": {**totals, "volumes": per},
        "bytesAdded": totals["bytesAdded"],
    }


# ---------------------------------------------------------------------------
# service
# ---------------------------------------------------------------------------


class VolumeBackupService:
    def __init__(self, svc: "Services", *, clock=_now, helper=run_helper) -> None:
        self.svc = svc
        self.store = svc.store
        self._clock = clock
        self._helper = helper
        self._locks: Dict[str, asyncio.Lock] = {}
        self._tasks: Dict[str, asyncio.Task] = {}
        self._snap_cache: Dict[Tuple[str, str, str], Tuple[float, List[Dict[str, Any]]]] = {}
        self.runner = CronRunner("volbackups", self._load_entries, self._fire, self._on_skip, clock=clock)
        self._housekeeping: Optional[asyncio.Task] = None

    # -- lifecycle --------------------------------------------------------
    async def start(self) -> None:
        try:
            n = self.store.fail_running_backup_runs("interrupted: the server restarted during the run")
            if n:
                log.warning("volbackup: marked %d interrupted run(s) failed", n)
        except Exception:  # noqa: BLE001
            pass
        await self.runner.start()
        self._housekeeping = asyncio.ensure_future(self._housekeep())

    async def stop(self) -> None:
        if self._housekeeping is not None:
            self._housekeeping.cancel()
            try:
                await self._housekeeping
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self.runner.stop()
        for t in list(self._tasks.values()):
            t.cancel()

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)
        await self.runner.drain()

    async def _housekeep(self) -> None:
        while True:
            try:
                self.store.prune_backup_runs(iso(self._clock() - timedelta(days=RUN_RETENTION_DAYS)))
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(24 * 3600)

    def _lock(self, repo_id: str) -> asyncio.Lock:
        lk = self._locks.get(repo_id)
        if lk is None:
            # Drop the idle locks first so the table holds only ids in use: a
            # lock nobody holds or waits on has no one to hand over to.
            for key in [key for key, other in self._locks.items() if not other.locked()]:
                del self._locks[key]
            lk = self._locks[repo_id] = asyncio.Lock()
        return lk

    # -- lookups ----------------------------------------------------------
    def _host(self, host_id: str):
        try:
            return self.store.get_host(host_id)
        except Exception:  # noqa: BLE001
            raise errors.NotFound(f"host {host_id!r} not found")

    def _ops(self, host_id: str):
        return self.svc.docker.ops(self._host(host_id))

    def _repo(self, repo_id: str) -> Dict[str, Any]:
        try:
            return self.store.get_backup_repo(repo_id)
        except NotFoundError:
            raise errors.NotFound("repository not found")

    def _runtime(self, repo: Dict[str, Any], host_id: str) -> Tuple[restic.RepoRuntime, Dict[str, str]]:
        if repo["type"] == "local" and repo.get("hostId") and repo["hostId"] != host_id:
            raise errors.Validation(
                f"repository {repo['name']!r} is a local path on host {repo['hostId']!r}; it cannot be used from host {host_id!r}"
            )
        secrets = self.store.repo_secrets(repo["id"])
        try:
            return restic.repo_runtime(repo, secrets), secrets
        except restic.RepoConfigError as err:
            raise errors.Validation(str(err))

    def repo_host(self, repo: Dict[str, Any], host_id: str = "") -> str:
        """Which host runs a repository-only operation (list/browse/test)."""
        if repo["type"] == "local":
            return repo["hostId"]
        return host_id or "local"

    def check_job_refs(self, job: Dict[str, Any]) -> None:
        self._host(job["hostId"])
        repo = self._repo(job["repoId"])
        if repo["type"] == "local" and repo["hostId"] != job["hostId"]:
            raise errors.Validation("a local-path repository can only be used by jobs on its own host")

    def check_repo_refs(self, repo: Dict[str, Any], secrets: Dict[str, str], existing_id: str = "") -> None:
        merged = dict(self.store.repo_secrets(existing_id)) if existing_id else {}
        for k, v in secrets.items():
            if v not in ("", MASK_VALUE):
                merged[k] = v
        if repo["type"] == "local":
            self._host(repo["hostId"])
        try:
            restic.repo_runtime(repo, merged)
        except restic.RepoConfigError as err:
            raise errors.Validation(str(err))

    # -- jobs view --------------------------------------------------------
    def job_view(self, job: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(job)
        due = None
        if job.get("enabled") and job.get("schedule"):
            due = self.runner.next_due(job["id"])
            if due is None:
                try:
                    due = next_run(job["schedule"], self._clock(), job.get("timezone", ""))
                except (CronParseError, ValueError):
                    due = None
        out["nextRun"] = iso(due)
        out["lastRun"] = self.store.last_backup_run(job["id"])
        out["running"] = any(
            k.startswith(job["id"] + ":") and not t.done() for k, t in self._tasks.items()
        )
        return out

    def reload(self) -> None:
        self.runner.reload()

    # -- scheduling glue ----------------------------------------------------
    def _load_entries(self) -> List[CronEntry]:
        out = []
        for j in self.store.list_backup_jobs():
            if not j["enabled"] or not j["schedule"]:
                continue
            out.append(
                CronEntry(
                    key=j["id"],
                    spec=j["schedule"],
                    tz=j["timezone"],
                    last_run=parse_iso(j.get("lastRunAt", "")),
                    anchor=parse_iso(j.get("updatedAt") or j.get("createdAt", "")),
                    payload=j["id"],
                )
            )
        return out

    async def _fire(self, entry: CronEntry, due: datetime) -> None:
        try:
            job = self.store.get_backup_job(entry.payload)
        except NotFoundError:
            return
        self.store.set_backup_job_fields(job["id"], last_run_at=iso(due))
        await self.run_backup(job, trigger="schedule")

    def _on_skip(self, entry: CronEntry, due: datetime, reason: str) -> None:
        try:
            job = self.store.get_backup_job(entry.payload)
        except NotFoundError:
            return
        self.store.set_backup_job_fields(job["id"], last_run_at=iso(due))
        now = iso(self._clock())
        self.store.save_backup_run(
            {
                "jobId": job["id"], "jobName": job["name"], "repoId": job["repoId"], "hostId": job["hostId"],
                "kind": "backup", "trigger": "schedule", "status": STATUS_SKIPPED, "error": reason,
                "startedAt": now, "finishedAt": now,
            }
        )

    # -- backup -------------------------------------------------------------
    def start_backup(self, job: Dict[str, Any], *, trigger: str = "manual") -> Dict[str, Any]:
        """Kick a backup off in the background; return the 'running' run."""
        for k, t in self._tasks.items():
            if k.startswith(job["id"] + ":") and not t.done():
                raise errors.Conflict("a backup of this job is already running")
        run = self._new_run(job, trigger)
        key = f"{job['id']}:{run['id']}"
        task = asyncio.ensure_future(self.run_backup(job, trigger=trigger, run=run))
        self._tasks[key] = task
        task.add_done_callback(lambda _t, k=key: self._tasks.pop(k, None))
        return run

    def _new_run(self, job: Dict[str, Any], trigger: str, kind: str = "backup") -> Dict[str, Any]:
        return self.store.save_backup_run(
            {
                "jobId": job.get("id", ""), "jobName": job.get("name", ""), "repoId": job.get("repoId", ""),
                "hostId": job.get("hostId", ""), "kind": kind, "trigger": trigger, "status": STATUS_RUNNING,
                "volumes": job.get("_volumes", []), "startedAt": iso(self._clock()),
            }
        )

    async def _deployment_project(self, job: Dict[str, Any]) -> str:
        sel = job.get("selector") or {}
        if sel.get("mode") != "deployment":
            return ""
        try:
            dep = self.store.get_deployment(sel.get("deploymentId", ""))
        except Exception:  # noqa: BLE001
            return ""
        return dep.resource_name() if dep.type == "compose" else ""

    async def preview_volumes(self, job: Dict[str, Any]) -> List[str]:
        ops = self._ops(job["hostId"])
        return await resolve_volumes(ops, job.get("selector") or {}, deployment_project=await self._deployment_project(job))

    async def run_backup(self, job: Dict[str, Any], *, trigger: str = "manual", run: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Run one backup of ``job`` to completion and return the saved run."""
        run = run or self._new_run(job, trigger)
        t0 = time.monotonic()
        logs: List[str] = []
        secrets_seen: Dict[str, str] = {}
        result: Dict[str, Any] = {"status": STATUS_FAILED, "error": "", "snapshots": [], "stats": {}, "bytesAdded": 0}
        volumes: List[str] = list(job.get("_volumes") or [])
        try:
            ops = self._ops(job["hostId"])
            repo = self._repo(job["repoId"])
            rt, secrets_seen = self._runtime(repo, job["hostId"])
            if not volumes:
                volumes = await resolve_volumes(ops, job.get("selector") or {}, deployment_project=await self._deployment_project(job))
            if not volumes:
                raise errors.Validation("the selector matched no volumes on this host")
            run["volumes"] = volumes
            self.store.save_backup_run(run)
            logs.append(f"volumes: {', '.join(volumes)}")
            hooks = job.get("hooks") or {}
            async with self._lock(repo["id"]):
                pre_ok = True
                for h in hooks.get("pre") or []:
                    ok, out = await self._exec_hook(ops, h)
                    logs.append(f"[pre-hook {h['container']}] {'ok' if ok else 'FAILED'}\n{out}".rstrip())
                    if not ok:
                        pre_ok = False
                        result["error"] = f"pre hook in {h['container']} failed"
                        break
                stopped: List[Tuple[str, str]] = []
                if pre_ok:
                    try:
                        if hooks.get("stopContainers"):
                            for cid, name in await containers_using(ops, volumes):
                                await ops.stop_container(cid)
                                stopped.append((cid, name))
                                logs.append(f"[stop] {name}")
                        script = restic.backup_script(
                            volumes, job_id=job.get("id") or "adhoc", host_label=job["hostId"],
                            tags=job.get("tags") or [], retention=job.get("retention"),
                        )
                        binds = [(v, f"{restic.DATA_ROOT}/{v}", True) for v in volumes] + rt.binds
                        hr: HelperResult = await self._helper(
                            ops, script, env=rt.env, binds=binds, network=rt.network,
                            timeout=BACKUP_TIMEOUT, label=run["id"],
                        )
                        logs.append(restic.clean_log(hr.lines))
                        parsed = restic.parse_markers(hr.lines)
                        if parsed.initialized or (parsed.section("open") and parsed.section("open").rc in (None, 0)):
                            if not repo.get("initialized"):
                                self.store.set_backup_repo_initialized(repo["id"], True)
                        result = summarize_backup(parsed, volumes, hr.exit_code)
                        if hr.timed_out:
                            result["status"] = STATUS_FAILED
                            result["error"] = (result.get("error", "") + "\nbackup timed out").strip()
                    finally:
                        for cid, name in reversed(stopped):
                            try:
                                await ops.start_container(cid)
                                logs.append(f"[start] {name}")
                            except Exception as err:  # noqa: BLE001
                                logs.append(f"[start] {name} FAILED: {err}")
                                result["status"] = STATUS_FAILED
                                result["error"] = (result.get("error", "") + f"\ncould not restart {name}: {err}").strip()
                for h in hooks.get("post") or []:
                    ok, out = await self._exec_hook(ops, h)
                    logs.append(f"[post-hook {h['container']}] {'ok' if ok else 'FAILED'}\n{out}".rstrip())
                    if not ok and result["status"] == STATUS_SUCCESS:
                        result["status"] = STATUS_WARNING
                        result["error"] = (result.get("error", "") + f"\npost hook in {h['container']} failed").strip()
        except errors.AppError as err:
            result["error"] = err.message or str(err)
        except asyncio.CancelledError:
            result["error"] = "cancelled"
            raise
        except Exception as err:  # noqa: BLE001
            log.exception("volbackup: job %s failed", job.get("id"))
            result["error"] = str(err) or err.__class__.__name__
        finally:
            run.update(
                {
                    "status": result["status"],
                    "error": restic.redact(result.get("error", ""), secrets_seen.values()),
                    "snapshots": result.get("snapshots", []),
                    "stats": result.get("stats", {}),
                    "bytesAdded": result.get("bytesAdded", 0),
                    "durationSec": round(time.monotonic() - t0, 2),
                    "volumes": volumes,
                    "log": restic.redact("\n".join(l for l in logs if l), secrets_seen.values()),
                    "finishedAt": iso(self._clock()),
                }
            )
            self.store.save_backup_run(run)
            self._invalidate_snapshots(job.get("repoId", ""))
            if job.get("id"):
                self._after_run(job, run)
        return run

    async def _exec_hook(self, ops, hook: Dict[str, str]) -> Tuple[bool, str]:
        try:
            containers = await ops.list_containers(False)
            match = [c for c in containers if _container_name(c) == hook["container"]]
            if not match:
                return False, f"container {hook['container']!r} is not running"
            out = await asyncio.wait_for(ops.exec(match[0].id, hook_command(hook["command"])), timeout=3600)
        except Exception as err:  # noqa: BLE001
            return False, str(err)
        return parse_hook_output(out)

    def _after_run(self, job: Dict[str, Any], run: Dict[str, Any]) -> None:
        """Raise an alert on failure; resolve the job's open alert on success."""
        try:
            current = self.store.get_backup_job(job["id"])
        except NotFoundError:
            return
        if run["status"] == STATUS_FAILED:
            alert_id = self._raise_alert(current, run)
            if alert_id:
                self.store.set_backup_job_fields(current["id"], alert_id=alert_id)
        elif run["status"] in (STATUS_SUCCESS, STATUS_WARNING) and current.get("alertId"):
            try:
                self.store.resolve_alert(current["alertId"])
            except Exception:  # noqa: BLE001
                pass
            self.store.set_backup_job_fields(current["id"], alert_id="")

    def _raise_alert(self, job: Dict[str, Any], run: Dict[str, Any]) -> str:
        from kowan.events import Event
        from kowan.models import Alert

        a = Alert(
            id=str(uuid.uuid4()),
            ruleId="",
            hostId=job["hostId"],
            level="critical",
            title=f"Volume backup failed: {job['name']}",
            message=(run.get("error") or "backup failed")[:1000],
            resource=f"volbackup:{job['name']}",
            channelId="",
            resolved=False,
            createdAt=iso(self._clock()),
        )
        try:
            self.store.create_alert(a)
        except Exception as err:  # noqa: BLE001
            log.error("volbackup: create alert: %s", err)
            return ""
        bus = getattr(self.svc, "bus", None)
        if bus is not None:
            bus.publish(
                Event(
                    type="alert",
                    source="volbackup",
                    payload={
                        "id": a.id, "ruleId": "", "hostId": a.host_id, "level": a.level, "title": a.title,
                        "message": a.message, "resource": a.resource, "channelId": "", "resolved": False,
                        "createdAt": a.created_at,
                    },
                )
            )
        return a.id

    # -- ad-hoc backup of one volume -------------------------------------------
    def start_volume_backup(self, host_id: str, volume: str, repo_id: str, tags: Optional[List[str]] = None) -> Dict[str, Any]:
        if not restic.valid_volume_name(volume):
            raise errors.Validation("invalid volume name")
        repo = self._repo(repo_id)
        self._host(host_id)
        if repo["type"] == "local" and repo["hostId"] != host_id:
            raise errors.Validation("a local-path repository can only back up volumes of its own host")
        job = {
            "id": "", "name": f"{volume} (manual)", "hostId": host_id, "repoId": repo_id,
            "selector": {"mode": "names", "volumes": [volume]}, "_volumes": [volume],
            "tags": [t for t in (tags or []) if restic.valid_tag(t)], "retention": {}, "hooks": {},
        }
        run = self._new_run(job, "manual")
        key = f"adhoc:{run['id']}"
        task = asyncio.ensure_future(self.run_backup(job, trigger="manual", run=run))
        self._tasks[key] = task
        task.add_done_callback(lambda _t, k=key: self._tasks.pop(k, None))
        return run

    # -- snapshots / browse ------------------------------------------------------
    def _invalidate_snapshots(self, repo_id: str) -> None:
        for k in list(self._snap_cache):
            if k[0] == repo_id:
                self._snap_cache.pop(k, None)

    async def list_snapshots(self, repo_id: str, host_id: str = "", volume: str = "", *, fresh: bool = False) -> List[Dict[str, Any]]:
        repo = self._repo(repo_id)
        h = self.repo_host(repo, host_id)
        key = (repo_id, h, volume)
        hit = self._snap_cache.get(key)
        if hit and not fresh and time.monotonic() - hit[0] < SNAPSHOT_CACHE_TTL:
            return hit[1]
        ops = self._ops(h)
        rt, secrets = self._runtime(repo, h)
        hr = await self._helper(ops, restic.snapshots_script(volume), env=rt.env, binds=rt.binds,
                                network=rt.network, timeout=LIST_TIMEOUT, label="snapshots")
        parsed = restic.parse_markers(hr.lines)
        rc = None
        for s in parsed.sections:
            if s.rc is not None:
                rc = s.rc
        if rc == 10:
            snaps: List[Dict[str, Any]] = []  # repository not initialised yet
        elif rc != 0:
            detail = " ".join(l for s in parsed.sections for l in s.lines if l.strip())[-400:]
            raise errors.BadGateway(restic.redact(f"restic snapshots failed (exit {rc}): {detail}", secrets.values()))
        else:
            snaps = restic.parse_snapshots(l for s in parsed.sections for l in s.lines)
        for s in snaps:
            s["repoId"] = repo_id
        self._snap_cache[key] = (time.monotonic(), snaps)
        return snaps

    async def volume_snapshots(self, host_id: str, volume: str) -> Dict[str, Any]:
        """Snapshots of one volume across the repositories its host's jobs use
        (plus any repo with ad-hoc runs of it), filtered to this host."""
        repo_ids = []
        for j in self.store.list_backup_jobs():
            if j["hostId"] == host_id and j["repoId"] not in repo_ids:
                repo_ids.append(j["repoId"])
        for r in self.store.list_backup_runs(volume=volume, host_id=host_id, limit=200):
            if r["repoId"] and r["repoId"] not in repo_ids:
                repo_ids.append(r["repoId"])
        out: List[Dict[str, Any]] = []
        errs: List[Dict[str, str]] = []

        async def one(rid: str) -> None:
            try:
                snaps = await self.list_snapshots(rid, host_id, volume)
                out.extend(s for s in snaps if s.get("hostname") in ("", host_id))
            except NotFoundError:
                return
            except errors.AppError as err:
                errs.append({"repoId": rid, "error": err.message})
            except Exception as err:  # noqa: BLE001
                errs.append({"repoId": rid, "error": str(err)})

        await asyncio.gather(*(one(r) for r in repo_ids))
        out.sort(key=lambda s: s["time"], reverse=True)
        return {"snapshots": out, "errors": errs, "repoIds": repo_ids}

    async def browse(self, repo_id: str, snapshot_id: str, path: str, host_id: str = "") -> Dict[str, Any]:
        repo = self._repo(repo_id)
        h = self.repo_host(repo, host_id)
        ops = self._ops(h)
        rt, secrets = self._runtime(repo, h)
        clean = "/" + "/".join(p for p in (path or "").split("/") if p and p not in (".", ".."))
        try:
            script = restic.ls_script(snapshot_id, clean)
        except ValueError as err:
            raise errors.Validation(str(err))
        hr = await self._helper(ops, script, env=rt.env, binds=rt.binds, network=rt.network,
                                timeout=LIST_TIMEOUT, label="ls")
        parsed = restic.parse_markers(hr.lines)
        rc = next((s.rc for s in parsed.sections if s.rc is not None), None)
        lines = [l for s in parsed.sections for l in s.lines]
        if rc != 0:
            raise errors.BadGateway(restic.redact(f"restic ls failed (exit {rc}): {' '.join(lines)[-400:]}", secrets.values()))
        entries, truncated = restic.parse_ls(lines, clean)
        return {"path": clean, "entries": entries, "truncated": truncated}

    async def test_repo(self, repo_id: str, host_id: str = "") -> Dict[str, Any]:
        repo = self._repo(repo_id)
        h = self.repo_host(repo, host_id)
        ops = self._ops(h)
        rt, secrets = self._runtime(repo, h)
        async with self._lock(repo_id):
            hr = await self._helper(ops, restic.check_script(), env=rt.env, binds=rt.binds,
                                    network=rt.network, timeout=LIST_TIMEOUT, label="check")
        parsed = restic.parse_markers(hr.lines)
        ok = parsed.done and hr.exit_code == 0
        if ok:
            self.store.set_backup_repo_initialized(repo_id, True)
        text = restic.redact(restic.clean_log(hr.lines), secrets.values())
        return {"ok": ok, "initialized": parsed.initialized, "log": text[-4000:]}

    # -- restore -----------------------------------------------------------------
    def start_restore(self, *, host_id: str, repo_id: str, snapshot_id: str, target: str,
                      new_volume: bool, stop_containers: bool = True, exact: bool = True) -> Dict[str, Any]:
        if not restic.valid_volume_name(target):
            raise errors.Validation("invalid target volume name")
        if not re.match(r"^[0-9a-f]{8,64}$", snapshot_id or ""):
            raise errors.Validation("invalid snapshot id")
        repo = self._repo(repo_id)
        self._host(host_id)
        if repo["type"] == "local" and repo["hostId"] != host_id:
            raise errors.Validation("a local-path repository can only restore on its own host")
        pseudo = {"id": "", "name": f"restore {snapshot_id[:8]} -> {target}", "hostId": host_id,
                  "repoId": repo_id, "_volumes": [target]}
        run = self._new_run(pseudo, "manual", kind="restore")
        key = f"restore:{run['id']}"
        task = asyncio.ensure_future(
            self.run_restore(run, host_id=host_id, repo=repo, snapshot_id=snapshot_id, target=target,
                             new_volume=new_volume, stop_containers=stop_containers, exact=exact)
        )
        self._tasks[key] = task
        task.add_done_callback(lambda _t, k=key: self._tasks.pop(k, None))
        return run

    async def run_restore(self, run: Dict[str, Any], *, host_id: str, repo: Dict[str, Any], snapshot_id: str,
                          target: str, new_volume: bool, stop_containers: bool, exact: bool) -> Dict[str, Any]:
        t0 = time.monotonic()
        logs: List[str] = []
        status, error, stats = STATUS_FAILED, "", {}
        secrets: Dict[str, str] = {}
        try:
            ops = self._ops(host_id)
            rt, secrets = self._runtime(repo, host_id)
            if new_volume:
                exists = True
                try:
                    await ops.inspect_volume(target)
                except Exception:  # noqa: BLE001
                    exists = False
                if exists:
                    raise errors.Conflict(f"volume {target!r} already exists")
                from kowan.models import VolumeCreateOptions

                await ops.create_volume(VolumeCreateOptions(name=target, labels={"kowan.restored-from": snapshot_id[:16]}))
                logs.append(f"[create] volume {target}")
            else:
                await ops.inspect_volume(target)
            async with self._lock(repo["id"]):
                stopped: List[Tuple[str, str]] = []
                try:
                    if stop_containers and not new_volume:
                        for cid, name in await containers_using(ops, [target]):
                            await ops.stop_container(cid)
                            stopped.append((cid, name))
                            logs.append(f"[stop] {name}")
                    script = restic.restore_script(snapshot_id, delete=exact and not new_volume)
                    hr = await self._helper(ops, script, env=rt.env, binds=[(target, restic.TARGET_MOUNT, False)] + rt.binds,
                                            network=rt.network, timeout=BACKUP_TIMEOUT, label=run["id"])
                    logs.append(restic.clean_log(hr.lines))
                    parsed = restic.parse_markers(hr.lines)
                    sect = parsed.section("restore")
                    if sect is not None and sect.rc == 0:
                        status = STATUS_SUCCESS
                        stats = restic.restore_summary(sect.lines) or {}
                    else:
                        lk = parsed.section("lookup")
                        rc = sect.rc if sect is not None else (lk.rc if lk is not None else hr.exit_code)
                        detail = " ".join(l for s in parsed.sections for l in s.lines if l.strip() and not l.startswith("{"))[-400:]
                        error = f"restic restore failed (exit {rc}): {detail}".strip()
                finally:
                    for cid, name in reversed(stopped):
                        try:
                            await ops.start_container(cid)
                            logs.append(f"[start] {name}")
                        except Exception as err:  # noqa: BLE001
                            logs.append(f"[start] {name} FAILED: {err}")
                            status = STATUS_FAILED
                            error = (error + f"\ncould not restart {name}: {err}").strip()
        except errors.AppError as err:
            error = err.message or str(err)
        except Exception as err:  # noqa: BLE001
            log.exception("volbackup: restore failed")
            error = str(err) or err.__class__.__name__
        run.update(
            {
                "status": status,
                "error": restic.redact(error, secrets.values()),
                "stats": stats,
                "snapshots": [{"volume": target, "id": snapshot_id}],
                "durationSec": round(time.monotonic() - t0, 2),
                "log": restic.redact("\n".join(l for l in logs if l), secrets.values()),
                "finishedAt": iso(self._clock()),
            }
        )
        self.store.save_backup_run(run)
        return run
