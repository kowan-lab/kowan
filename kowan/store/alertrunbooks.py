"""Alert rule → runbook links: ``alert_rule_runbooks``.

Provides :class:`_AlertRunbookMixin`. A rule may name one runbook to run when
it fires (``kowan/runbooks/alerts.py``), at most once per ``cooldown_minutes``
for the same container. A table of its own, not columns on ``alert_rules``,
so existing databases get it with ``CREATE TABLE IF NOT EXISTS``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:  # pragma: no cover
    from .store import _StoreBase as _Base
else:
    _Base = object

ALERT_RUNBOOKS_SCHEMA = """
CREATE TABLE IF NOT EXISTS alert_rule_runbooks (
    rule_id          TEXT PRIMARY KEY,
    runbook_id       TEXT NOT NULL,
    cooldown_minutes INTEGER NOT NULL DEFAULT 10,
    updated_at       TEXT NOT NULL
);
"""


def _row(r) -> Dict[str, Any]:
    return {"ruleId": r["rule_id"], "runbookId": r["runbook_id"], "cooldownMinutes": r["cooldown_minutes"],
            "updatedAt": r["updated_at"]}


class _AlertRunbookMixin(_Base):
    def get_rule_runbook(self, rule_id: str) -> Optional[Dict[str, Any]]:
        r = self._one("SELECT rule_id, runbook_id, cooldown_minutes, updated_at FROM alert_rule_runbooks WHERE rule_id = ?",
                      (rule_id,))
        return _row(r) if r is not None else None

    def list_rule_runbooks(self) -> List[Dict[str, Any]]:
        return [_row(r) for r in self._query("SELECT rule_id, runbook_id, cooldown_minutes, updated_at FROM alert_rule_runbooks")]

    def set_rule_runbook(self, rule_id: str, runbook_id: str, cooldown_minutes: int) -> Dict[str, Any]:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._execute("INSERT OR REPLACE INTO alert_rule_runbooks (rule_id, runbook_id, cooldown_minutes, updated_at) "
                      "VALUES (?,?,?,?)", (rule_id, runbook_id, int(cooldown_minutes), now))
        return self.get_rule_runbook(rule_id)  # type: ignore[return-value]

    def delete_rule_runbook(self, rule_id: str) -> None:
        self._execute("DELETE FROM alert_rule_runbooks WHERE rule_id = ?", (rule_id,))
