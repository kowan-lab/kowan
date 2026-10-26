"""Schedules the uptime probes and turns their results into state and alerts.

Every second the runner starts the checks that are due (each on its own
interval, at most ``_CONCURRENCY`` at once). A check goes *down* after
``fail_threshold`` failures in a row: an alert is stored and published (the
notifier delivers it to the check's channel, or to every channel when none is
set) and an incident opens. The first success after that closes the incident,
resolves the alert and publishes a recovery on the same path, so channels hear
both ends. Results older than the retention are pruned hourly.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Set

from kowan.events import Event
from kowan.models import Alert

from . import probe

log = logging.getLogger("kowan.uptime")

_CONCURRENCY = 16
# HTTPS certificates are read again after this long.
_CERT_EVERY = 6 * 3600.0
_PRUNE_EVERY = 3600.0
DEFAULT_RETENTION = timedelta(days=90)


def _rfc3339(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class UptimeRunner:
    def __init__(self, svc: Any, retention: timedelta = DEFAULT_RETENTION) -> None:
        self._svc = svc
        self._store = svc.store
        self._bus = getattr(svc, "bus", None)
        self._retention = retention
        self._next: Dict[str, float] = {}
        self._cert_at: Dict[str, float] = {}
        self._running: Set[str] = set()
        self._sem = asyncio.Semaphore(_CONCURRENCY)
        self._task: Optional[asyncio.Task] = None
        self._pruned_at = 0.0
        self._locks: Dict[str, asyncio.Lock] = {}

    # -- lifecycle ---------------------------------------------------------

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

    def reschedule(self, check_id: str) -> None:
        """Run a check on the next tick (after a create or an edit)."""
        self._next.pop(check_id, None)
        self._cert_at.pop(check_id, None)

    async def _loop(self) -> None:
        while True:
            try:
                self.tick()
            except Exception as err:  # noqa: BLE001 - keep the loop alive
                log.error("uptime: tick: %s", err)
            await asyncio.sleep(1.0)

    def tick(self) -> None:
        now = time.monotonic()
        if now - self._pruned_at >= _PRUNE_EVERY:
            self._pruned_at = now
            self.prune()
        checks = self._store.list_uptime_checks()
        # Forget deleted checks.
        ids = {c["id"] for c in checks} | self._running
        for state in (self._next, self._cert_at, self._locks):
            for k in [k for k in state if k not in ids]:
                del state[k]
        for c in checks:
            if not c["enabled"] or c["id"] in self._running:
                continue
            if self._next.get(c["id"], 0.0) > now:
                continue
            self._next[c["id"]] = now + max(10, int(c["interval_sec"] or 60))
            self._running.add(c["id"])
            asyncio.ensure_future(self._guarded(c["id"]))

    async def _guarded(self, check_id: str) -> None:
        try:
            async with self._sem:
                await self.run(check_id)
        except Exception as err:  # noqa: BLE001
            log.error("uptime: check %s: %s", check_id, err)
        finally:
            self._running.discard(check_id)

    def prune(self) -> None:
        before = _rfc3339(datetime.now(timezone.utc) - self._retention)
        try:
            self._store.prune_uptime_results(before)
        except Exception as err:  # noqa: BLE001
            log.error("uptime: prune: %s", err)

    # -- one check -----------------------------------------------------------

    async def run(self, check_id: str) -> probe.Result:
        """Probe once, record the result and apply the state machine."""
        lock = self._locks.setdefault(check_id, asyncio.Lock())
        async with lock:
            c = self._store.get_uptime_check(check_id)
            timeout = float(max(1, int(c["timeout_sec"] or 10)))
            if c["kind"] == "tcp":
                res = await probe.tcp_probe(c["target"], timeout=timeout)
            else:
                res = await probe.http_probe(c["target"], method=c["method"], expect=c["expect_status"],
                                             keyword=c["keyword"], verify_tls=c["verify_tls"], timeout=timeout)
            at = probe.now_rfc3339()
            self._store.add_uptime_result(check_id, at, res.ok, round(res.latency_ms, 1), res.code, res.error)
            state: Dict[str, Any] = {
                "last_checked_at": at, "last_latency_ms": round(res.latency_ms, 1),
                "last_code": res.code, "last_error": res.error,
            }
            if c["kind"] == "http" and c["target"].startswith("https://"):
                if time.monotonic() - self._cert_at.get(check_id, -_CERT_EVERY) >= _CERT_EVERY:
                    self._cert_at[check_id] = time.monotonic()
                    exp = await probe.cert_expiry(c["target"], timeout=timeout)
                    if exp:
                        state["cert_expires_at"] = exp
            if res.ok:
                state["fails"] = 0
                if c["status"] != "up":
                    state["status"] = "up"
                    state["status_since"] = at
                    if c["status"] == "down":
                        self._recover(c, at)
                        state["alert_id"] = ""
            else:
                fails = int(c["fails"] or 0) + 1
                state["fails"] = fails
                if c["status"] != "down" and fails >= max(1, int(c["fail_threshold"] or 1)):
                    state["status"] = "down"
                    state["status_since"] = at
                    state["alert_id"] = self._go_down(c, at, res.error)
            self._store.save_uptime_state(check_id, state)
            return res

    def _go_down(self, c: Dict[str, Any], at: str, error: str) -> str:
        self._store.open_uptime_incident(str(uuid.uuid4()), c["id"], at, error)
        a = Alert(
            id=str(uuid.uuid4()), ruleId=f"uptime:{c['id']}", hostId="", level=c["level"] or "critical",
            title=f"Down: {c['name']}", message=f"{c['target']}: {error}", resource=c["target"],
            channelId=c["channel_id"], resolved=False, createdAt=at,
        )
        try:
            self._store.create_alert(a)
        except Exception as err:  # noqa: BLE001
            log.error("uptime: store alert for %s: %s", c["name"], err)
            return ""
        log.info("uptime: %s is down: %s", c["name"], error)
        self._publish(a)
        return a.id

    def _recover(self, c: Dict[str, Any], at: str) -> None:
        self._store.close_uptime_incident(c["id"], at)
        if c["alert_id"]:
            try:
                self._store.resolve_alert(c["alert_id"])
            except Exception as err:  # noqa: BLE001
                log.error("uptime: resolve alert for %s: %s", c["name"], err)
        log.info("uptime: %s is up again", c["name"])
        # Same level as the outage so channels that heard about it hear this too.
        self._publish(Alert(
            id=str(uuid.uuid4()), ruleId=f"uptime:{c['id']}", hostId="", level=c["level"] or "critical",
            title=f"Up again: {c['name']}", message=f"{c['target']} responds again", resource=c["target"],
            channelId=c["channel_id"], resolved=True, createdAt=at,
        ))

    def _publish(self, a: Alert) -> None:
        if self._bus is None:
            return
        self._bus.publish(Event(type="alert", source="uptime", payload={
            "id": a.id, "ruleId": a.rule_id, "hostId": a.host_id, "level": a.level, "title": a.title,
            "message": a.message, "resource": a.resource, "channelId": a.channel_id,
            "resolved": a.resolved, "createdAt": a.created_at,
        }))
