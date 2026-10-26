"""Follows the logs of the containers whose workload keeps logs.

Every few seconds the collector lists the running containers of every host,
works out which workload each belongs to (the same ids the UI uses:
``dep~<id>``, ``stack~<host>~<project>``, ``ctr~<host>~<name>``) and keeps one
follower per container of a workload with a retention. A follower streams the
container's log with timestamps, skips what is already stored (after a
restart it re-reads a short tail) and writes lines in small batches. Pruning
runs hourly.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from kowan.workloads import compose_deployments, workload_of

from .store import LogStore, normalize_ts

log = logging.getLogger("kowan.logstore")

_SYNC_EVERY = 5.0
_PRUNE_EVERY = 3600.0
_FLUSH_LINES = 200
_FLUSH_SECS = 0.5
_MAX_LINE = 8192
DEFAULT_CAP = 2_000_000


def split_line(raw: str) -> Optional[Tuple[str, str]]:
    """``"<rfc3339nano> text"`` -> (normalised ts, text); None if malformed."""
    ts, sep, text = raw.partition(" ")
    if not sep and not ts:
        return None
    try:
        nts = normalize_ts(ts)
    except ValueError:
        return None
    return nts, text[:_MAX_LINE]


class Collector:
    def __init__(self, svc: Any, logs: LogStore, cap: int = DEFAULT_CAP) -> None:
        self._svc = svc
        self._logs = logs
        self._cap = cap
        self._followers: Dict[Tuple[str, str], asyncio.Task] = {}
        self._task: Optional[asyncio.Task] = None
        self._pruned_at = 0.0

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self._stop_followers(list(self._followers))

    async def _stop_followers(self, keys: List[Tuple[str, str]]) -> None:
        tasks = [self._followers.pop(k) for k in keys if k in self._followers]
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    @property
    def following(self) -> List[Tuple[str, str]]:
        return sorted(self._followers)

    async def _loop(self) -> None:
        while True:
            try:
                await self.sync()
            except Exception as err:  # noqa: BLE001 - keep collecting
                log.error("logstore: sync: %s", err)
            await asyncio.sleep(_SYNC_EVERY)

    async def sync(self) -> None:
        """Start followers for the containers that should be followed and stop
        the others; prune when due."""
        now = time.monotonic()
        if now - self._pruned_at >= _PRUNE_EVERY:
            self._pruned_at = now
            try:
                removed = self._logs.prune(datetime.now(timezone.utc), self._cap)
                if removed:
                    log.info("logstore: pruned %d lines", removed)
            except Exception as err:  # noqa: BLE001
                log.error("logstore: prune: %s", err)
        policies = self._logs.retention()
        want: Dict[Tuple[str, str], Tuple[Any, str, str, str, str, Dict[str, str]]] = {}
        if policies:
            store = self._svc.store
            compose_deps = compose_deployments(store)
            for host in store.list_hosts():
                try:
                    ops = self._svc.docker.ops(host)
                    containers = await asyncio.wait_for(ops.list_containers(False), 10)
                except Exception as err:  # noqa: BLE001 - host offline: try next time
                    log.debug("logstore: list containers on %s: %s", host.id, err)
                    continue
                for c in containers:
                    name = (c.names[0] if c.names else c.id).lstrip("/")
                    workload, stack, dep = workload_of(c.labels or {}, host.id, name, compose_deps)
                    if workload in policies:
                        want[(host.id, c.id)] = (ops, name, workload, stack, dep, dict(c.labels or {}))
        stale = [k for k, t in self._followers.items() if k not in want or t.done()]
        await self._stop_followers(stale)
        for key, (ops, name, workload, stack, dep, labels) in want.items():
            if key not in self._followers:
                self._followers[key] = asyncio.ensure_future(
                    self.follow(ops, key[0], key[1], name, workload, stack, dep, labels))

    async def follow(self, ops: Any, host_id: str, container_id: str, name: str, workload: str, stack: str, dep: str,
                     labels: Optional[Dict[str, str]] = None) -> int:
        """Stream one container's log into the archive until it ends."""
        source_id, resume = self._logs.source(host_id, container_id, name, workload, stack, dep, labels)
        # After a restart the short tail overlaps what is stored: skip up to
        # the last stored time. Live lines are never compared (bursts can share
        # a timestamp).
        stream = await ops.container_logs(container_id, True, 1000 if resume else 200)
        pending: List[Tuple[str, str]] = []
        buf = ""
        flushed_at = time.monotonic()
        written = 0
        try:
            async for chunk in stream:
                buf += chunk.decode("utf-8", "replace") if isinstance(chunk, bytes) else str(chunk)
                *lines, buf = buf.split("\n")
                for raw in lines:
                    parsed = split_line(raw.rstrip("\r"))
                    if parsed is None or (resume and parsed[0] <= resume):
                        continue
                    pending.append(parsed)
                if pending and (len(pending) >= _FLUSH_LINES or time.monotonic() - flushed_at >= _FLUSH_SECS):
                    written += self._logs.append(source_id, pending)
                    pending = []
                    flushed_at = time.monotonic()
        finally:
            if buf:
                parsed = split_line(buf.rstrip("\r"))
                if parsed is not None and not (resume and parsed[0] <= resume):
                    pending.append(parsed)
            if pending:
                written += self._logs.append(source_id, pending)
            close = getattr(stream, "close", None)
            if close is not None:
                res = close()
                if asyncio.iscoroutine(res):
                    await res
        return written
