"""GitOps persistence: syncs, managed-resource markers and run history.

Provides :class:`_GitOpsMixin` (folded into :class:`kowan.store.store.Store`).
The tables are part of the consolidated ``_SCHEMA`` in :mod:`kowan.store.store`.

Tables
------
``gitops_syncs``
    One row per Sync (a git repo + branch + path to reconcile from). ``plan``
    caches the latest computed plan as JSON so the UI can show it without
    re-fetching; ``status`` is ``in_sync`` / ``drift`` / ``error`` / ``""``.
``gitops_managed``
    The ``managedBy`` marker: which sync owns which Kowan resource, keyed by
    ``(kind, resource_id)`` so a resource is owned by at most one sync. Only
    resources listed here may ever be deleted by a sync.
``gitops_runs``
    History of refreshes/applies: trigger, commit, the plan shown and the
    per-resource result.
"""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from ._common import NotFoundError, bool_to_int

if TYPE_CHECKING:  # pragma: no cover
    from .store import _StoreBase as _Base
else:
    _Base = object



_SYNC_FIELDS = (
    "id", "name", "repo_url", "ref", "path", "credential_id", "interval_sec",
    "auto_apply", "prune", "enabled", "webhook_token", "webhook_secret", "status",
    "last_commit", "last_checked_at", "last_applied_commit", "last_applied_at",
    "last_error", "plan", "created_at", "updated_at",
)
_BOOL_FIELDS = ("auto_apply", "prune", "enabled")
_RUN_FIELDS = (
    "id", "sync_id", "trigger", "action", "status", "commit_sha", "plan", "result",
    "error", "started_at", "finished_at",
)


def _loads(raw: Any) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _row_to_sync(row: sqlite3.Row) -> Dict[str, Any]:
    d = {k: row[k] for k in _SYNC_FIELDS}
    for k in _BOOL_FIELDS:
        d[k] = bool(d[k])
    d["plan"] = _loads(d["plan"])
    return d


def _row_to_run(row: sqlite3.Row) -> Dict[str, Any]:
    d = {k: row[k] for k in _RUN_FIELDS}
    d["plan"] = _loads(d["plan"])
    d["result"] = _loads(d["result"])
    return d


def _sync_params(s: Dict[str, Any]) -> list:
    out = []
    for k in _SYNC_FIELDS:
        v = s.get(k)
        if k in _BOOL_FIELDS:
            v = bool_to_int(bool(v))
        elif k == "plan":
            v = json.dumps(v, separators=(",", ":")) if v else ""
        elif k == "interval_sec":
            v = int(v or 0)
        elif v is None:
            v = ""
        out.append(v)
    return out


