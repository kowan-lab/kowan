"""Runbooks started by alerts.

An alert rule may name a runbook (``alert_rule_runbooks``): when the rule
fires, the runbook runs with its parameters' defaults and these environment
variables, so a step can act on what fired::

    KOWAN_ALERT_RULE       the rule's name
    KOWAN_ALERT_LEVEL      info | warning | critical
    KOWAN_ALERT_HOST       the host id
    KOWAN_ALERT_CONTAINER  the container's name ("" for a host alert)
    KOWAN_ALERT_MESSAGE    the alert text

A rule runs its runbook at most once per cooldown for the same container
(a flapping service does not start it over and over), and never twice at
once (the runbook service refuses). How it went is posted as a notice, so it
shows in the notification center and on the channels next to the alert.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional, Tuple

from kowan.events import Event

log = logging.getLogger("kowan.runbooks.alerts")

EVENT_TYPE_ALERT = "alert"
EVENT_TYPE_NOTICE = "notice"


class AlertRunbooks:
    def __init__(self, svc: Any, clock=time.monotonic) -> None:
        self.svc = svc
        self._clock = clock
        self._last: Dict[Tuple[str, str], float] = {}
        self._task: Optional["asyncio.Task[None]"] = None
        self._runs: set = set()

    def start(self) -> None:
        bus = getattr(self.svc, "bus", None)
        if bus is None or self._task is not None:
            return
        sub = bus.subscribe(EVENT_TYPE_ALERT)

        async def loop() -> None:
            try:
                async for ev in sub:
                    try:
                        await self.handle(ev.payload or {})
                    except Exception as err:  # noqa: BLE001 - one alert must not stop the loop
                        log.error("alert runbook: %s", err)
            finally:
                sub.cancel()

        self._task = asyncio.ensure_future(loop())

    async def stop(self) -> None:
        tasks = [t for t in [self._task, *self._runs] if t is not None]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._task = None

    async def handle(self, alert: Dict[str, Any]) -> Optional[str]:
        """Start the rule's runbook for one alert; returns the run id, or None
        when there is nothing to run (no link, cooling down, gone)."""
        rule_id = alert.get("ruleId") or ""
        if not rule_id or alert.get("resolved"):
            return None
        link = self.svc.store.get_rule_runbook(rule_id)
        if link is None:
            return None
        key = (rule_id, f"{alert.get('hostId', '')}/{alert.get('containerName', '')}")
        now = self._clock()
        last = self._last.get(key)
        if last is not None and now - last < int(link["cooldownMinutes"]) * 60:
            log.info("alert runbook: %s for %s cooling down", link["runbookId"], key[1])
            return None
        runbooks = self.svc.runbooks
        try:
            rb = await runbooks.get(link["runbookId"])
        except Exception:  # noqa: BLE001 - deleted since
            self._notice("warning", f"Runbook for {alert.get('title', 'an alert')} not found",
                         "The alert rule names a runbook that no longer exists.", alert)
            return None
        context = {
            "KOWAN_ALERT_RULE": alert.get("title", ""), "KOWAN_ALERT_LEVEL": alert.get("level", ""),
            "KOWAN_ALERT_HOST": alert.get("hostId", ""), "KOWAN_ALERT_CONTAINER": alert.get("containerName", ""),
            "KOWAN_ALERT_MESSAGE": alert.get("message", ""),
        }
        try:
            run = runbooks.start(rb, {}, f"alert: {alert.get('title', '')}", "alert", context)
        except ValueError as err:  # a required parameter without default, or already running
            self._notice("warning", f"Runbook {rb['name']} not started", str(err), alert)
            return None
        self._last[key] = now
        task = asyncio.ensure_future(self._report(rb, run["id"], alert))
        self._runs.add(task)
        task.add_done_callback(self._runs.discard)
        return run["id"]

    async def _report(self, rb: Dict[str, Any], run_id: str, alert: Dict[str, Any]) -> None:
        live = self.svc.runbooks.live(run_id)
        if live is not None:
            await live.done.wait()
            run = live.run
        else:
            run = self.svc.store.get_runbook_run(run_id)
        ok = run["status"] in ("succeeded", "warning")
        failed = next((r for r in run["steps"] if r["status"] != "succeeded"), None)
        why = (failed["error"] or f"exit code {failed['exitCode']}") if failed else ""
        self._notice("info" if ok else "warning",
                     f"Runbook {rb['name']} {run['status']} after {alert.get('title', 'an alert')}",
                     f"{alert.get('message', '')}" + (f" — {why}" if why else ""), alert, run_id)

    def _notice(self, level: str, title: str, body: str, alert: Dict[str, Any], ref: str = "") -> None:
        bus = getattr(self.svc, "bus", None)
        if bus is None:
            return
        bus.publish(Event(type=EVENT_TYPE_NOTICE, source="runbooks", payload={
            "level": level, "title": title, "body": body, "hostId": alert.get("hostId", ""),
            "source": "runbooks", "refId": ref or alert.get("id", "")}))
