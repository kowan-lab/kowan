"""Audit-log CRUD.

Provides :class:`_AuditMixin`, folded into :class:`kowan.store.store.Store`, plus
:class:`AuditListOptions`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from kowan.models import AuditEntry

if TYPE_CHECKING:  # pragma: no cover
    from .store import _StoreBase as _Base
else:
    _Base = object


@dataclass
class AuditListOptions:
    """Parameterizes :meth:`_AuditMixin.list_audit`.

    limit/offset paginate; user filters by a username substring (case-insensitive
    LIKE); since restricts to entries with a time at or after the given RFC3339
    timestamp; path keeps the entries for one resource: the exact path or any
    path below it (``/api/v1/deployments/{id}`` also matches ``…/{id}/deploy``).
    Zero/empty values disable the corresponding filter.
    """

    limit: int = 0
    offset: int = 0
    user: str = ""
    since: str = ""
    path: str = ""


def _like_escape(s: str) -> str:
    """Escape LIKE wildcards so ``s`` matches literally (used with ESCAPE '\\')."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _row_to_audit(row: sqlite3.Row) -> AuditEntry:
    return AuditEntry(
        id=row["id"],
        time=row["time"],
        userId=row["user_id"],
        username=row["username"],
        action=row["action"],
        path=row["path"],
        status=row["status"],
        ip=row["ip"],
        detail=row["detail"],
    )


# Rows deleted per statement by the retention prunes: keeps each write (and the
# store lock it holds) short even when the first prune has years to clear.
PRUNE_BATCH = 5000


class _AuditMixin(_Base):
    def prune_audit(self, before: str, batch: int = PRUNE_BATCH) -> int:
        """Delete audit entries with ``time`` strictly before ``before`` (RFC3339).

        Served by ``idx_audit_log_time``; deletes in ``batch``-sized chunks and
        returns the total number of rows removed.
        """
        total = 0
        while True:
            cur = self._execute(
                "DELETE FROM audit_log WHERE rowid IN "
                "(SELECT rowid FROM audit_log WHERE time < ? LIMIT ?)",
                (before, batch),
            )
            n = cur.rowcount or 0
            total += n
            if n < batch:
                return total

    def create_audit(self, e: AuditEntry) -> None:
        """Insert a single audit entry.

        The time field is stamped here server-side (UTC, RFC3339) when empty so
        callers never need to provide it.
        """
        if not e.time:
            e.time = (
                datetime.now(timezone.utc)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z")
            )
        self._execute(
            "INSERT INTO audit_log "
            "(id, time, user_id, username, action, path, status, ip, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                e.id, e.time, e.user_id, e.username, e.action, e.path,
                e.status, e.ip, e.detail,
            ),
        )

    def list_audit(self, opts: AuditListOptions) -> tuple[list[AuditEntry], int]:
        """Return audit entries newest-first with the total match count.

        Honors the user/since filters and limit/offset pagination and returns
        (entries, total) where total ignores limit/offset.
        """
        where: list[str] = []
        args: list[Any] = []
        u = opts.user.strip()
        if u:
            where.append("username LIKE ?")
            args.append("%" + u + "%")
        since = opts.since.strip()
        if since:
            where.append("time >= ?")
            args.append(since)
        path = opts.path.strip().rstrip("/")
        if path:
            where.append("(path = ? OR path LIKE ? ESCAPE '\\')")
            args.extend([path, _like_escape(path) + "/%"])

        clause = ""
        if where:
            clause = " WHERE " + " AND ".join(where)

        total_row = self._one("SELECT COUNT(*) AS n FROM audit_log" + clause, tuple(args))
        total = total_row["n"] if total_row is not None else 0

        query = (
            "SELECT id, time, user_id, username, action, path, status, ip, detail "
            "FROM audit_log" + clause + " ORDER BY time DESC"
        )
        list_args: list[Any] = list(args)
        if opts.limit > 0:
            query += " LIMIT ?"
            list_args.append(opts.limit)
            if opts.offset > 0:
                query += " OFFSET ?"
                list_args.append(opts.offset)

        rows = self._query(query, tuple(list_args))
        return [_row_to_audit(r) for r in rows], total
