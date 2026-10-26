"""Resource history per workload, and the memory-growth warning.

The monitor already samples the stats of every running container each tick;
:class:`WorkloadHistory` folds those samples into one row per workload, host
and 5-minute bucket (average and peak CPU and memory, the memory limit, the
network counters). When a bucket closes, the workload's memory over the last
hours is checked for a steady climb (a likely leak): a straight-line fit that
explains the points well, a rise that matters in absolute and relative terms,
and no restart in between. A climb raises a warning alert (``memgrowth:``
rule id, published on the bus like every alert); it resolves once memory stops
climbing.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from kowan.events import Event
from kowan.models import Alert, Container, ContainerStats
from kowan.workloads import workload_of

log = logging.getLogger("kowan.monitor")

BUCKET_SECS = 300
KEEP_DAYS = 30
# The growth check looks at this window, needs at least MIN_POINTS buckets
# spanning MIN_SPAN, a fit at least this good, and a rise of at least
# MIN_GROWTH_BYTES and MIN_GROWTH_RATIO of the starting memory.
WINDOW = timedelta(hours=6)
MIN_POINTS = 24
MIN_SPAN = timedelta(hours=2)
MIN_R2 = 0.8
MIN_GROWTH_BYTES = 50 * 1024 * 1024
MIN_GROWTH_RATIO = 0.2

RULE_PREFIX = "memgrowth:"


def _rfc3339(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


@dataclass
class _Acc:
    """Samples of one workload on one host within the open bucket."""

    samples: int = 0
    containers: int = 0
    cpu_sum: float = 0.0
    cpu_max: float = 0.0
    mem_sum: int = 0
    mem_max: int = 0
    mem_limit: int = 0
    net_rx: int = 0
    net_tx: int = 0
    labels: Dict[str, str] = field(default_factory=dict)
    name: str = ""


def memory_trend(series: Sequence[Tuple[str, int, int, int]]) -> Optional[Dict[str, Any]]:
    """Least-squares line through ``(time, mem, limit, containers)`` points.

    Returns slope (bytes per hour), r2, the fitted start and end, the hours
    until the limit at that pace (None without a limit or a climb) and whether
    the container count changed (a restart or scale makes the fit
    meaningless). None with fewer than two points."""
    if len(series) < 2:
        return None
    t0 = _parse(series[0][0])
    xs = [(_parse(p[0]) - t0).total_seconds() / 3600 for p in series]
    ys = [float(p[1]) for p in series]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx
    intercept = my - slope * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys))
    r2 = 1.0 if ss_tot == 0 else max(0.0, 1 - ss_res / ss_tot)
    start, end = intercept, intercept + slope * xs[-1]
    limit = series[-1][2]
    hours_to_limit = (limit - ys[-1]) / slope if limit and slope > 0 and limit > ys[-1] else None
    return {
        "points": n, "spanHours": round(xs[-1], 2), "slopeBytesPerHour": int(slope), "r2": round(r2, 3),
        "start": int(start), "end": int(end), "limit": limit,
        "hoursToLimit": round(hours_to_limit, 1) if hours_to_limit is not None else None,
        "restarted": len({p[3] for p in series}) > 1,
    }


def is_growing(trend: Optional[Dict[str, Any]]) -> bool:
    if not trend or trend["restarted"] or trend["points"] < MIN_POINTS:
        return False
    if trend["spanHours"] < MIN_SPAN.total_seconds() / 3600 or trend["slopeBytesPerHour"] <= 0:
        return False
    rise = trend["end"] - trend["start"]
    return trend["r2"] >= MIN_R2 and rise >= MIN_GROWTH_BYTES and rise >= MIN_GROWTH_RATIO * max(trend["start"], 1)


def _mib(n: float) -> str:
    return f"{n / (1024 * 1024):.0f} MiB"


class WorkloadHistory:
    def __init__(self, store: Any, bus: Any = None) -> None:
        self._store = store
        self._bus = bus
        self._bucket: Optional[int] = None
        self._acc: Dict[Tuple[str, str], _Acc] = {}

    def observe(
        self, host_id: str, running: Dict[str, Container], stats: Dict[str, ContainerStats],
        compose_deps: Dict[Tuple[str, str], str], now: Optional[datetime] = None,
    ) -> None:
        """Fold one tick of a host into the open bucket; closing a bucket
        writes it and runs the growth check."""
        now = now or datetime.now(timezone.utc)
        bucket = int(now.timestamp()) // BUCKET_SECS
        if self._bucket is not None and bucket != self._bucket:
            self.flush()
        self._bucket = bucket
        per: Dict[str, _Acc] = {}
        for cid, c in running.items():
            st = stats.get(cid)
            if st is None:
                continue
            name = (c.names[0] if c.names else cid).lstrip("/")
            workload, _, _ = workload_of(c.labels or {}, host_id, name, compose_deps)
            a = per.setdefault(workload, _Acc(labels=dict(c.labels or {}), name=name))
            a.containers += 1
            a.cpu_sum += st.cpu_percent
            a.mem_sum += int(st.mem_usage)
            a.mem_limit += int(st.mem_limit)
            a.net_rx += int(st.net_rx)
            a.net_tx += int(st.net_tx)
        for workload, tick in per.items():
            acc = self._acc.setdefault((workload, host_id), _Acc(labels=tick.labels, name=tick.name))
            acc.samples += 1
            acc.containers = tick.containers
            acc.cpu_sum += tick.cpu_sum
            acc.cpu_max = max(acc.cpu_max, tick.cpu_sum)
            acc.mem_sum += tick.mem_sum
            acc.mem_max = max(acc.mem_max, tick.mem_sum)
            acc.mem_limit = tick.mem_limit
            acc.net_rx, acc.net_tx = tick.net_rx, tick.net_tx

    def flush(self) -> None:
        """Write the open bucket and check the workloads it covered."""
        if self._bucket is None or not self._acc:
            return
        at = _rfc3339(datetime.fromtimestamp(self._bucket * BUCKET_SECS, tz=timezone.utc))
        rows = [
            {"workload": w, "host_id": h, "time": at, "samples": a.samples, "containers": a.containers,
             "cpu_avg": a.cpu_sum / a.samples, "cpu_max": a.cpu_max, "mem_avg": a.mem_sum // a.samples,
             "mem_max": a.mem_max, "mem_limit": a.mem_limit, "net_rx": a.net_rx, "net_tx": a.net_tx}
            for (w, h), a in self._acc.items()
        ]
        acc, self._acc = self._acc, {}
        try:
            self._store.add_workload_metrics(rows)
            for (w, h), a in acc.items():
                self._store.set_workload_labels(w, h, a.labels)
        except Exception as err:  # noqa: BLE001 - history is best-effort
            log.error("monitor: workload history: %s", err)
            return
        end = datetime.fromtimestamp(self._bucket * BUCKET_SECS, tz=timezone.utc)
        for (w, h), a in acc.items():
            try:
                self.check_growth(w, h, a.name, end)
            except Exception as err:  # noqa: BLE001
                log.error("monitor: memory growth check %s: %s", w, err)

    def check_growth(self, workload: str, host_id: str, name: str, now: datetime) -> bool:
        series = self._store.workload_memory_series(workload, host_id, _rfc3339(now - WINDOW))
        trend = memory_trend(series)
        growing = is_growing(trend)
        rule = f"{RULE_PREFIX}{workload}"
        open_id = self._store.open_alert_for_rule(rule)
        if growing and not open_id:
            per_hour = trend["slopeBytesPerHour"]
            msg = f"Memory rose from {_mib(trend['start'])} to {_mib(trend['end'])} in {trend['spanHours']:.0f}h (+{_mib(per_hour)}/h) without a restart."
            if trend["hoursToLimit"] is not None:
                msg += f" At this pace it reaches its limit in about {trend['hoursToLimit']:.0f}h."
            a = Alert(id=str(uuid.uuid4()), ruleId=rule, hostId=host_id, level="warning",
                      title=f"Memory keeps growing: {name}", message=msg, resource=workload,
                      resolved=False, createdAt=_rfc3339(now))
            self._store.create_alert(a)
            self._publish(a)
        elif not growing and open_id:
            self._store.resolve_alert(open_id)
            self._publish(Alert(id=str(uuid.uuid4()), ruleId=rule, hostId=host_id, level="warning",
                                title=f"Memory stable again: {name}", message="The steady climb stopped.",
                                resource=workload, resolved=True, createdAt=_rfc3339(now)))
        return growing

    def prune(self, now: Optional[datetime] = None) -> int:
        now = now or datetime.now(timezone.utc)
        return self._store.prune_workload_metrics(_rfc3339(now - timedelta(days=KEEP_DAYS)))

    def _publish(self, a: Alert) -> None:
        if self._bus is None:
            return
        self._bus.publish(Event(type="alert", source="monitor", payload={
            "id": a.id, "ruleId": a.rule_id, "hostId": a.host_id, "level": a.level, "title": a.title,
            "message": a.message, "resource": a.resource, "channelId": a.channel_id,
            "resolved": a.resolved, "createdAt": a.created_at,
        }))


def growth_of(store: Any, workload: str, hosts: List[str], now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """The current memory trend of a workload (the host with the steepest
    climb when it runs on several), with ``growing`` set."""
    now = now or datetime.now(timezone.utc)
    best: Optional[Dict[str, Any]] = None
    for h in hosts:
        trend = memory_trend(store.workload_memory_series(workload, h, _rfc3339(now - WINDOW)))
        if trend and (best is None or trend["slopeBytesPerHour"] > best["slopeBytesPerHour"]):
            best = {**trend, "hostId": h, "growing": is_growing(trend)}
    return best
