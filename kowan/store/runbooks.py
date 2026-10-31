"""Runbooks: ``runbooks`` + ``runbook_runs`` tables.

Provides :class:`_RunbookMixin`, folded into :class:`kowan.store.store.Store`.
Records are plain dicts in the API (camelCase) shape. A runbook's parameters
and steps are JSON lists (see :mod:`kowan.runbooks.model`); a run keeps the
outcome and the output of every step it ran. Runbooks declared with container
labels (``kowan.runbook.*``) are not stored: they are read from the containers,
and only their runs land here.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from ._common import NotFoundError, bool_to_int

if TYPE_CHECKING:  # pragma: no cover
    from .store import _StoreBase as _Base
else:
    _Base = object

RUNBOOKS_SCHEMA = """
CREATE TABLE IF NOT EXISTS runbooks (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    params      TEXT NOT NULL DEFAULT '[]',
    steps       TEXT NOT NULL DEFAULT '[]',
    confirm     INTEGER NOT NULL DEFAULT 0,
    created_by  TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runbook_runs (
    id           TEXT PRIMARY KEY,
    runbook_id   TEXT NOT NULL,
    runbook_name TEXT NOT NULL,
    workloads    TEXT NOT NULL DEFAULT '[]',
    params       TEXT NOT NULL DEFAULT '{}',
    user         TEXT NOT NULL DEFAULT '',
    trigger      TEXT NOT NULL DEFAULT 'manual',
    status       TEXT NOT NULL,
    steps        TEXT NOT NULL DEFAULT '[]',
    started_at   TEXT NOT NULL,
    finished_at  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_runbook_runs_runbook ON runbook_runs (runbook_id, started_at);
"""

# Runs kept per runbook; older ones are dropped as new ones finish.
KEEP_RUNS = 100

_RB_COLS = "id, name, description, params, steps, confirm, created_by, created_at, updated_at"
_RUN_COLS = "id, runbook_id, runbook_name, workloads, params, user, trigger, status, steps, started_at, finished_at"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _loads(raw: Any, default: Any) -> Any:
    try:
        v = json.loads(raw) if raw else default
    except (TypeError, ValueError):
        return default
    return v if isinstance(v, type(default)) else default


def _dumps(v: Any) -> str:
    return json.dumps(v, separators=(",", ":"))


def _rb_row(r) -> Dict[str, Any]:
    return {
        "id": r["id"], "name": r["name"], "description": r["description"],
        "params": _loads(r["params"], []), "steps": _loads(r["steps"], []),
        "confirm": bool(r["confirm"]), "createdBy": r["created_by"],
        "createdAt": r["created_at"], "updatedAt": r["updated_at"], "source": "kowan",
    }


def _run_row(r) -> Dict[str, Any]:
    return {
        "id": r["id"], "runbookId": r["runbook_id"], "runbookName": r["runbook_name"],
        "workloads": _loads(r["workloads"], []), "params": _loads(r["params"], {}),
        "user": r["user"], "trigger": r["trigger"], "status": r["status"],
        "steps": _loads(r["steps"], []), "startedAt": r["started_at"], "finishedAt": r["finished_at"],
    }


class _RunbookMixin(_Base):
    # -- runbooks ------------------------------------------------------------
    def list_runbooks(self) -> List[Dict[str, Any]]:
        rows = self._query(f"SELECT {_RB_COLS} FROM runbooks ORDER BY name COLLATE NOCASE")
        return [_rb_row(r) for r in rows]

    def get_runbook(self, id: str) -> Dict[str, Any]:
        r = self._one(f"SELECT {_RB_COLS} FROM runbooks WHERE id = ?", (id,))
        if r is None:
            raise NotFoundError("runbook not found")
        return _rb_row(r)

    def save_runbook(self, rb: Dict[str, Any]) -> Dict[str, Any]:
        """Insert or replace a runbook; a new one gets an id, an existing one
        keeps its creation time and author."""
        now = _now()
        rid = rb.get("id") or str(uuid.uuid4())
        old = self._one("SELECT created_at, created_by FROM runbooks WHERE id = ?", (rid,))
        created = old["created_at"] if old else now
        author = old["created_by"] if old else str(rb.get("createdBy") or "")
        self._execute(
            f"INSERT OR REPLACE INTO runbooks ({_RB_COLS}) VALUES (?,?,?,?,?,?,?,?,?)",
            (rid, rb["name"], rb.get("description", ""), _dumps(rb.get("params") or []),
             _dumps(rb.get("steps") or []), bool_to_int(bool(rb.get("confirm"))), author, created, now),
        )
        return self.get_runbook(rid)

    def delete_runbook(self, id: str) -> None:
        self.get_runbook(id)
        self._execute("DELETE FROM runbooks WHERE id = ?", (id,))
        self._execute("DELETE FROM runbook_runs WHERE runbook_id = ?", (id,))

    # -- runs ------------------------------------------------------------------
    def save_runbook_run(self, run: Dict[str, Any]) -> None:
        """Insert or update a run (a running one is saved again when it ends)."""
        self._execute(
            f"INSERT OR REPLACE INTO runbook_runs ({_RUN_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (run["id"], run["runbookId"], run["runbookName"], _dumps(run.get("workloads") or []),
             _dumps(run.get("params") or {}), run.get("user", ""), run.get("trigger", "manual"),
             run["status"], _dumps(run.get("steps") or []), run["startedAt"], run.get("finishedAt", "")),
        )
        if run.get("finishedAt"):
            self._execute(
                "DELETE FROM runbook_runs WHERE runbook_id = ? AND id NOT IN "
                "(SELECT id FROM runbook_runs WHERE runbook_id = ? ORDER BY started_at DESC LIMIT ?)",
                (run["runbookId"], run["runbookId"], KEEP_RUNS),
            )

    def get_runbook_run(self, id: str) -> Dict[str, Any]:
        r = self._one(f"SELECT {_RUN_COLS} FROM runbook_runs WHERE id = ?", (id,))
        if r is None:
            raise NotFoundError("runbook run not found")
        return _run_row(r)

    def list_runbook_runs(self, runbook_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        if runbook_id:
            rows = self._query(f"SELECT {_RUN_COLS} FROM runbook_runs WHERE runbook_id = ? "
                               "ORDER BY started_at DESC LIMIT ?", (runbook_id, limit))
        else:
            rows = self._query(f"SELECT {_RUN_COLS} FROM runbook_runs ORDER BY started_at DESC LIMIT ?", (limit,))
        return [_run_row(r) for r in rows]

    def fail_unfinished_runbook_runs(self) -> int:
        """Runs left 'running' by a restart did not finish: say so."""
        cur = self._query("SELECT id FROM runbook_runs WHERE status = 'running'")
        self._execute("UPDATE runbook_runs SET status = 'failed', finished_at = ? WHERE status = 'running'", (_now(),))
        return len(cur)
