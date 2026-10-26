"""Background monitor: poll every managed Docker host, evaluate alert rules,
raise/resolve alerts and record time-series metrics.

Each interval the monitor, for every host:

  * probes liveness with ``ops.ping()`` (authoritative — it never trusts the
    stale ``Host.online`` column);
  * evaluates ``host_down`` and emits an online/offline connectivity notice;
  * for online hosts, lists containers + samples per-container stats once and
    reuses that data for every container/disk rule and for the metric sample;
  * applies each rule's operator/threshold, sustained ``for`` duration, and
    auto-resolve, raising alerts (persisted + published on the bus as ``alert``)
    and auto-resolving them (published as ``notice``) when the condition clears.

The evaluation core (``_evaluate``/``compare``/``severity_of``/…) is pure; the
async loop wraps the synchronous store and awaits the async Docker Ops.

Concurrency note: the whole monitor runs as a single asyncio task on one event
loop, so plain dict access to its in-memory bookkeeping is already safe and no
lock is needed — the state
maps are only ever mutated from ``tick`` and its callees, which never yield to
another task mid-mutation.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from kowan.events import Event
from kowan.monitor.workloads import WorkloadHistory
from kowan.workloads import compose_deployments
from kowan.models import (
    Alert,
    AlertRule,
    Container,
    ContainerStats,
    DiskUsage,
    Host,
    MetricSample,
    SystemInfo,
)
from kowan.models.alert import (
    AlertLevelInfo,
    AlertLevelWarning,
    AlertTypeContainerDown,
    AlertTypeContainerExited,
    AlertTypeContainerRestarts,
    AlertTypeContainerUnhealthy,
    AlertTypeCPUHigh,
    AlertTypeDiskHigh,
    AlertTypeHostDown,
    AlertTypeMemHigh,
    OpEqual,
    OpGreaterEqual,
    OpLess,
    OpLessEqual,
    rule_type_by_name,
    valid_level,
)

log = logging.getLogger("kowan.monitor")

# EVENT_TYPE_ALERT is the bus event type published when an alert fires. It is
# consumed both by the notify Manager (external channels) and the notification
# center (the bell).
EVENT_TYPE_ALERT = "alert"

# EVENT_TYPE_NOTICE is the bus event type for noteworthy-but-not-alert events
# (host connectivity changes, alert auto-resolutions). It feeds the notification
# center only — it is deliberately NOT delivered to external channels.
EVENT_TYPE_NOTICE = "notice"

# _STATS_TIMEOUT bounds each per-container stats/inspect/ping sample (seconds) so
# a single slow host cannot stall a whole tick.
_STATS_TIMEOUT = 5.0

# _DEFAULT_METRICS_RETENTION_SECS is the fallback retention window for metric
# samples when the configured retention is non-positive (168h).
_DEFAULT_METRICS_RETENTION_SECS = 168 * 3600

# _DEFAULT_INTERVAL_SECS is the fallback tick interval when the configured
# interval is non-positive.
_DEFAULT_INTERVAL_SECS = 15.0

# _METRICS_PRUNE_EVERY bounds how often (in ticks) the monitor prunes metric
# samples older than the retention window. With the default 15s interval this is
# roughly hourly.
_METRICS_PRUNE_EVERY = 240

# _RETENTION_EVERY_SECS: how often the monitor applies the audit/alert/workflow
# run retention policies (kowan/store/retention.py). Wall-clock based so it stays
# hourly whatever the tick interval.
_RETENTION_EVERY_SECS = 3600.0

# _SAMPLE_CONCURRENCY bounds how many per-container stats/inspect calls run at
# once on a single host. LocalClient.container_stats uses the Engine's
# ``one-shot`` mode (API >= 1.41), which answers immediately and derives CPU%
# from the previous tick's sample; only the first sample of a container (or an
# older daemon) pays the legacy ~1-2s two-sample wait. A small bound keeps the
# daemon / agent tunnel from being flooded either way.
_SAMPLE_CONCURRENCY = 8


async def _bounded_gather(fn: Callable[[str], Any], ids: List[str]) -> None:
    """Run ``fn(id)`` for every id with at most :data:`_SAMPLE_CONCURRENCY` in
    flight. ``fn`` must swallow its own errors (the samplers above do)."""
    if not ids:
        return
    sem = asyncio.Semaphore(_SAMPLE_CONCURRENCY)

    async def _run(i: str) -> None:
        async with sem:
            await fn(i)

    await asyncio.gather(*(_run(i) for i in ids))


def _rfc3339_now() -> str:
    """UTC timestamp in RFC3339 form (second precision, ``Z`` suffix)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _CondState:
    """Lifecycle of a single rule×resource condition across ticks.

    ``pending_since`` is the monotonic time (``time.monotonic()``) when the
    condition first became true (``None`` = not pending), used for the sustained
    ``for`` gate. ``firing`` marks that an alert is currently active, and
    ``alert_id`` is that alert's id (used to auto-resolve).
    """

    __slots__ = ("pending_since", "firing", "alert_id")

    def __init__(self) -> None:
        self.pending_since: Optional[float] = None
        self.firing: bool = False
        self.alert_id: str = ""


