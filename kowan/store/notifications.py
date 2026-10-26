"""Notification-center CRUD.

Provides :class:`_NotificationMixin`, folded into :class:`kowan.store.store.Store`.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from kowan.models import Notification

from ._common import bool_to_int

if TYPE_CHECKING:  # pragma: no cover
    from .store import _StoreBase as _Base
else:
    _Base = object


_NOTIFICATION_COLUMNS = (
    "id, kind, level, title, body, host_id, source, ref_id, read, created_at"
)


def _row_to_notification(row: sqlite3.Row) -> Notification:
    return Notification(
        id=row["id"],
        kind=row["kind"],
        level=row["level"],
        title=row["title"],
        body=row["body"],
        hostId=row["host_id"],
        source=row["source"],
        refId=row["ref_id"],
        read=row["read"] != 0,
        createdAt=row["created_at"],
    )


class _NotificationMixin(_Base):
    def create_notification(self, n: Notification) -> None:
        """Insert a notification-center entry."""
        self._execute(
            f"INSERT INTO notifications ({_NOTIFICATION_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                n.id, n.kind, n.level, n.title, n.body, n.host_id, n.source,
                n.ref_id, bool_to_int(n.read), n.created_at,
            ),
        )

    def list_notifications(self, limit: int) -> list[Notification]:
        """Return recent notifications, newest first (limit <= 0 => default 50)."""
        if limit <= 0:
            limit = 50
        rows = self._query(
            f"SELECT {_NOTIFICATION_COLUMNS} FROM notifications "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [_row_to_notification(r) for r in rows]

    def count_unread_notifications(self) -> int:
        """Return how many notifications are unread."""
        row = self._one("SELECT COUNT(*) AS n FROM notifications WHERE read = 0")
        return row["n"] if row is not None else 0

    def mark_notification_read(self, id: str) -> None:
        """Mark a single notification as read."""
        self._execute("UPDATE notifications SET read = 1 WHERE id = ?", (id,))

    def mark_all_notifications_read(self) -> None:
        """Mark every notification as read."""
        self._execute("UPDATE notifications SET read = 1 WHERE read = 0")

    def prune_notifications(self, before: str) -> int:
        """Delete notifications older than the RFC3339 cutoff; return the count."""
        cur = self._execute(
            "DELETE FROM notifications WHERE created_at < ?", (before,)
        )
        return cur.rowcount
