"""Crash snapshots: ``crash_snapshots``.

Provides :class:`_CrashMixin`. One row per crash Kowan caught
(``kowan/crashes``): the summary in columns, everything it saved (logs,
configuration, environment with secrets hidden, resources) in ``data`` JSON.
A container that keeps crashing adds to ``repeats`` of its last snapshot
instead of a new row every time. At most ``KEEP_PER_CONTAINER`` per
container and ``KEEP_DAYS`` of them are kept.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from ._common import NotFoundError

if TYPE_CHECKING:  # pragma: no cover
    from .store import _StoreBase as _Base
else:
    _Base = object

CRASHES_SCHEMA = """
CREATE TABLE IF NOT EXISTS crash_snapshots (
    id             TEXT PRIMARY KEY,
    host_id        TEXT NOT NULL,
    container_id   TEXT NOT NULL,
    container_name TEXT NOT NULL,
    workload       TEXT NOT NULL,
    image          TEXT NOT NULL DEFAULT '',
    exit_code      INTEGER NOT NULL DEFAULT 0,
    oom            INTEGER NOT NULL DEFAULT 0,
    repeats        INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    last_at        TEXT NOT NULL,
    data           TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_crash_snapshots_workload ON crash_snapshots (workload, created_at);
"""

KEEP_PER_CONTAINER = 20
KEEP_DAYS = 30

_COLS = "id, host_id, container_id, container_name, workload, image, exit_code, oom, repeats, created_at, last_at"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row(r, with_data: bool = False) -> Dict[str, Any]:
    out = {
        "id": r["id"], "hostId": r["host_id"], "containerId": r["container_id"], "containerName": r["container_name"],
        "workload": r["workload"], "image": r["image"], "exitCode": r["exit_code"], "oomKilled": bool(r["oom"]),
        "repeats": r["repeats"], "createdAt": r["created_at"], "lastAt": r["last_at"],
    }
    if with_data:
        try:
            out["data"] = json.loads(r["data"] or "{}")
        except ValueError:
            out["data"] = {}
    return out


class _CrashMixin(_Base):
    def add_crash_snapshot(self, snap: Dict[str, Any]) -> Dict[str, Any]:
        sid = str(uuid.uuid4())
        now = _now()
        self._execute(
            f"INSERT INTO crash_snapshots ({_COLS}, data) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, snap["hostId"], snap["containerId"], snap["containerName"], snap["workload"], snap.get("image", ""),
             int(snap.get("exitCode") or 0), 1 if snap.get("oomKilled") else 0, 0, now, now,
             json.dumps(snap.get("data") or {}, separators=(",", ":"))),
        )
        self._execute(
            "DELETE FROM crash_snapshots WHERE host_id = ? AND container_name = ? AND id NOT IN "
            "(SELECT id FROM crash_snapshots WHERE host_id = ? AND container_name = ? ORDER BY created_at DESC LIMIT ?)",
            (snap["hostId"], snap["containerName"], snap["hostId"], snap["containerName"], KEEP_PER_CONTAINER),
        )
        cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._execute("DELETE FROM crash_snapshots WHERE last_at < ?", (cutoff,))
        return self.get_crash_snapshot(sid)

    def note_crash_repeat(self, sid: str, exit_code: int) -> None:
        """Another crash of the same container while its snapshot is fresh."""
        self._execute("UPDATE crash_snapshots SET repeats = repeats + 1, last_at = ?, exit_code = ? WHERE id = ?",
                      (_now(), int(exit_code), sid))

    def get_crash_snapshot(self, sid: str) -> Dict[str, Any]:
        r = self._one(f"SELECT {_COLS}, data FROM crash_snapshots WHERE id = ?", (sid,))
        if r is None:
            raise NotFoundError("crash snapshot not found")
        return _row(r, with_data=True)

    def list_crash_snapshots(self, workload: str = "", host_id: str = "", limit: int = 50) -> List[Dict[str, Any]]:
        where, args = [], []
        if workload:
            where.append("workload = ?")
            args.append(workload)
        if host_id:
            where.append("host_id = ?")
            args.append(host_id)
        sql = f"SELECT {_COLS} FROM crash_snapshots"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(max(1, min(int(limit), 500)))
        return [_row(r) for r in self._query(sql, tuple(args))]

    def delete_crash_snapshot(self, sid: str) -> None:
        self.get_crash_snapshot(sid)
        self._execute("DELETE FROM crash_snapshots WHERE id = ?", (sid,))

    def last_crash_snapshot(self, host_id: str, name: str) -> Optional[Dict[str, Any]]:
        r = self._one(f"SELECT {_COLS} FROM crash_snapshots WHERE host_id = ? AND container_name = ? "
                      "ORDER BY created_at DESC LIMIT 1", (host_id, name))
        return _row(r) if r is not None else None
