"""The log archive: its own SQLite file with an FTS5 index over the lines.

``log_sources`` names each followed container once (host, id, name, the
workload it belongs to); ``log_lines`` holds the lines with their Docker
timestamp normalised to a fixed width, so text order is time order;
``log_fts`` indexes the line text (kept in sync by triggers).
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS log_sources (
    id             INTEGER PRIMARY KEY,
    host_id        TEXT NOT NULL,
    container_id   TEXT NOT NULL,
    container_name TEXT NOT NULL DEFAULT '',
    workload       TEXT NOT NULL,
    stack          TEXT NOT NULL DEFAULT '',
    deployment_id  TEXT NOT NULL DEFAULT '',
    labels         TEXT NOT NULL DEFAULT '{}',
    last_ts        TEXT NOT NULL DEFAULT '',
    UNIQUE (host_id, container_id)
);
CREATE INDEX IF NOT EXISTS log_sources_workload ON log_sources (workload);
CREATE TABLE IF NOT EXISTS log_lines (
    id        INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL,
    ts        TEXT NOT NULL,
    line      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS log_lines_source_ts ON log_lines (source_id, ts);
CREATE INDEX IF NOT EXISTS log_lines_ts ON log_lines (ts);
CREATE VIRTUAL TABLE IF NOT EXISTS log_fts USING fts5(line, content='log_lines', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS log_lines_ai AFTER INSERT ON log_lines BEGIN
    INSERT INTO log_fts (rowid, line) VALUES (new.id, new.line);
END;
CREATE TRIGGER IF NOT EXISTS log_lines_ad AFTER DELETE ON log_lines BEGIN
    INSERT INTO log_fts (log_fts, rowid, line) VALUES ('delete', old.id, old.line);
END;
CREATE TABLE IF NOT EXISTS log_retention (
    workload   TEXT PRIMARY KEY,
    days       INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT ''
);
"""

_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})?$")


def normalize_ts(ts: str) -> str:
    """Any RFC3339 time as ``YYYY-MM-DDTHH:MM:SS.nnnnnnnnnZ`` in UTC (fixed
    width, so string order is time order). Raises ValueError."""
    m = _TS.match(ts.strip())
    if not m:
        raise ValueError(f"invalid time {ts!r}")
    base, frac, zone = m.group(1), (m.group(2) or ""), m.group(3) or "Z"
    if zone != "Z":
        dt = datetime.fromisoformat(base + zone).astimezone(timezone.utc)
        base = dt.strftime("%Y-%m-%dT%H:%M:%S")
    return f"{base}.{(frac + '0' * 9)[:9]}Z"


def fts_query(text: str) -> Tuple[str, List[str]]:
    """Turn what a user typed into an FTS5 MATCH expression.

    Words and "quoted phrases" must all appear; a leading ``-`` excludes. Every
    term is quoted, so FTS operators in the input are just text. Returns
    ``(match, excluded)``; ``match`` is "" when only exclusions were given.
    """
    terms = re.findall(r'-?"[^"]*"|\S+', text)
    must: List[str] = []
    excluded: List[str] = []
    for t in terms:
        neg = t.startswith("-") and len(t) > 1
        if neg:
            t = t[1:]
        t = t.strip('"')
        if not t:
            continue
        quoted = '"' + t.replace('"', '""') + '"'
        (excluded if neg else must).append(quoted)
    match = " AND ".join(must)
    if match and excluded:
        match = f"({match}) NOT ({' OR '.join(excluded)})"
    return match, excluded


