"""Metric sample CRUD.

Provides :class:`_MetricMixin`, folded into :class:`kowan.store.store.Store`.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from kowan.models import MetricSample

if TYPE_CHECKING:  # pragma: no cover
    from .store import _StoreBase as _Base
else:
    _Base = object


_METRIC_COLUMNS = "host_id, time, running, total, images, cpu_percent, mem_used, mem_total"


def _row_to_metric(row: sqlite3.Row) -> MetricSample:
    return MetricSample(
        hostId=row["host_id"],
        time=row["time"],
        running=row["running"],
        total=row["total"],
        images=row["images"],
        cpuPercent=row["cpu_percent"],
        memUsed=row["mem_used"],
        memTotal=row["mem_total"],
    )


class _MetricMixin(_Base):
    def create_metric(self, m: MetricSample) -> None:
        """Insert a single time-series metric sample. Time is expected to be
        stamped server-side (RFC3339, UTC)."""
        self._execute(
            f"INSERT INTO metrics ({_METRIC_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (m.host_id, m.time, m.running, m.total, m.images, m.cpu_percent, m.mem_used, m.mem_total),
        )

    def list_metrics(self, host_id: str, since: str, step: int = 1) -> list[MetricSample]:
        """Return the samples for a host recorded at or after ``since`` (RFC3339),
        ordered by time ascending so charts plot left-to-right.

        Samples are averaged into buckets of ``step`` seconds, so a long range
        returns a bounded number of points instead of every row; with a step
        below the sampling interval each bucket holds one sample and comes back
        unchanged. Counts and totals take the bucket's maximum."""
        rows = self._query(
            "SELECT host_id, MIN(time) AS time, MAX(running) AS running, MAX(total) AS total, "
            "MAX(images) AS images, AVG(cpu_percent) AS cpu_percent, "
            "CAST(AVG(mem_used) AS INTEGER) AS mem_used, MAX(mem_total) AS mem_total "
            "FROM metrics WHERE host_id = ? AND time >= ? "
            "GROUP BY CAST(strftime('%s', time) AS INTEGER) / ? ORDER BY time ASC",
            (host_id, since, max(1, int(step))),
        )
        return [_row_to_metric(r) for r in rows]

    def prune_metrics(self, before: str) -> int:
        """Delete samples recorded strictly before the given RFC3339 timestamp and
        return the number of rows removed. Called periodically by the monitor to
        bound the metrics table to a retention window."""
        cur = self._execute("DELETE FROM metrics WHERE time < ?", (before,))
        return cur.rowcount