class _GitOpsMixin(_Base):
    # -- syncs -----------------------------------------------------------
    def create_gitops_sync(self, s: Dict[str, Any]) -> None:
        cols = ", ".join(_SYNC_FIELDS)
        marks = ", ".join("?" for _ in _SYNC_FIELDS)
        self._execute(f"INSERT INTO gitops_syncs ({cols}) VALUES ({marks})", _sync_params(s))

    def update_gitops_sync(self, s: Dict[str, Any]) -> None:
        sets = ", ".join(f"{k} = ?" for k in _SYNC_FIELDS[1:])
        params = _sync_params(s)
        self._execute(f"UPDATE gitops_syncs SET {sets} WHERE id = ?", params[1:] + [params[0]])

    def get_gitops_sync(self, id: str) -> Dict[str, Any]:
        row = self._one(f"SELECT {', '.join(_SYNC_FIELDS)} FROM gitops_syncs WHERE id = ?", (id,))
        if row is None:
            raise NotFoundError("get gitops sync: no rows in result set")
        return _row_to_sync(row)

    def get_gitops_sync_by_token(self, token: str) -> Dict[str, Any]:
        if not token:
            raise NotFoundError("get gitops sync: no rows in result set")
        row = self._one(
            f"SELECT {', '.join(_SYNC_FIELDS)} FROM gitops_syncs WHERE webhook_token = ?", (token,)
        )
        if row is None:
            raise NotFoundError("get gitops sync: no rows in result set")
        return _row_to_sync(row)

    def list_gitops_syncs(self) -> List[Dict[str, Any]]:
        rows = self._query(f"SELECT {', '.join(_SYNC_FIELDS)} FROM gitops_syncs ORDER BY name")
        return [_row_to_sync(r) for r in rows]

    def delete_gitops_sync(self, id: str) -> None:
        with self._lock:
            self._execute("DELETE FROM gitops_managed WHERE sync_id = ?", (id,))
            self._execute("DELETE FROM gitops_runs WHERE sync_id = ?", (id,))
            self._execute("DELETE FROM gitops_syncs WHERE id = ?", (id,))

    # -- managed markers -----------------------------------------------
    def set_gitops_managed(
        self, sync_id: str, kind: str, resource_id: str, name: str,
        applied_hash: str = "", commit: str = "", updated_at: str = "",
    ) -> None:
        self._execute(
            "INSERT INTO gitops_managed (kind, resource_id, sync_id, name, applied_hash, commit_sha, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(kind, resource_id) DO UPDATE SET "
            "sync_id = excluded.sync_id, name = excluded.name, applied_hash = excluded.applied_hash, "
            "commit_sha = excluded.commit_sha, updated_at = excluded.updated_at",
            (kind, resource_id, sync_id, name, applied_hash, commit, updated_at),
        )

    def delete_gitops_managed(self, kind: str, resource_id: str) -> None:
        self._execute("DELETE FROM gitops_managed WHERE kind = ? AND resource_id = ?", (kind, resource_id))

    def list_gitops_managed(self, sync_id: Optional[str] = None) -> List[Dict[str, Any]]:
        cols = "kind, resource_id, sync_id, name, applied_hash, commit_sha, updated_at"
        if sync_id is None:
            rows = self._query(f"SELECT {cols} FROM gitops_managed ORDER BY kind, name")
        else:
            rows = self._query(
                f"SELECT {cols} FROM gitops_managed WHERE sync_id = ? ORDER BY kind, name", (sync_id,)
            )
        return [{k: r[k] for k in r.keys()} for r in rows]

    # -- runs --------------------------------------------------------------
    def create_gitops_run(self, run: Dict[str, Any]) -> None:
        self._execute(
            f"INSERT INTO gitops_runs ({', '.join(_RUN_FIELDS)}) VALUES ({', '.join('?' for _ in _RUN_FIELDS)})",
            [self._run_value(run, k) for k in _RUN_FIELDS],
        )

    @staticmethod
    def _run_value(run: Dict[str, Any], k: str) -> Any:
        v = run.get(k)
        if k in ("plan", "result"):
            return json.dumps(v, separators=(",", ":")) if v is not None else ""
        return "" if v is None else v

    def get_gitops_run(self, id: str) -> Dict[str, Any]:
        row = self._one(f"SELECT {', '.join(_RUN_FIELDS)} FROM gitops_runs WHERE id = ?", (id,))
        if row is None:
            raise NotFoundError("get gitops run: no rows in result set")
        return _row_to_run(row)

    def list_gitops_runs(self, sync_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self._query(
            f"SELECT {', '.join(_RUN_FIELDS)} FROM gitops_runs WHERE sync_id = ? "
            "ORDER BY started_at DESC, rowid DESC LIMIT ?",
            (sync_id, max(1, min(int(limit or 50), 500))),
        )
        return [_row_to_run(r) for r in rows]

    def prune_gitops_runs(self, sync_id: str, keep: int = 100) -> int:
        cur = self._execute(
            "DELETE FROM gitops_runs WHERE sync_id = ? AND id NOT IN ("
            "SELECT id FROM gitops_runs WHERE sync_id = ? ORDER BY started_at DESC, rowid DESC LIMIT ?)",
            (sync_id, sync_id, keep),
        )
        return cur.rowcount or 0