class LogStore:
    def __init__(self, path: str) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=5000;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _q(self, sql: str, params: Sequence[Any] = ()) -> List[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, tuple(params)))

    def _x(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    # -- retention policies ------------------------------------------------------

    def list_retention(self) -> List[Dict[str, Any]]:
        rows = self._q(
            "SELECT r.workload, r.days, r.created_at, r.created_by, "
            "(SELECT COUNT(*) FROM log_lines l JOIN log_sources s ON s.id = l.source_id WHERE s.workload = r.workload) AS lines, "
            "(SELECT MIN(l.ts) FROM log_lines l JOIN log_sources s ON s.id = l.source_id WHERE s.workload = r.workload) AS oldest "
            "FROM log_retention r ORDER BY r.workload"
        )
        return [
            {"workload": r["workload"], "days": r["days"], "createdAt": r["created_at"], "createdBy": r["created_by"],
             "lines": r["lines"], "oldest": r["oldest"] or ""}
            for r in rows
        ]

    def retention(self) -> Dict[str, int]:
        return {r["workload"]: r["days"] for r in self._q("SELECT workload, days FROM log_retention")}

    def set_retention(self, workload: str, days: int, at: str, by: str) -> None:
        self._x(
            "INSERT INTO log_retention (workload, days, created_at, created_by) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(workload) DO UPDATE SET days = excluded.days",
            (workload, days, at, by),
        )

    def delete_retention(self, workload: str) -> int:
        """Stop keeping a workload's logs and delete its lines."""
        self._x("DELETE FROM log_retention WHERE workload = ?", (workload,))
        n = self._x(
            "DELETE FROM log_lines WHERE source_id IN (SELECT id FROM log_sources WHERE workload = ?)", (workload,)
        ).rowcount
        self._x("DELETE FROM log_sources WHERE workload = ?", (workload,))
        return n

    # -- writing ---------------------------------------------------------------

    def source(self, host_id: str, container_id: str, name: str, workload: str, stack: str, deployment_id: str,
               labels: Optional[Dict[str, str]] = None) -> Tuple[int, str]:
        """Id and last stored timestamp of a container's source row (created
        or refreshed: a container can move to another workload). The labels
        are kept for access checks when searching."""
        self._x(
            "INSERT INTO log_sources (host_id, container_id, container_name, workload, stack, deployment_id, labels) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(host_id, container_id) DO UPDATE SET "
            "container_name = excluded.container_name, workload = excluded.workload, stack = excluded.stack, "
            "deployment_id = excluded.deployment_id, labels = excluded.labels",
            (host_id, container_id, name, workload, stack, deployment_id, json.dumps(labels or {}, sort_keys=True)),
        )
        row = self._q("SELECT id, last_ts FROM log_sources WHERE host_id = ? AND container_id = ?", (host_id, container_id))[0]
        return row["id"], row["last_ts"]

    def append(self, source_id: int, lines: Iterable[Tuple[str, str]]) -> int:
        batch = list(lines)
        if not batch:
            return 0
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.executemany(
                    "INSERT INTO log_lines (source_id, ts, line) VALUES (?, ?, ?)",
                    [(source_id, ts, line) for ts, line in batch],
                )
                self._conn.execute("UPDATE log_sources SET last_ts = ? WHERE id = ?", (batch[-1][0], source_id))
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return len(batch)

    def prune(self, now: datetime, cap: int) -> int:
        """Drop lines older than their workload's retention, lines of workloads
        without one, and the oldest lines beyond ``cap`` in total."""
        removed = 0
        policies = self.retention()
        for workload, days in policies.items():
            cutoff = normalize_ts((now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ"))
            removed += self._x(
                "DELETE FROM log_lines WHERE ts < ? AND source_id IN (SELECT id FROM log_sources WHERE workload = ?)",
                (cutoff, workload),
            ).rowcount
        placeholders = ",".join("?" * len(policies))
        orphan = f"SELECT id FROM log_sources WHERE workload NOT IN ({placeholders})" if policies else "SELECT id FROM log_sources"
        removed += self._x(f"DELETE FROM log_lines WHERE source_id IN ({orphan})", tuple(policies)).rowcount
        self._x(f"DELETE FROM log_sources WHERE id IN ({orphan})", tuple(policies))
        total = self._q("SELECT COUNT(*) AS n FROM log_lines")[0]["n"]
        if total > cap:
            removed += self._x(
                "DELETE FROM log_lines WHERE id IN (SELECT id FROM log_lines ORDER BY id LIMIT ?)", (total - cap,)
            ).rowcount
        return removed

    # -- reading ---------------------------------------------------------------

    def search(
        self, *, text: str = "", workloads: Optional[Sequence[str]] = None, host_id: str = "",
        since: str = "", until: str = "", before: int = 0, limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """Matching lines, newest first. ``before`` pages back (an id)."""
        where: List[str] = []
        params: List[Any] = []
        joins = ""
        match, excluded = fts_query(text)
        if match:
            joins = "JOIN log_fts ON log_fts.rowid = l.id"
            where.append("log_fts MATCH ?")
            params.append(match)
        elif excluded:
            where.append("l.id NOT IN (SELECT rowid FROM log_fts WHERE log_fts MATCH ?)")
            params.append(" OR ".join(excluded))
        if workloads is not None:
            if not workloads:
                return []
            where.append(f"s.workload IN ({','.join('?' * len(workloads))})")
            params.extend(workloads)
        if host_id:
            where.append("s.host_id = ?")
            params.append(host_id)
        if since:
            where.append("l.ts >= ?")
            params.append(since)
        if until:
            where.append("l.ts <= ?")
            params.append(until)
        if before:
            where.append("l.id < ?")
            params.append(before)
        sql = (
            "SELECT l.id, l.ts, l.line, s.host_id, s.container_id, s.container_name, s.workload, s.stack, s.deployment_id, s.labels "
            f"FROM log_lines l JOIN log_sources s ON s.id = l.source_id {joins} "
            + (f"WHERE {' AND '.join(where)} " if where else "")
            + "ORDER BY l.id DESC LIMIT ?"
        )
        params.append(limit)
        return [
            {"id": r["id"], "ts": r["ts"], "line": r["line"], "hostId": r["host_id"], "containerId": r["container_id"],
             "container": r["container_name"], "workload": r["workload"], "stack": r["stack"], "deploymentId": r["deployment_id"],
             "labels": json.loads(r["labels"] or "{}")}
            for r in self._q(sql, params)
        ]
