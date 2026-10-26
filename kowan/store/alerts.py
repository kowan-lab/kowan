"""Alert and alert-rule CRUD.

Provides :class:`_AlertMixin`, folded into :class:`kowan.store.store.Store`.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from kowan.models import Alert, AlertRule

from ._common import NotFoundError, bool_to_int

if TYPE_CHECKING:  # pragma: no cover
    from .store import _StoreBase as _Base
else:
    _Base = object


# alertColumns is the SELECT projection shared by the alert read paths.
_ALERT_COLUMNS = (
    "id, rule_id, host_id, level, title, message, resource, channel_id, "
    "resolved, created_at"
)

# alertRuleColumns is the SELECT projection shared by the alert-rule read paths.
_ALERT_RULE_COLUMNS = (
    "id, name, type, operator, threshold, severity, for_duration, "
    "host_id, match, label_key, label_value, channel_id, auto_resolve, "
    "enabled, created_at"
)


def _row_to_alert(row: sqlite3.Row) -> Alert:
    return Alert(
        id=row["id"],
        ruleId=row["rule_id"],
        hostId=row["host_id"],
        level=row["level"],
        title=row["title"],
        message=row["message"],
        resource=row["resource"],
        channelId=row["channel_id"],
        resolved=row["resolved"] != 0,
        createdAt=row["created_at"],
    )


def _row_to_alert_rule(row: sqlite3.Row) -> AlertRule:
    return AlertRule(
        id=row["id"],
        name=row["name"],
        type=row["type"],
        operator=row["operator"],
        threshold=row["threshold"],
        severity=row["severity"],
        forDuration=row["for_duration"],
        hostId=row["host_id"],
        match=row["match"],
        labelKey=row["label_key"],
        labelValue=row["label_value"],
        channelId=row["channel_id"],
        autoResolve=row["auto_resolve"] != 0,
        enabled=row["enabled"] != 0,
        createdAt=row["created_at"],
    )


class _AlertMixin(_Base):
    def create_alert(self, a: Alert) -> None:
        """Insert a new alert."""
        self._execute(
            f"INSERT INTO alerts ({_ALERT_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                a.id, a.rule_id, a.host_id, a.level, a.title, a.message,
                a.resource, a.channel_id, bool_to_int(a.resolved), a.created_at,
            ),
        )

    def list_alerts(self, limit: int) -> list[Alert]:
        """Return recent alerts, newest first (limit <= 0 => default 100)."""
        if limit <= 0:
            limit = 100
        rows = self._query(
            f"SELECT {_ALERT_COLUMNS} FROM alerts ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [_row_to_alert(r) for r in rows]

    def prune_resolved_alerts(self, before: str, batch: int = 5000) -> int:
        """Delete RESOLVED alerts created strictly before ``before`` (RFC3339).

        Active (unresolved) alerts are never pruned, however old. Served by
        ``idx_alerts_resolved_created``; deletes in chunks, returns rows removed.
        """
        total = 0
        while True:
            cur = self._execute(
                "DELETE FROM alerts WHERE rowid IN "
                "(SELECT rowid FROM alerts WHERE resolved = 1 AND created_at < ? LIMIT ?)",
                (before, batch),
            )
            n = cur.rowcount or 0
            total += n
            if n < batch:
                return total

    def open_alert_for_rule(self, rule_id: str) -> str:
        """Id of the newest unresolved alert raised by ``rule_id`` ("" if none)."""
        rows = self._query(
            "SELECT id FROM alerts WHERE rule_id = ? AND resolved = 0 ORDER BY created_at DESC LIMIT 1", (rule_id,)
        )
        return rows[0]["id"] if rows else ""

    def resolve_alert(self, id: str) -> None:
        """Mark an alert resolved."""
        self._execute("UPDATE alerts SET resolved = 1 WHERE id = ?", (id,))

    def create_alert_rule(self, r: AlertRule) -> None:
        """Insert a new alert rule."""
        self._execute(
            f"INSERT INTO alert_rules ({_ALERT_RULE_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                r.id, r.name, r.type, r.operator, r.threshold, r.severity,
                r.for_duration, r.host_id, r.match, r.label_key, r.label_value,
                r.channel_id, bool_to_int(r.auto_resolve), bool_to_int(r.enabled),
                r.created_at,
            ),
        )

    def get_alert_rule(self, id: str) -> AlertRule:
        """Return the alert rule with the given id, raising NotFoundError if absent."""
        row = self._one(
            f"SELECT {_ALERT_RULE_COLUMNS} FROM alert_rules WHERE id = ?", (id,)
        )
        if row is None:
            raise NotFoundError("get alert rule: no rows in result set")
        return _row_to_alert_rule(row)

    def list_alert_rules(self) -> list[AlertRule]:
        """Return all alert rules ordered by creation time."""
        rows = self._query(
            f"SELECT {_ALERT_RULE_COLUMNS} FROM alert_rules ORDER BY created_at"
        )
        return [_row_to_alert_rule(r) for r in rows]

    def update_alert_rule(self, r: AlertRule) -> None:
        """Update the mutable fields of an alert rule."""
        self._execute(
            "UPDATE alert_rules SET "
            "name = ?, type = ?, operator = ?, threshold = ?, severity = ?, "
            "for_duration = ?, host_id = ?, match = ?, label_key = ?, "
            "label_value = ?, channel_id = ?, auto_resolve = ?, enabled = ? "
            "WHERE id = ?",
            (
                r.name, r.type, r.operator, r.threshold, r.severity,
                r.for_duration, r.host_id, r.match, r.label_key, r.label_value,
                r.channel_id, bool_to_int(r.auto_resolve), bool_to_int(r.enabled),
                r.id,
            ),
        )

    def delete_alert_rule(self, id: str) -> None:
        """Remove an alert rule by id."""
        self._execute("DELETE FROM alert_rules WHERE id = ?", (id,))
