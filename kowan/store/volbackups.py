"""Volume backups (restic): repositories, jobs and runs.

Provides :class:`_VolBackupMixin`, folded into :class:`kowan.store.store.Store`.
Records are camelCase dicts (API shape).

Secrets (the restic repository password, S3 secret key, SSH private key, REST
password) are sealed with the store's AES-256-GCM cipher in
``backup_repos.secrets`` — the same cipher and helpers as the unified
credentials table — and are only ever returned masked. :meth:`repo_secrets`
is the single unmasking path, used by the backup executor.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from ._common import MASK_VALUE, NotFoundError, StoreError, bool_to_int

if TYPE_CHECKING:  # pragma: no cover
    from .store import _StoreBase as _Base
else:
    _Base = object

# DDL for backup_repos / backup_jobs / backup_runs lives in store._SCHEMA.

_REPO_COLS = "id, name, type, host_id, config, secrets, initialized, created_at, updated_at"
_JOB_COLS = (
    "id, name, host_id, repo_id, selector, schedule, timezone, retention, hooks, tags, "
    "enabled, alert_id, last_run_at, created_at, updated_at"
)
_RUN_COLS = (
    "id, job_id, job_name, repo_id, host_id, kind, trigger, status, volumes, snapshots, "
    "stats, bytes_added, duration, error, log, started_at, finished_at"
)
# Hard cap on a stored run log.
MAX_LOG_BYTES = 256 * 1024


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _j(raw: Any, default: Any) -> Any:
    try:
        v = json.loads(raw) if raw else default
    except (TypeError, ValueError):
        return default
    return v if isinstance(v, type(default)) else default


def _dump(v: Any) -> str:
    return json.dumps(v, separators=(",", ":"))


class _VolBackupMixin(_Base):
    # -- repositories ------------------------------------------------------
    def _repo_row(self, r, *, with_secrets: bool = False) -> Dict[str, Any]:
        try:
            sec = self._decrypt_data(r["secrets"]) if r["secrets"] else {}
        except StoreError:
            sec = {}
        out = {
            "id": r["id"],
            "name": r["name"],
            "type": r["type"],
            "hostId": r["host_id"],
            "config": _j(r["config"], {}),
            # Masked: only "is it set" leaks.
            "secrets": {k: (MASK_VALUE if v else "") for k, v in sec.items()},
            "initialized": bool(r["initialized"]),
            "createdAt": r["created_at"],
            "updatedAt": r["updated_at"],
        }
        if with_secrets:
            out["_secrets"] = sec
        return out

    def list_backup_repos(self) -> List[Dict[str, Any]]:
        return [self._repo_row(r) for r in self._query(f"SELECT {_REPO_COLS} FROM backup_repos ORDER BY name")]

    def get_backup_repo(self, rid: str) -> Dict[str, Any]:
        r = self._one(f"SELECT {_REPO_COLS} FROM backup_repos WHERE id = ?", (rid,))
        if r is None:
            raise NotFoundError("repository not found")
        return self._repo_row(r)

    def repo_secrets(self, rid: str) -> Dict[str, str]:
        """Decrypted secret map (executor only; never serialize it)."""
        r = self._one("SELECT secrets FROM backup_repos WHERE id = ?", (rid,))
        if r is None:
            raise NotFoundError("repository not found")
        return self._decrypt_data(r["secrets"]) if r["secrets"] else {}

    def save_backup_repo(self, repo: Dict[str, Any], secrets: Dict[str, str]) -> Dict[str, Any]:
        """Insert/update a repository. ``secrets`` values that are blank or the
        mask keep the stored value (edit forms never resend secrets)."""
        rid = repo.get("id") or str(uuid.uuid4())
        now = _now()
        existing = self._one("SELECT created_at, secrets, initialized FROM backup_repos WHERE id = ?", (rid,))
        stored: Dict[str, str] = {}
        if existing is not None and existing["secrets"]:
            stored = self._decrypt_data(existing["secrets"])
        merged = dict(stored)
        for k, v in (secrets or {}).items():
            if v in ("", MASK_VALUE, None):
                continue
            merged[k] = str(v)
        initialized = repo.get("initialized")
        if initialized is None:
            initialized = bool(existing["initialized"]) if existing is not None else False
        vals = (
            repo["name"],
            repo["type"],
            repo.get("hostId", "") or "",
            _dump(repo.get("config") or {}),
            self._encrypt_data(merged),
            bool_to_int(bool(initialized)),
            now,
        )
        try:
            # Plain INSERT / UPDATE, never INSERT OR REPLACE: on a UNIQUE(name)
            # clash REPLACE would silently delete the *other* repository.
            if existing is None:
                self._execute(
                    "INSERT INTO backup_repos (name, type, host_id, config, secrets, initialized, updated_at, id, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    vals + (rid, now),
                )
            else:
                self._execute(
                    "UPDATE backup_repos SET name = ?, type = ?, host_id = ?, config = ?, secrets = ?, initialized = ?,"
                    " updated_at = ? WHERE id = ?",
                    vals + (rid,),
                )
        except StoreError as err:
            if "UNIQUE" in str(err):
                raise StoreError("a repository with this name already exists") from err
            raise
        return self.get_backup_repo(rid)

    def set_backup_repo_initialized(self, rid: str, value: bool = True) -> None:
        self._execute("UPDATE backup_repos SET initialized = ? WHERE id = ?", (bool_to_int(value), rid))

    def delete_backup_repo(self, rid: str) -> None:
        cur = self._execute("DELETE FROM backup_repos WHERE id = ?", (rid,))
        if cur.rowcount == 0:
            raise NotFoundError("repository not found")

    # -- jobs ---------------------------------------------------------------
    @staticmethod
    def _job_row(r) -> Dict[str, Any]:
        return {
            "id": r["id"],
            "name": r["name"],
            "hostId": r["host_id"],
            "repoId": r["repo_id"],
            "selector": _j(r["selector"], {}),
            "schedule": r["schedule"],
            "timezone": r["timezone"],
            "retention": _j(r["retention"], {}),
            "hooks": _j(r["hooks"], {}),
            "tags": _j(r["tags"], []),
            "enabled": bool(r["enabled"]),
            "alertId": r["alert_id"],
            "lastRunAt": r["last_run_at"],
            "createdAt": r["created_at"],
            "updatedAt": r["updated_at"],
        }

    def list_backup_jobs(self) -> List[Dict[str, Any]]:
        return [self._job_row(r) for r in self._query(f"SELECT {_JOB_COLS} FROM backup_jobs ORDER BY name")]

    def get_backup_job(self, jid: str) -> Dict[str, Any]:
        r = self._one(f"SELECT {_JOB_COLS} FROM backup_jobs WHERE id = ?", (jid,))
        if r is None:
            raise NotFoundError("backup job not found")
        return self._job_row(r)

    def save_backup_job(self, job: Dict[str, Any]) -> Dict[str, Any]:
        jid = job.get("id") or str(uuid.uuid4())
        now = _now()
        existing = self._one("SELECT created_at, alert_id, last_run_at FROM backup_jobs WHERE id = ?", (jid,))
        self._execute(
            "INSERT OR REPLACE INTO backup_jobs (" + _JOB_COLS + ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                jid,
                job["name"],
                job["hostId"],
                job["repoId"],
                _dump(job.get("selector") or {}),
                job.get("schedule", "") or "",
                job.get("timezone", "") or "",
                _dump(job.get("retention") or {}),
                _dump(job.get("hooks") or {}),
                _dump(list(job.get("tags") or [])),
                bool_to_int(bool(job.get("enabled", True))),
                job.get("alertId", existing["alert_id"] if existing is not None else "") or "",
                job.get("lastRunAt", existing["last_run_at"] if existing is not None else "") or "",
                existing["created_at"] if existing is not None else now,
                now,
            ),
        )
        return self.get_backup_job(jid)

    def set_backup_job_fields(self, jid: str, *, last_run_at: Optional[str] = None, alert_id: Optional[str] = None) -> None:
        if last_run_at is not None:
            self._execute("UPDATE backup_jobs SET last_run_at = ? WHERE id = ?", (last_run_at, jid))
        if alert_id is not None:
            self._execute("UPDATE backup_jobs SET alert_id = ? WHERE id = ?", (alert_id, jid))

    def delete_backup_job(self, jid: str) -> None:
        cur = self._execute("DELETE FROM backup_jobs WHERE id = ?", (jid,))
        if cur.rowcount == 0:
            raise NotFoundError("backup job not found")

    def jobs_using_repo(self, rid: str) -> List[str]:
        return [r["name"] for r in self._query("SELECT name FROM backup_jobs WHERE repo_id = ?", (rid,))]

    # -- runs ---------------------------------------------------------------
    @staticmethod
    def _run_row(r, *, with_log: bool = True) -> Dict[str, Any]:
        out = {
            "id": r["id"],
            "jobId": r["job_id"],
            "jobName": r["job_name"],
            "repoId": r["repo_id"],
            "hostId": r["host_id"],
            "kind": r["kind"],
            "trigger": r["trigger"],
            "status": r["status"],
            "volumes": _j(r["volumes"], []),
            "snapshots": _j(r["snapshots"], []),
            "stats": _j(r["stats"], {}),
            "bytesAdded": r["bytes_added"],
            "durationSec": r["duration"],
            "error": r["error"],
            "startedAt": r["started_at"],
            "finishedAt": r["finished_at"],
        }
        if with_log:
            out["log"] = r["log"]
        return out

    def save_backup_run(self, run: Dict[str, Any]) -> Dict[str, Any]:
        rid = run.get("id") or str(uuid.uuid4())
        log_text = run.get("log", "") or ""
        if len(log_text.encode("utf-8", "replace")) > MAX_LOG_BYTES:
            log_text = "[log truncated]\n" + log_text[-MAX_LOG_BYTES:]
        self._execute(
            "INSERT OR REPLACE INTO backup_runs (" + _RUN_COLS + ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                rid,
                run.get("jobId", ""),
                run.get("jobName", ""),
                run.get("repoId", ""),
                run.get("hostId", ""),
                run.get("kind", "backup"),
                run.get("trigger", ""),
                run.get("status", ""),
                _dump(list(run.get("volumes") or [])),
                _dump(list(run.get("snapshots") or [])),
                _dump(run.get("stats") or {}),
                int(run.get("bytesAdded", 0) or 0),
                float(run.get("durationSec", 0) or 0),
                (run.get("error", "") or "")[:4000],
                log_text,
                run.get("startedAt") or _now(),
                run.get("finishedAt", ""),
            ),
        )
        return {**run, "id": rid}

    def get_backup_run(self, rid: str) -> Dict[str, Any]:
        r = self._one(f"SELECT {_RUN_COLS} FROM backup_runs WHERE id = ?", (rid,))
        if r is None:
            raise NotFoundError("backup run not found")
        return self._run_row(r)

    def list_backup_runs(self, *, job_id: str = "", volume: str = "", host_id: str = "", limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit or 100), 1000))
        where, params = [], []
        if job_id:
            where.append("job_id = ?")
            params.append(job_id)
        if host_id:
            where.append("host_id = ?")
            params.append(host_id)
        if volume:
            # volumes is a JSON array of names; match the quoted element.
            where.append("volumes LIKE ?")
            params.append('%"' + volume + '"%')
        sql = f"SELECT {_RUN_COLS} FROM backup_runs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)
        rows = self._query(sql, params)
        out = [self._run_row(r, with_log=False) for r in rows]
        if volume:
            out = [r for r in out if volume in r["volumes"]]
        return out

    def last_backup_run(self, job_id: str) -> Optional[Dict[str, Any]]:
        r = self._one(
            f"SELECT {_RUN_COLS} FROM backup_runs WHERE job_id = ? AND kind = 'backup' ORDER BY started_at DESC LIMIT 1",
            (job_id,),
        )
        return self._run_row(r, with_log=False) if r else None

    def fail_running_backup_runs(self, message: str) -> int:
        """Mark runs left 'running' by a crash/restart as failed (startup)."""
        cur = self._execute(
            "UPDATE backup_runs SET status = 'failed', error = ?, finished_at = ? WHERE status = 'running'",
            (message, _now()),
        )
        return cur.rowcount or 0

    def prune_backup_runs(self, before: str) -> int:
        cur = self._execute("DELETE FROM backup_runs WHERE started_at < ?", (before,))
        return cur.rowcount or 0
