"""Uptime checks, their results and incidents.

Provides :class:`_UptimeMixin`, folded into :class:`kowan.store.store.Store`.
Checks are plain rows (no JSON columns); results are one row per probe and are
pruned by age; an incident is a down period (open while ``ended_at`` is "").
Timestamps are RFC3339 UTC strings, so they compare in SQL as text.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from ._common import NotFoundError, bool_to_int

if TYPE_CHECKING:  # pragma: no cover
    from .store import _StoreBase as _Base
else:
    _Base = object

UPTIME_SCHEMA = """
CREATE TABLE IF NOT EXISTS uptime_checks (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL UNIQUE,
    kind           TEXT NOT NULL,
    target         TEXT NOT NULL,
    method         TEXT NOT NULL DEFAULT 'GET',
    expect_status  TEXT NOT NULL DEFAULT '200-399',
    keyword        TEXT NOT NULL DEFAULT '',
    verify_tls     INTEGER NOT NULL DEFAULT 1,
    interval_sec   INTEGER NOT NULL DEFAULT 60,
    timeout_sec    INTEGER NOT NULL DEFAULT 10,
    fail_threshold INTEGER NOT NULL DEFAULT 2,
    deployment_id  TEXT NOT NULL DEFAULT '',
    channel_id     TEXT NOT NULL DEFAULT '',
    level          TEXT NOT NULL DEFAULT 'critical',
    enabled        INTEGER NOT NULL DEFAULT 1,
    public         INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'unknown',
    status_since   TEXT NOT NULL DEFAULT '',
    fails          INTEGER NOT NULL DEFAULT 0,
    last_checked_at TEXT NOT NULL DEFAULT '',
    last_latency_ms REAL NOT NULL DEFAULT 0,
    last_code      INTEGER NOT NULL DEFAULT 0,
    last_error     TEXT NOT NULL DEFAULT '',
    cert_expires_at TEXT NOT NULL DEFAULT '',
    alert_id       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS uptime_results (
    check_id   TEXT NOT NULL,
    at         TEXT NOT NULL,
    ok         INTEGER NOT NULL,
    latency_ms REAL NOT NULL DEFAULT 0,
    code       INTEGER NOT NULL DEFAULT 0,
    error      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS uptime_results_check_at ON uptime_results (check_id, at);

CREATE TABLE IF NOT EXISTS uptime_incidents (
    id         TEXT PRIMARY KEY,
    check_id   TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at   TEXT NOT NULL DEFAULT '',
    error      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS uptime_incidents_check ON uptime_incidents (check_id, started_at);
"""

# Columns a client may set; the rest is state the runner owns.
_CONFIG = (
    "name", "kind", "target", "method", "expect_status", "keyword", "verify_tls", "interval_sec",
    "timeout_sec", "fail_threshold", "deployment_id", "channel_id", "level", "enabled", "public",
)
_STATE = (
    "status", "status_since", "fails", "last_checked_at", "last_latency_ms", "last_code",
    "last_error", "cert_expires_at", "alert_id",
)
_BOOL = {"verify_tls", "enabled", "public"}


def _row(row: sqlite3.Row) -> Dict[str, Any]:
    out = dict(row)
    for k in _BOOL:
        out[k] = bool(out[k])
    return out


class _UptimeMixin(_Base):
    # -- checks --------------------------------------------------------------

    def create_uptime_check(self, c: Dict[str, Any]) -> None:
        cols = ("id", *_CONFIG, "created_at", "updated_at")
        vals = [bool_to_int(c[k]) if k in _BOOL else c[k] for k in cols]
        self._execute(
            f"INSERT INTO uptime_checks ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals
        )

    def update_uptime_check(self, c: Dict[str, Any]) -> None:
        """Save the configuration fields (state is written by save_uptime_state)."""
        sets = ", ".join(f"{k} = ?" for k in (*_CONFIG, "updated_at"))
        vals = [bool_to_int(c[k]) if k in _BOOL else c[k] for k in (*_CONFIG, "updated_at")]
        self._execute(f"UPDATE uptime_checks SET {sets} WHERE id = ?", (*vals, c["id"]))

    def save_uptime_state(self, check_id: str, state: Dict[str, Any]) -> None:
        keys = [k for k in _STATE if k in state]
        if not keys:
            return
        sets = ", ".join(f"{k} = ?" for k in keys)
        self._execute(f"UPDATE uptime_checks SET {sets} WHERE id = ?", (*[state[k] for k in keys], check_id))

    def get_uptime_check(self, check_id: str) -> Dict[str, Any]:
        row = self._one("SELECT * FROM uptime_checks WHERE id = ?", (check_id,))
        if row is None:
            raise NotFoundError("get uptime check: no rows in result set")
        return _row(row)

    def list_uptime_checks(self) -> List[Dict[str, Any]]:
        return [_row(r) for r in self._query("SELECT * FROM uptime_checks ORDER BY name")]

    def delete_uptime_check(self, check_id: str) -> None:
        self._execute("DELETE FROM uptime_checks WHERE id = ?", (check_id,))
        self._execute("DELETE FROM uptime_results WHERE check_id = ?", (check_id,))
        self._execute("DELETE FROM uptime_incidents WHERE check_id = ?", (check_id,))

    # -- results -------------------------------------------------------------

    def add_uptime_result(self, check_id: str, at: str, ok: bool, latency_ms: float, code: int, error: str) -> None:
        self._execute(
            "INSERT INTO uptime_results (check_id, at, ok, latency_ms, code, error) VALUES (?, ?, ?, ?, ?, ?)",
            (check_id, at, bool_to_int(ok), latency_ms, code, error),
        )

    def list_uptime_results(self, check_id: str, since: str, limit: int = 2000) -> List[Dict[str, Any]]:
        """Results at or after ``since``, oldest first (the newest ``limit``)."""
        rows = self._query(
            "SELECT at, ok, latency_ms, code, error FROM uptime_results WHERE check_id = ? AND at >= ? "
            "ORDER BY at DESC LIMIT ?",
            (check_id, since, limit),
        )
        out = [
            {"at": r["at"], "ok": bool(r["ok"]), "latencyMs": r["latency_ms"], "code": r["code"], "error": r["error"]}
            for r in rows
        ]
        out.reverse()
        return out

    def uptime_stats(self, check_id: str, since: str) -> Dict[str, Any]:
        """Share of successful probes and mean latency of the successful ones."""
        row = self._one(
            "SELECT COUNT(*) AS n, COALESCE(SUM(ok), 0) AS up, AVG(CASE WHEN ok = 1 THEN latency_ms END) AS lat "
            "FROM uptime_results WHERE check_id = ? AND at >= ?",
            (check_id, since),
        )
        n = row["n"] if row is not None else 0
        return {
            "probes": n,
            "uptime": (row["up"] / n * 100.0) if n else None,
            "latencyMs": row["lat"] if row is not None else None,
        }

    def uptime_daily(self, check_id: str, since: str) -> List[Dict[str, Any]]:
        """Per-day uptime (UTC days) for the status page bars."""
        rows = self._query(
            "SELECT substr(at, 1, 10) AS day, COUNT(*) AS n, SUM(ok) AS up FROM uptime_results "
            "WHERE check_id = ? AND at >= ? GROUP BY day ORDER BY day",
            (check_id, since),
        )
        return [{"date": r["day"], "uptime": r["up"] / r["n"] * 100.0, "probes": r["n"]} for r in rows]

    def prune_uptime_results(self, before: str) -> int:
        cur = self._execute("DELETE FROM uptime_results WHERE at < ?", (before,))
        self._execute("DELETE FROM uptime_incidents WHERE ended_at != '' AND ended_at < ?", (before,))
        return cur.rowcount if cur is not None else 0

    # -- incidents -------------------------------------------------------------

    def open_uptime_incident(self, incident_id: str, check_id: str, at: str, error: str) -> None:
        self._execute(
            "INSERT INTO uptime_incidents (id, check_id, started_at, error) VALUES (?, ?, ?, ?)",
            (incident_id, check_id, at, error),
        )

    def close_uptime_incident(self, check_id: str, at: str) -> None:
        self._execute(
            "UPDATE uptime_incidents SET ended_at = ? WHERE check_id = ? AND ended_at = ''", (at, check_id)
        )

    def list_uptime_incidents(self, check_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        if check_id:
            rows = self._query(
                "SELECT * FROM uptime_incidents WHERE check_id = ? ORDER BY started_at DESC LIMIT ?", (check_id, limit)
            )
        else:
            rows = self._query("SELECT * FROM uptime_incidents ORDER BY started_at DESC LIMIT ?", (limit,))
        return [
            {"id": r["id"], "checkId": r["check_id"], "startedAt": r["started_at"], "endedAt": r["ended_at"], "error": r["error"]}
            for r in rows
        ]
