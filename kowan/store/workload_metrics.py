"""Resource history per workload: one row per workload, host and 5-minute
bucket, written by the monitor (kowan/monitor/workloads.py).

Provides :class:`_WorkloadMetricMixin`, folded into :class:`kowan.store.store.Store`.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Dict, Iterable, List

if TYPE_CHECKING:  # pragma: no cover
    from .store import _StoreBase as _Base
else:
    _Base = object

WORKLOAD_METRICS_SCHEMA = """
CREATE TABLE IF NOT EXISTS workload_metrics (
    workload    TEXT NOT NULL,
    host_id     TEXT NOT NULL,
    time        TEXT NOT NULL,
    samples     INTEGER NOT NULL DEFAULT 0,
    containers  INTEGER NOT NULL DEFAULT 0,
    cpu_avg     REAL NOT NULL DEFAULT 0,
    cpu_max     REAL NOT NULL DEFAULT 0,
    mem_avg     INTEGER NOT NULL DEFAULT 0,
    mem_max     INTEGER NOT NULL DEFAULT 0,
    mem_limit   INTEGER NOT NULL DEFAULT 0,
    net_rx      INTEGER NOT NULL DEFAULT 0,
    net_tx      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (workload, host_id, time)
);
CREATE INDEX IF NOT EXISTS idx_workload_metrics_time ON workload_metrics (time);
-- The labels of a workload's containers, for access checks on its history.
CREATE TABLE IF NOT EXISTS workload_meta (
    workload TEXT NOT NULL,
    host_id  TEXT NOT NULL,
    labels   TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (workload, host_id)
);
"""

_COLS = ("workload", "host_id", "time", "samples", "containers", "cpu_avg", "cpu_max", "mem_avg", "mem_max",
         "mem_limit", "net_rx", "net_tx")

# Longer ranges are read in hourly buckets so a chart never gets thousands of points.
_HOURLY_AFTER_HOURS = 48


class _WorkloadMetricMixin(_Base):
    def add_workload_metrics(self, rows: Iterable[Dict[str, Any]]) -> None:
        batch = [tuple(r[c] for c in _COLS) for r in rows]
        if not batch:
            return
        with self._lock:
            self._conn.executemany(
                f"INSERT OR REPLACE INTO workload_metrics ({', '.join(_COLS)}) VALUES ({', '.join('?' * len(_COLS))})",
                batch,
            )
            self._conn.commit()

    def set_workload_labels(self, workload: str, host_id: str, labels: Dict[str, str]) -> None:
        self._execute(
            "INSERT INTO workload_meta (workload, host_id, labels) VALUES (?, ?, ?) "
            "ON CONFLICT(workload, host_id) DO UPDATE SET labels = excluded.labels",
            (workload, host_id, json.dumps(labels or {}, sort_keys=True)),
        )

    def workload_hosts(self, workload: str) -> Dict[str, Dict[str, str]]:
        """``host -> labels`` of the hosts a workload has history on."""
        rows = self._query("SELECT host_id, labels FROM workload_meta WHERE workload = ?", (workload,))
        return {r["host_id"]: json.loads(r["labels"] or "{}") for r in rows}

    def list_workload_metrics(self, workload: str, since: str, hours: float = 24) -> List[Dict[str, Any]]:
        """Points from ``since`` on, summed over the workload's hosts, oldest
        first. Ranges over two days come back in hourly buckets."""
        bucket = "substr(time, 1, 13) || ':00:00Z'" if hours > _HOURLY_AFTER_HOURS else "time"
        # Per (host, bucket) first, then summed across hosts: CPU and memory
        # of a workload spread over hosts add up.
        rows = self._query(
            f"SELECT t, SUM(containers) AS containers, SUM(cpu_avg) AS cpu_avg, SUM(cpu_max) AS cpu_max, "
            "SUM(mem_avg) AS mem_avg, SUM(mem_max) AS mem_max, SUM(mem_limit) AS mem_limit, "
            "SUM(net_rx) AS net_rx, SUM(net_tx) AS net_tx FROM ("
            f"  SELECT {bucket} AS t, host_id, MAX(containers) AS containers, AVG(cpu_avg) AS cpu_avg, "
            "   MAX(cpu_max) AS cpu_max, CAST(AVG(mem_avg) AS INTEGER) AS mem_avg, MAX(mem_max) AS mem_max, "
            "   MAX(mem_limit) AS mem_limit, MAX(net_rx) AS net_rx, MAX(net_tx) AS net_tx "
            "  FROM workload_metrics WHERE workload = ? AND time >= ? GROUP BY t, host_id"
            ") GROUP BY t ORDER BY t",
            (workload, since),
        )
        return [
            {"time": r["t"], "containers": r["containers"], "cpu": round(r["cpu_avg"], 2), "cpuMax": round(r["cpu_max"], 2),
             "mem": r["mem_avg"], "memMax": r["mem_max"], "memLimit": r["mem_limit"], "netRx": r["net_rx"], "netTx": r["net_tx"]}
            for r in rows
        ]

    def workload_memory_series(self, workload: str, host_id: str, since: str) -> List[tuple]:
        """``(time, mem_avg, mem_limit, containers)`` of one workload on one
        host, oldest first: the input of the memory-growth check."""
        rows = self._query(
            "SELECT time, mem_avg, mem_limit, containers FROM workload_metrics "
            "WHERE workload = ? AND host_id = ? AND time >= ? ORDER BY time",
            (workload, host_id, since),
        )
        return [(r["time"], r["mem_avg"], r["mem_limit"], r["containers"]) for r in rows]

    def prune_workload_metrics(self, before: str) -> int:
        n = self._execute("DELETE FROM workload_metrics WHERE time < ?", (before,)).rowcount
        self._execute(
            "DELETE FROM workload_meta WHERE NOT EXISTS (SELECT 1 FROM workload_metrics m "
            "WHERE m.workload = workload_meta.workload AND m.host_id = workload_meta.host_id)"
        )
        return n