class Monitor:
    """Periodically polls hosts and evaluates alert rules.

    Construct with the service container (``svc``), which supplies the store,
    docker provider, event bus and config. Call :meth:`start` (typically as a
    background asyncio task) to run the loop; :meth:`stop` requests shutdown.
    """

    def __init__(self, svc: Any) -> None:
        self._svc = svc
        self._store = svc.store
        self._provider = svc.docker
        self._bus = svc.bus

        cfg = getattr(svc, "config", None)
        interval = _duration_secs(getattr(cfg, "monitor_interval", None), _DEFAULT_INTERVAL_SECS)
        retention = _duration_secs(getattr(cfg, "metrics_retention", None), _DEFAULT_METRICS_RETENTION_SECS)
        self._interval: float = interval if interval > 0 else _DEFAULT_INTERVAL_SECS
        self._retention: float = retention if retention > 0 else _DEFAULT_METRICS_RETENTION_SECS

        # Retention for audit_log / resolved alerts / workflow runs (env
        # KOWAN_AUDIT_RETENTION / KOWAN_ALERT_RETENTION / KOWAN_WORKFLOW_RUNS_KEEP).
        # Missing config -> defaults; an explicit non-positive value disables.
        from kowan.store import retention as _ret

        self._audit_retention = getattr(cfg, "audit_retention", _ret.DEFAULT_AUDIT_RETENTION)
        self._alert_retention = getattr(cfg, "alert_retention", _ret.DEFAULT_ALERT_RETENTION)
        self._workflow_runs_keep = getattr(cfg, "workflow_runs_keep", _ret.DEFAULT_WORKFLOW_RUNS_KEEP)
        self._last_retention: Optional[float] = None

        # tick_count counts elapsed ticks; used to schedule periodic pruning.
        self._tick_count = 0
        self._stop = asyncio.Event()

        # Per-host snapshot of every container seen running at least once, keyed
        # by container id. container_down/exited rules use it both to know what
        # "should" be running and to label the alert.
        self._seen_running: Dict[str, Dict[str, Container]] = {}
        # Per-condition lifecycle state, keyed by condition_key.
        self._cond: Dict[str, _CondState] = {}
        # Last-observed connectivity per host, and which hosts we have already
        # observed once (to suppress a notice on the very first observation).
        self._host_online: Dict[str, bool] = {}
        self._host_seen: set[str] = set()
        # Resource history per workload and the memory-growth warning.
        self.history = WorkloadHistory(self._store, self._bus)
        self._compose_deps: Dict[Any, str] = {}

    # --- lifecycle ---

    async def start(self) -> None:
        """Run the monitor loop until :meth:`stop` is called (or the task is
        cancelled). Performs an immediate first tick, then ticks every interval."""
        log.info("monitor started (interval=%ss)", self._interval)
        try:
            # One tick immediately so alerts surface without waiting a full
            # interval after startup.
            await self._tick()
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
                except asyncio.TimeoutError:
                    pass
                if self._stop.is_set():
                    break
                await self._tick()
        except asyncio.CancelledError:
            log.info("monitor stopped")
            raise
        log.info("monitor stopped")

    def stop(self) -> None:
        """Request the loop to exit after the current tick."""
        self._stop.set()

    # --- tick ---

    async def _tick(self) -> None:
        """One monitoring pass across all hosts: evaluate rules and record a
        metric sample per online host, then prune old samples periodically."""
        self._tick_count += 1

        try:
            hosts = self._store.list_hosts()
        except Exception as err:  # noqa: BLE001
            log.error("monitor: list hosts: %s", err)
            return

        try:
            rules = self._store.list_alert_rules()
        except Exception as err:  # noqa: BLE001
            log.error("monitor: list alert rules: %s", err)
            return

        # Only enabled rules participate. Metrics are still recorded even with no
        # rules, so we do not return early here.
        enabled = [r for r in rules if r.enabled]
        try:
            self._compose_deps = compose_deployments(self._store)
        except Exception as err:  # noqa: BLE001
            log.debug("monitor: list deployments: %s", err)

        async def _one(h: Host) -> None:
            if self._stop.is_set():
                return
            try:
                await self._process_host(h, enabled)
            except Exception as err:  # noqa: BLE001
                # A single host's failure — or a poisoned rule that raises while
                # formatting its message — must never kill the whole monitor loop
                # (which would stop ALL metric recording and alerting until
                # restart). Log and carry on to the next host.
                log.error("monitor: process host %s: %s", getattr(h, "id", "?"), err)

        # Hosts are independent (per-host state is keyed by host id and every
        # mutation is synchronous between awaits), so probe them concurrently:
        # one slow/offline host (up to _STATS_TIMEOUT per call) no longer delays
        # every other host's sample and alert evaluation.
        await asyncio.gather(*(_one(h) for h in hosts))
        if self._stop.is_set():
            return

        # Periodically trim the metrics table to the retention window (on the
        # first tick and every _METRICS_PRUNE_EVERY ticks) to avoid a DELETE every
        # interval.
        if self._tick_count == 1 or self._tick_count % _METRICS_PRUNE_EVERY == 0:
            self._prune_metrics()
            try:
                self.history.prune()
            except Exception as err:  # noqa: BLE001
                log.error("monitor: prune workload history: %s", err)

        now = time.monotonic()
        if self._last_retention is None or now - self._last_retention >= _RETENTION_EVERY_SECS:
            self._last_retention = now
            await self._prune_retention()

    async def _prune_retention(self) -> None:
        """Apply the audit/alert/workflow-run retention policies in a worker
        thread (a first prune may delete a lot; the store is thread-safe and
        chunks its deletes). Never raises into the loop."""
        from kowan.store.retention import prune_retention

        try:
            await asyncio.to_thread(
                prune_retention,
                self._store,
                audit_retention=self._audit_retention,
                alert_retention=self._alert_retention,
                workflow_runs_keep=self._workflow_runs_keep,
            )
        except Exception as err:  # noqa: BLE001
            log.error("monitor: retention prune: %s", err)

    def _prune_metrics(self) -> None:
        """Delete metric samples older than the retention window. Errors are
        logged and never propagated so they cannot stall the loop."""
        before = datetime.now(timezone.utc)
        before = before.timestamp() - self._retention
        before_str = datetime.fromtimestamp(before, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            n = self._store.prune_metrics(before_str)
        except Exception as err:  # noqa: BLE001
            log.error("monitor: prune metrics: %s", err)
            return
        if n > 0:
            log.debug("monitor: pruned %d metric samples before %s", n, before_str)

    async def _process_host(self, h: Host, all_rules: List[AlertRule]) -> None:
        """Evaluate every rule scoped to a single host. Host-level rules
        (host_down) run even when offline; container/disk rules need a reachable
        Docker endpoint, so they run only when online. The single container+stats
        fetch is shared between rule evaluation and the metric sample."""
        rules = rules_for_host(all_rules, h.id)

        # Host liveness is not persisted as a boolean (the online column was
        # dropped); we probe the host ourselves each tick and use that
        # authoritative result for host_down alerts and connectivity notices.
        # Resolve Ops once here and reuse it below for the container/stats work.
        ops = None
        ops_err: Optional[Exception] = None
        try:
            ops = self._provider.ops(h)
        except Exception as err:  # noqa: BLE001 - provider.ops() failure
            ops_err = err

        online = ops is not None and await self._probe_online(ops)

        # Host-scoped rules and the connectivity notice run regardless of online
        # state — that is precisely when an offline host must be detected.
        self._eval_host_down(h, rules, online)
        self._note_host_transition(h, online)

        if not online:
            return

        # Reachable now: stamp last_seen so the UI's "last seen" reflects real
        # contact (the value is otherwise set only at enrollment). Best-effort —
        # a write failure must not abort the tick.
        try:
            self._store.set_host_last_seen(h.id, _rfc3339_now())
        except Exception as err:  # noqa: BLE001
            log.debug("monitor: persist last_seen host=%s: %s", h.id, err)

        if ops_err is not None or ops is None:
            log.debug("monitor: resolve ops host=%s: %s", h.id, ops_err)
            return

        try:
            containers = await ops.list_containers(True)
        except Exception as err:  # noqa: BLE001
            log.debug("monitor: list containers host=%s: %s", h.id, err)
            return

        # Forget containers that no longer exist at all (removed, or replaced by
        # a recreate/redeploy with a new id). Without this the per-host snapshot
        # and the per-condition state grew forever on hosts with container churn,
        # and every recreated container raised a never-resolving container_down
        # alert for its old id. A crashed/stopped container is still listed
        # (all=True), so it keeps alerting exactly as before.
        self._forget_removed(h.id, {c.id for c in containers})

        # Index running containers and refresh the per-host "seen running" snapshot.
        running: Dict[str, Container] = {}
        for c in containers:
            if is_running(c.state):
                running[c.id] = c
                self._mark_seen_running(h.id, c)

        # Sample per-container stats once for every running container. The same
        # samples feed both the cpu_high/mem_high rules and the aggregate metric.
        stats: Dict[str, ContainerStats] = {}

        async def _sample(cid: str) -> None:
            try:
                st = await asyncio.wait_for(ops.container_stats(cid), timeout=_STATS_TIMEOUT)
            except Exception as err:  # noqa: BLE001
                log.debug("monitor: container stats host=%s container=%s: %s", h.id, cid, err)
                return
            stats[cid] = st

        await _bounded_gather(_sample, list(running))

        # SystemInfo gives Images and MemTotal for the metric sample without extra
        # list calls. Its failure must not block rule evaluation, so best-effort.
        info: Optional[SystemInfo] = None
        try:
            info = await ops.system_info()
        except Exception as err:  # noqa: BLE001
            log.debug("monitor: system info host=%s: %s", h.id, err)

        # Evaluate the cheap rules inline; defer the ones needing an extra Docker
        # call (inspect for restart counts, disk usage) so they run at most once
        # per host per tick even with several rules of that type.
        need_restart = False
        need_disk = False
        for r in rules:
            if r.type == AlertTypeContainerDown:
                self._eval_container_down(h, r, running)
            elif r.type == AlertTypeCPUHigh:
                self._eval_resource(h, r, running, stats, lambda s: s.cpu_percent, "CPU", "%")
            elif r.type == AlertTypeMemHigh:
                self._eval_resource(h, r, running, stats, lambda s: s.mem_percent, "memory", "%")
            elif r.type == AlertTypeContainerUnhealthy:
                self._eval_unhealthy(h, r, running)
            elif r.type == AlertTypeContainerExited:
                self._eval_exited(h, r, containers)
            elif r.type == AlertTypeContainerRestarts:
                need_restart = True
            elif r.type == AlertTypeDiskHigh:
                need_disk = True

        if need_restart:
            await self._eval_restart_loop(h, rules, ops, running)
        if need_disk:
            await self._eval_disk(h, rules, ops)

        self._record_metric(h, containers, running, stats, info)
        self.history.observe(h.id, running, stats, self._compose_deps)

    def _record_metric(
        self,
        h: Host,
        containers: List[Container],
        running: Dict[str, Container],
        stats: Dict[str, ContainerStats],
        info: Optional[SystemInfo],
    ) -> None:
        """Aggregate the already-fetched container list and stats into a single
        per-host MetricSample and persist it. Errors are logged, never raised."""
        cpu_sum = 0.0
        mem_used = 0
        for cid in running:
            st = stats.get(cid)
            if st is None:
                continue
            cpu_sum += st.cpu_percent
            mem_used += int(st.mem_usage)

        sample = MetricSample(
            hostId=h.id,
            time=_rfc3339_now(),
            running=len(running),
            total=len(containers),
            cpuPercent=cpu_sum,
            memUsed=mem_used,
        )
        # SystemInfo is the preferred source for Images and MemTotal; only trust
        # it when the call succeeded so a transient failure does not record zeros
        # that look like a real drop on the chart.
        if info is not None:
            sample.images = info.images
            sample.mem_total = info.mem_total

        try:
            self._store.create_metric(sample)
        except Exception as err:  # noqa: BLE001
            log.error("monitor: create metric host=%s: %s", h.id, err)

    # --- per-rule-type evaluators ---

    def _eval_container_down(self, h: Host, r: AlertRule, running: Dict[str, Container]) -> None:
        """Fire when a container previously seen running on this host is no
        longer running."""
        for cid, snap in self._seen_running_snapshot(h.id).items():
            if not rule_matches_container(r, snap):
                continue
            up = cid in running
            key = condition_key(r.id, h.id, cid)
            msg = f"container {container_label(snap)} is no longer running on {host_label(h)}"
            self._evaluate(r, h, key, container_label(snap), not up, msg)

    def _eval_resource(
        self,
        h: Host,
        r: AlertRule,
        running: Dict[str, Container],
        stats: Dict[str, ContainerStats],
        extract: Callable[[ContainerStats], float],
        label: str,
        unit: str,
    ) -> None:
        """Fire when a running container's sampled metric satisfies the rule's
        operator/threshold. ``extract`` pulls the relevant percentage."""
        for cid, c in running.items():
            if not rule_matches_container(r, c):
                continue
            st = stats.get(cid)
            if st is None:
                continue
            value = extract(st)
            key = condition_key(r.id, h.id, cid)
            cond = compare(value, r.operator, r.threshold)
            msg = (
                f"container {container_label(c)} on {host_label(h)} reported "
                f"{label} {value:.1f}{unit} (threshold {r.operator} {_fmt_g(r.threshold)}{unit})"
            )
            self._evaluate(r, h, key, container_label(c), cond, msg)

    def _eval_unhealthy(self, h: Host, r: AlertRule, running: Dict[str, Container]) -> None:
        """Fire when a running container's Docker healthcheck reports unhealthy
        (read from the container status string, so no extra inspect call)."""
        for cid, c in running.items():
            if not rule_matches_container(r, c):
                continue
            key = condition_key(r.id, h.id, cid)
            cond = is_unhealthy(c)
            msg = f"container {container_label(c)} on {host_label(h)} is reporting unhealthy"
            self._evaluate(r, h, key, container_label(c), cond, msg)

    def _eval_exited(self, h: Host, r: AlertRule, containers: List[Container]) -> None:
        """Fire when a container that was seen running has since exited with a
        non-zero exit code. Gating on the seen-running set avoids alerting on
        long-dead one-shot containers at startup."""
        seen = self._seen_running_snapshot(h.id)
        for c in containers:
            if c.state != "exited" or not rule_matches_container(r, c):
                continue
            if c.id not in seen:
                continue
            code, ok = parse_exit_code(c.status)
            key = condition_key(r.id, h.id, c.id)
            cond = ok and code != 0
            msg = f"container {container_label(c)} on {host_label(h)} exited with code {code}"
            self._evaluate(r, h, key, container_label(c), cond, msg)

    async def _eval_restart_loop(
        self, h: Host, rules: List[AlertRule], ops: Any, running: Dict[str, Container]
    ) -> None:
        """Fire when a running container's restart count satisfies the rule's
        operator/threshold (a crash loop). RestartCount is not in the list
        response, so each running container is inspected once and the count
        reused across every restart_loop rule on the host."""
        counts: Dict[str, int] = {}

        async def _inspect(cid: str) -> None:
            try:
                doc = await asyncio.wait_for(ops.inspect_container(cid), timeout=_STATS_TIMEOUT)
            except Exception as err:  # noqa: BLE001
                log.debug("monitor: inspect container host=%s container=%s: %s", h.id, cid, err)
                return
            n, ok = inspect_restart_count(doc)
            if ok:
                counts[cid] = n

        await _bounded_gather(_inspect, list(running))

        for r in rules:
            if r.type != AlertTypeContainerRestarts:
                continue
            for cid, c in running.items():
                if not rule_matches_container(r, c):
                    continue
                if cid not in counts:
                    continue
                n = counts[cid]
                key = condition_key(r.id, h.id, cid)
                cond = compare(float(n), r.operator, r.threshold)
                msg = (
                    f"container {container_label(c)} on {host_label(h)} has restarted "
                    f"{n} times (threshold {r.operator} {_fmt_g(r.threshold)})"
                )
                self._evaluate(r, h, key, container_label(c), cond, msg)

    async def _eval_disk(self, h: Host, rules: List[AlertRule], ops: Any) -> None:
        """Fire when the disk consumed by Docker on the host (images +
        containers + volumes + build cache) satisfies the rule's operator/
        threshold, in GB. DiskUsage is fetched once and reused across all disk
        rules."""
        try:
            du: DiskUsage = await ops.disk_usage()
        except Exception as err:  # noqa: BLE001
            log.debug("monitor: disk usage host=%s: %s", h.id, err)
            return
        total_bytes = du.images_size + du.containers_size + du.volumes_size + du.build_cache_size
        gb = float(total_bytes) / 1e9
        for r in rules:
            if r.type != AlertTypeDiskHigh:
                continue
            key = condition_key(r.id, h.id, "")
            cond = compare(gb, r.operator, r.threshold)
            msg = (
                f"Docker on {host_label(h)} is using {gb:.1f} GB "
                f"(threshold {r.operator} {_fmt_g(r.threshold)} GB)"
            )
            self._evaluate(r, h, key, host_label(h), cond, msg)

    def _eval_host_down(self, h: Host, rules: List[AlertRule], online: bool) -> None:
        """Fire when the host's Docker endpoint is offline. ``online`` is the
        liveness result probed this tick (authoritative, unlike the stale
        persisted Host.online column)."""
        for r in rules:
            if r.type != AlertTypeHostDown:
                continue
            key = condition_key(r.id, h.id, "")
            msg = f"host {host_label(h)} is unreachable"
            self._evaluate(r, h, key, host_label(h), not online, msg)

    async def _probe_online(self, ops: Any) -> bool:
        """Whether the host's Docker endpoint responds to a Ping within a short
        timeout, mirroring the hosts-service liveness check. The bounded timeout
        keeps a hung host from stalling the tick."""
        try:
            await asyncio.wait_for(ops.ping(), timeout=_STATS_TIMEOUT)
            return True
        except Exception:  # noqa: BLE001
            return False

    # --- evaluation core ---

    def _evaluate(
        self, r: AlertRule, h: Host, key: str, resource: str, cond_true: bool, message: str
    ) -> None:
        """Apply the shared condition lifecycle for one rule×resource: honour the
        sustained-duration gate before firing, deduplicate while a condition
        persists, and auto-resolve when the condition clears."""
        st = self._cond_for(key)
        if cond_true:
            if st.firing:
                return  # already alerting; nothing to do
            if st.pending_since is None:
                st.pending_since = time.monotonic()
            d = float(r.for_duration)
            if d > 0 and (time.monotonic() - st.pending_since) < d:
                return  # condition not yet sustained long enough
            st.alert_id = self._raise(r, h, resource, message)
            st.firing = True
            st.pending_since = None
            return

        # Condition cleared.
        if st.firing and r.auto_resolve:
            self._resolve(r, h, resource, st.alert_id)
        st.firing = False
        st.alert_id = ""
        st.pending_since = None

    def _raise(self, r: AlertRule, h: Host, resource: str, message: str) -> str:
        """Persist an alert and publish it on the event bus, returning the new
        alert's id so the caller can auto-resolve it later. The alert level is
        the rule's severity (falling back to the rule type's default)."""
        a = Alert(
            id=str(uuid.uuid4()),
            ruleId=r.id,
            hostId=h.id,
            level=severity_of(r),
            title=r.name,
            message=message,
            resource=resource,
            channelId=r.channel_id,
            resolved=False,
            createdAt=_rfc3339_now(),
        )

        try:
            self._store.create_alert(a)
        except Exception as err:  # noqa: BLE001
            log.error("monitor: create alert rule=%s host=%s: %s", r.id, h.id, err)
            return ""

        log.info("alert raised level=%s rule=%s host=%s resource=%s", a.level, r.name, h.id, resource)

        if self._bus is not None:
            self._publish(
                Event(
                    type=EVENT_TYPE_ALERT,
                    source="monitor",
                    payload={
                        "id": a.id,
                        "ruleId": a.rule_id,
                        "hostId": a.host_id,
                        "level": a.level,
                        "title": a.title,
                        "message": a.message,
                        "resource": a.resource,
                        "channelId": a.channel_id,
                        "resolved": a.resolved,
                        "createdAt": a.created_at,
                    },
                )
            )
        return a.id

    def _resolve(self, r: AlertRule, h: Host, resource: str, alert_id: str) -> None:
        """Mark the active alert resolved and emit a notice (for the notification
        center) that the condition has cleared."""
        if alert_id:
            try:
                self._store.resolve_alert(alert_id)
            except Exception as err:  # noqa: BLE001
                log.error("monitor: auto-resolve alert %s: %s", alert_id, err)
        log.info("alert auto-resolved rule=%s host=%s resource=%s", r.name, h.id, resource)
        self._publish_notice(
            AlertLevelInfo,
            "Resolved: " + r.name,
            f"{resource} on {host_label(h)} has recovered",
            h.id,
            r.id,
        )

    def _note_host_transition(self, h: Host, online: bool) -> None:
        """Emit a notice when a host flips between online and offline relative to
        its previous observation. The first observation establishes a baseline
        and emits nothing."""
        seen = h.id in self._host_seen
        prev = self._host_online.get(h.id, False)
        self._host_seen.add(h.id)
        self._host_online[h.id] = online

        if not seen or prev == online:
            return
        if online:
            self._publish_notice(
                AlertLevelInfo, "Host online", f"{host_label(h)} is reachable again", h.id, ""
            )
        else:
            self._publish_notice(
                AlertLevelWarning, "Host offline", f"{host_label(h)} became unreachable", h.id, ""
            )

    def _publish_notice(self, level: str, title: str, body: str, host_id: str, ref_id: str) -> None:
        """Emit a notice event onto the bus for the notification center."""
        if self._bus is None:
            return
        self._publish(
            Event(
                type=EVENT_TYPE_NOTICE,
                source="monitor",
                payload={
                    "level": level,
                    "title": title,
                    "body": body,
                    "hostId": host_id,
                    "source": "monitor",
                    "refId": ref_id,
                },
            )
        )

    def _publish(self, event: Event) -> None:
        """Publish on the bus. The Python MemoryBus.publish is synchronous and
        non-blocking (bounded per-subscriber buffers), so no await is needed."""
        self._bus.publish(event)

    # --- in-memory state helpers ---

    def _mark_seen_running(self, host_id: str, c: Container) -> None:
        s = self._seen_running.get(host_id)
        if s is None:
            s = {}
            self._seen_running[host_id] = s
        s[c.id] = c

    def _forget_removed(self, host_id: str, present: set) -> None:
        """Drop seen-running entries (and their idle condition state) for
        containers absent from the host's full container list. Condition state
        that is currently firing is kept so its alert stays tracked."""
        s = self._seen_running.get(host_id)
        if not s:
            return
        gone = [cid for cid in s if cid not in present]
        if not gone:
            return
        for cid in gone:
            del s[cid]
        suffixes = tuple(f"|{host_id}|{cid}" for cid in gone)
        for key in [k for k in self._cond if k.endswith(suffixes)]:
            if not self._cond[key].firing:
                del self._cond[key]

    def _seen_running_snapshot(self, host_id: str) -> Dict[str, Container]:
        """A copy of the last-observed containers seen running on the host."""
        return dict(self._seen_running.get(host_id, {}))

    def _cond_for(self, key: str) -> _CondState:
        """The lifecycle state for a condition key, creating it on first use."""
        st = self._cond.get(key)
        if st is None:
            st = _CondState()
            self._cond[key] = st
        return st


# --- small pure helpers ---


def _duration_secs(value: Any, default: float) -> float:
    """Coerce a config duration (``timedelta``, number of seconds, or None) into
    seconds. Non-positive/None yields ``default``."""
    if value is None:
        return default
    total = value.total_seconds() if hasattr(value, "total_seconds") else float(value)
    return total if total > 0 else default


def _fmt_g(v: float) -> str:
    """Format a threshold compactly (trim a trailing ``.0``)."""
    # int(nan) raises ValueError and int(inf) raises OverflowError, so guard the
    # non-finite case first — an unguarded raise here would escape into the monitor tick and kill the loop.
    if not math.isfinite(v):
        return repr(v)
    if v == int(v):
        return str(int(v))
    return repr(v)


def rules_for_host(rules: List[AlertRule], host_id: str) -> List[AlertRule]:
    """The subset of rules scoped to the given host (empty HostID = all hosts)."""
    return [r for r in rules if r.host_id == "" or r.host_id == host_id]


def rule_matches_container(r: AlertRule, c: Container) -> bool:
    """Whether a container-scoped rule applies to ``c``, given the rule's optional
    name substring (Match) and label selector."""
    # kowan.notify=off (or false/0/no) opts a container out of alerts.
    if str((c.labels or {}).get("kowan.notify", "")).strip().lower() in ("off", "false", "0", "no", "none", "disabled"):
        return False
    if r.match != "" and r.match.lower() not in container_label(c).lower():
        return False
    if r.label_key != "":
        if r.label_key not in c.labels:
            return False
        if r.label_value != "" and c.labels[r.label_key] != r.label_value:
            return False
    return True


def severity_of(r: AlertRule) -> str:
    """Resolve a rule's alert level: its explicit severity if valid, otherwise the
    rule type's default, otherwise warning."""
    if valid_level(r.severity):
        return r.severity
    spec, ok = rule_type_by_name(r.type)
    if ok and spec is not None and spec.default_severity != "":
        return spec.default_severity
    return AlertLevelWarning


def compare(value: float, op: str, threshold: float) -> bool:
    """Apply a comparison operator. An unrecognized operator falls back to ``>``,
    matching the historical threshold behaviour."""
    if op == OpLess:
        return value < threshold
    if op == OpGreaterEqual:
        return value >= threshold
    if op == OpLessEqual:
        return value <= threshold
    if op == OpEqual:
        return value == threshold
    # OpGreater and anything unset.
    return value > threshold


def is_unhealthy(c: Container) -> bool:
    """Whether a container's status string carries Docker's ``(unhealthy)``
    healthcheck marker (e.g. ``Up 5 minutes (unhealthy)``)."""
    return "(unhealthy)" in c.status.lower()


def parse_exit_code(status: str) -> tuple[int, bool]:
    """Extract the exit code from an exited container's status string, e.g.
    ``Exited (137) 2 minutes ago`` -> (137, True)."""
    i = status.find("(")
    j = status.find(")")
    if i < 0 or j <= i:
        return 0, False
    try:
        n = int(status[i + 1 : j].strip())
    except ValueError:
        return 0, False
    return n, True


def inspect_restart_count(doc: Dict[str, Any]) -> tuple[int, bool]:
    """Read RestartCount from a raw container inspect document. JSON numbers may
    decode as int or float through the mapping representation."""
    if "RestartCount" not in doc:
        return 0, False
    v = doc["RestartCount"]
    if isinstance(v, bool):
        return 0, False
    if isinstance(v, (int, float)):
        return int(v), True
    return 0, False


def condition_key(rule_id: str, host_id: str, container_id: str) -> str:
    return f"{rule_id}|{host_id}|{container_id}"


def is_running(state: str) -> bool:
    """Whether a container State string denotes a running container. Docker
    reports ``running`` for live containers."""
    return state == "running"


def short_id(id: str) -> str:
    """Truncate a docker ID to its short form for human-readable messages."""
    return id[:12] if len(id) > 12 else id


def container_label(c: Container) -> str:
    """The friendliest available identifier for a container."""
    if c.names and c.names[0] != "":
        name = c.names[0]
        return name[1:] if name.startswith("/") else name
    return short_id(c.id)


def host_label(h: Host) -> str:
    """The friendliest available identifier for a host."""
    return h.name if h.name != "" else h.id
