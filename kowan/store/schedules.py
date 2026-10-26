"""Scheduled start/stop: ``schedules`` + ``schedule_runs`` tables.

Provides :class:`_ScheduleMixin`, folded into :class:`kowan.store.store.Store`.
Records are plain dicts in the API (camelCase) shape, like the updates mixin.

A schedule targets a container (by name, so it survives a recreate), a Kowan
deployment (by id) or a compose stack (by project name) on one host, and runs
``action`` on the cron ``cron`` in ``timezone``. Label-derived schedules
(``kowan.schedule.*`` container labels) are NOT stored here: they are discovered
live by the service; only their run history lands in ``schedule_runs`` (with a
synthetic ``label:...`` schedule id).
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

# DDL for schedules / schedule_runs lives in store._SCHEMA.

_SCHED_COLS = (
    "id, name, target_type, target_id, host_id, action, cron, timezone, enabled, "
    "only_if, last_run_at, last_status, created_at, updated_at"
)
_RUN_COLS = (
    "id, schedule_id, schedule_name, target_type, target_id, host_id, action, trigger, "
    "status, message, scheduled_for, started_at, finished_at"
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _loads_obj(raw: Any) -> dict:
    try:
        v = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def _sched_row(r) -> Dict[str, Any]:
    return {
        "id": r["id"],
        "name": r["name"],
        "targetType": r["target_type"],
        "targetId": r["target_id"],
        "hostId": r["host_id"],
        "action": r["action"],
        "cron": r["cron"],
        "timezone": r["timezone"],
        "enabled": bool(r["enabled"]),
        "onlyIf": _loads_obj(r["only_if"]),
        "lastRunAt": r["last_run_at"],
        "lastStatus": r["last_status"],
        "createdAt": r["created_at"],
        "updatedAt": r["updated_at"],
        "source": "manual",
    }


def _run_row(r) -> Dict[str, Any]:
    return {
        "id": r["id"],
        "scheduleId": r["schedule_id"],
        "scheduleName": r["schedule_name"],
        "targetType": r["target_type"],
        "targetId": r["target_id"],
        "hostId": r["host_id"],
        "action": r["action"],
        "trigger": r["trigger"],
        "status": r["status"],
        "message": r["message"],
        "scheduledFor": r["scheduled_for"],
        "startedAt": r["started_at"],
        "finishedAt": r["finished_at"],
    }


class _ScheduleMixin(_Base):
    def list_schedules(self) -> List[Dict[str, Any]]:
        rows = self._query(f"SELECT {_SCHED_COLS} FROM schedules ORDER BY created_at")
        return [_sched_row(r) for r in rows]

    def get_schedule(self, sid: str) -> Dict[str, Any]:
        r = self._one(f"SELECT {_SCHED_COLS} FROM schedules WHERE id = ?", (sid,))
        if r is None:
            raise NotFoundError("schedule not found")
        return _sched_row(r)

    def save_schedule(self, s: Dict[str, Any]) -> Dict[str, Any]:
        """Insert or update (by id) a schedule. Missing id -> new uuid."""
        now = _now()
        sid = s.get("id") or str(uuid.uuid4())
        existing = self._one("SELECT created_at, last_run_at, last_status FROM schedules WHERE id = ?", (sid,))
        created = existing["created_at"] if existing else (s.get("createdAt") or now)
        last_run = s.get("lastRunAt", existing["last_run_at"] if existing else "") or ""
        last_status = s.get("lastStatus", existing["last_status"] if existing else "") or ""
        self._execute(
            "INSERT OR REPLACE INTO schedules (" + _SCHED_COLS + ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                sid,
                s.get("name", "") or "",
                s["targetType"],
                s["targetId"],
                s.get("hostId", "") or "",
                s["action"],
                s["cron"],
                s.get("timezone", "") or "",
                bool_to_int(bool(s.get("enabled", True))),
                json.dumps(s.get("onlyIf") or {}, separators=(",", ":")),
                last_run,
                last_status,
                created,
                now,
            ),
        )
        return self.get_schedule(sid)

    def delete_schedule(self, sid: str) -> None:
        cur = self._execute("DELETE FROM schedules WHERE id = ?", (sid,))
        if cur.rowcount == 0:
            raise NotFoundError("schedule not found")
        self._execute("DELETE FROM schedule_runs WHERE schedule_id = ?", (sid,))

    def mark_schedule_run(self, sid: str, at: str, status: str) -> None:
        self._execute(
            "UPDATE schedules SET last_run_at = ?, last_status = ? WHERE id = ?", (at, status, sid)
        )

    # -- runs -------------------------------------------------------------
    def add_schedule_run(self, run: Dict[str, Any]) -> Dict[str, Any]:
        rid = run.get("id") or str(uuid.uuid4())
        self._execute(
            "INSERT OR REPLACE INTO schedule_runs (" + _RUN_COLS + ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                rid,
                run.get("scheduleId", ""),
                run.get("scheduleName", ""),
                run.get("targetType", ""),
                run.get("targetId", ""),
                run.get("hostId", ""),
                run.get("action", ""),
                run.get("trigger", ""),
                run.get("status", ""),
                (run.get("message", "") or "")[:4000],
                run.get("scheduledFor", ""),
                run.get("startedAt") or _now(),
                run.get("finishedAt", ""),
            ),
        )
        return {**run, "id": rid}

    def list_schedule_runs(self, schedule_id: str = "", limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit or 100), 1000))
        if schedule_id:
            rows = self._query(
                f"SELECT {_RUN_COLS} FROM schedule_runs WHERE schedule_id = ? ORDER BY started_at DESC LIMIT ?",
                (schedule_id, limit),
            )
        else:
            rows = self._query(
                f"SELECT {_RUN_COLS} FROM schedule_runs ORDER BY started_at DESC LIMIT ?", (limit,)
            )
        return [_run_row(r) for r in rows]

    def last_schedule_run(self, schedule_id: str) -> Optional[Dict[str, Any]]:
        r = self._one(
            f"SELECT {_RUN_COLS} FROM schedule_runs WHERE schedule_id = ? ORDER BY started_at DESC LIMIT 1",
            (schedule_id,),
        )
        return _run_row(r) if r else None

    def prune_schedule_runs(self, before: str) -> int:
        cur = self._execute("DELETE FROM schedule_runs WHERE started_at < ?", (before,))
        return cur.rowcount
