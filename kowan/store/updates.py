"""Self-update history (``updates`` table), see ``docs/updates-design.md``.

Provides :class:`_UpdateMixin`, folded into :class:`kowan.store.store.Store`.
Records are plain dicts in the API (camelCase) shape so the orchestrator, the
reconciler and the HTTP handlers share one representation::

    {id, component, hostId (None for the server), mode, status,
     fromImage, fromDigest, fromVersion, toImage, toDigest, toVersion,
     phases: [{phase, ok, at, detail}], error, logs: [str],
     startedAt, finishedAt, startedBy, batchId}
"""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Tuple

from ._common import NotFoundError, StoreError

if TYPE_CHECKING:  # pragma: no cover
    from .store import _StoreBase as _Base
else:
    _Base = object

# (column, api key) pairs for the scalar text columns.
_TEXT_FIELDS: Tuple[Tuple[str, str], ...] = (
    ("id", "id"),
    ("component", "component"),
    ("mode", "mode"),
    ("status", "status"),
    ("from_image", "fromImage"),
    ("from_digest", "fromDigest"),
    ("from_version", "fromVersion"),
    ("to_image", "toImage"),
    ("to_digest", "toDigest"),
    ("to_version", "toVersion"),
    ("error", "error"),
    ("started_at", "startedAt"),
    ("finished_at", "finishedAt"),
    ("started_by", "startedBy"),
    ("batch_id", "batchId"),
)
_COLUMNS = ", ".join([c for c, _ in _TEXT_FIELDS] + ["host_id", "phases", "logs"])

UPDATES_SCHEMA = """
CREATE TABLE IF NOT EXISTS updates (
    id           TEXT PRIMARY KEY,
    component    TEXT NOT NULL,
    host_id      TEXT NOT NULL DEFAULT '',
    mode         TEXT NOT NULL DEFAULT 'update',
    status       TEXT NOT NULL,
    from_image   TEXT NOT NULL DEFAULT '',
    from_digest  TEXT NOT NULL DEFAULT '',
    from_version TEXT NOT NULL DEFAULT '',
    to_image     TEXT NOT NULL DEFAULT '',
    to_digest    TEXT NOT NULL DEFAULT '',
    to_version   TEXT NOT NULL DEFAULT '',
    phases       TEXT NOT NULL DEFAULT '[]',
    error        TEXT NOT NULL DEFAULT '',
    logs         TEXT NOT NULL DEFAULT '[]',
    started_at   TEXT NOT NULL,
    finished_at  TEXT NOT NULL DEFAULT '',
    started_by   TEXT NOT NULL DEFAULT '',
    batch_id     TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_updates_component ON updates (component, host_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_updates_status ON updates (status);
"""


def _loads_list(raw: Any) -> list:
    try:
        v = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []
    return v if isinstance(v, list) else []


def _row_to_update(row: sqlite3.Row) -> Dict[str, Any]:
    rec: Dict[str, Any] = {key: row[col] or "" for col, key in _TEXT_FIELDS}
    rec["hostId"] = row["host_id"] or None
    rec["phases"] = _loads_list(row["phases"])
    rec["logs"] = _loads_list(row["logs"])
    return rec


def _params(rec: Dict[str, Any]) -> list:
    vals = [str(rec.get(key) or "") for _, key in _TEXT_FIELDS]
    vals.append(str(rec.get("hostId") or ""))
    vals.append(json.dumps(list(rec.get("phases") or []), separators=(",", ":"), default=str))
    vals.append(json.dumps([str(x) for x in (rec.get("logs") or [])], separators=(",", ":")))
    return vals


class _UpdateMixin(_Base):
    def save_update(self, rec: Dict[str, Any]) -> None:
        """Insert or fully replace an update record (upsert on id)."""
        if not rec.get("id") or not rec.get("component") or not rec.get("status"):
            raise StoreError("update record needs id, component and status")
        placeholders = ", ".join("?" for _ in _COLUMNS.split(","))
        self._execute(
            f"INSERT OR REPLACE INTO updates ({_COLUMNS}) VALUES ({placeholders})",
            _params(rec),
        )

    def acquire_update(self, rec: Dict[str, Any], stale_before: str) -> Optional[Dict[str, Any]]:
        """Atomically insert ``rec`` unless another update of the same component/host
        is ``running``. Returns ``None`` on success or the blocking record.

        A running record started before ``stale_before`` (RFC3339) is considered a
        stale lock: it is marked ``unknown`` and the new record is inserted.
        """
        host = str(rec.get("hostId") or "")
        with self._lock:
            try:
                row = self._conn.execute(
                    f"SELECT {_COLUMNS} FROM updates WHERE component = ? AND host_id = ? "
                    "AND status = 'running' ORDER BY started_at DESC LIMIT 1",
                    (rec["component"], host),
                ).fetchone()
                if row is not None:
                    blocking = _row_to_update(row)
                    if (blocking.get("startedAt") or "") >= stale_before:
                        return blocking
                    self._conn.execute(
                        "UPDATE updates SET status = 'unknown', finished_at = ?, "
                        "error = CASE WHEN error = '' THEN 'stale: no result within the lock timeout' ELSE error END "
                        "WHERE id = ?",
                        (rec.get("startedAt") or "", blocking["id"]),
                    )
                placeholders = ", ".join("?" for _ in _COLUMNS.split(","))
                self._conn.execute(
                    f"INSERT INTO updates ({_COLUMNS}) VALUES ({placeholders})", _params(rec)
                )
                self._conn.commit()
            except sqlite3.Error as e:
                self._conn.rollback()
                raise StoreError(str(e)) from e
        return None

    def get_update(self, update_id: str) -> Dict[str, Any]:
        row = self._one(f"SELECT {_COLUMNS} FROM updates WHERE id = ?", (update_id,))
        if row is None:
            raise NotFoundError(f"update {update_id} not found")
        return _row_to_update(row)

    def list_updates(self, component: str, host_id: Optional[str] = None,
                     limit: int = 20) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit or 20), 200))
        rows = self._query(
            f"SELECT {_COLUMNS} FROM updates WHERE component = ? AND host_id = ? "
            "ORDER BY started_at DESC, rowid DESC LIMIT ?",
            (component, host_id or "", limit),
        )
        return [_row_to_update(r) for r in rows]

    def list_updates_by_status(self, statuses: Iterable[str]) -> List[Dict[str, Any]]:
        sts = list(statuses)
        if not sts:
            return []
        marks = ", ".join("?" for _ in sts)
        rows = self._query(
            f"SELECT {_COLUMNS} FROM updates WHERE status IN ({marks}) ORDER BY started_at ASC",
            sts,
        )
        return [_row_to_update(r) for r in rows]

    def last_update(self, component: str, host_id: Optional[str] = None,
                    statuses: Optional[Iterable[str]] = None) -> Optional[Dict[str, Any]]:
        sql = f"SELECT {_COLUMNS} FROM updates WHERE component = ? AND host_id = ?"
        params: list = [component, host_id or ""]
        sts = list(statuses or [])
        if sts:
            sql += " AND status IN (" + ", ".join("?" for _ in sts) + ")"
            params += sts
        sql += " ORDER BY started_at DESC, rowid DESC LIMIT 1"
        row = self._one(sql, params)
        return _row_to_update(row) if row is not None else None

    def prune_updates(self, component: str, host_id: Optional[str] = None, keep: int = 50) -> int:
        """Delete all but the newest ``keep`` records (running ones are never pruned)."""
        cur = self._execute(
            "DELETE FROM updates WHERE component = ? AND host_id = ? AND status != 'running' "
            "AND id NOT IN (SELECT id FROM updates WHERE component = ? AND host_id = ? "
            "ORDER BY started_at DESC, rowid DESC LIMIT ?)",
            (component, host_id or "", component, host_id or "", int(keep)),
        )
        return cur.rowcount or 0
