"""Health verification of a freshly started container.

A container counts as verified when, before the deadline:

* it is ``Running`` and its ``RestartCount`` has not changed since verification
  began, continuously for ``stable`` seconds, AND
* if its config defines a HEALTHCHECK, ``State.Health.Status == "healthy"``.

It FAILS immediately when it exits / dies / is OOM-killed / restarts, or when
the health status becomes ``unhealthy``; and fails at the deadline otherwise
(e.g. still ``starting``). :func:`evaluate` is the pure rule (unit-tested);
:func:`wait_verified` is the polling loop around it.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

VERDICT_OK = "ok"
VERDICT_FAIL = "fail"
VERDICT_WAIT = "wait"


def has_healthcheck(inspect: Dict[str, Any]) -> bool:
    hc = ((inspect.get("Config") or {}).get("Healthcheck")) or {}
    test = hc.get("Test") or []
    if test and str(test[0]).upper() != "NONE":
        return True
    # Health state present means the daemon is running a check.
    return bool(((inspect.get("State") or {}).get("Health")))


def restart_count(inspect: Dict[str, Any]) -> int:
    try:
        return int(inspect.get("RestartCount") or 0)
    except (TypeError, ValueError):
        return 0


def evaluate(
    inspect: Dict[str, Any],
    baseline_restarts: int,
    stable_since: Optional[float],
    now: float,
    stable_seconds: float,
) -> Tuple[str, str, Optional[float]]:
    """Apply the verify rules to one inspect sample.

    Returns ``(verdict, reason, stable_since)`` where ``stable_since`` is the
    monotonic time the container was first seen running (carried across polls).
    """
    st = inspect.get("State") or {}
    status = str(st.get("Status") or "")
    rc = restart_count(inspect)
    if rc != baseline_restarts:
        return VERDICT_FAIL, f"container restarted (restart count {baseline_restarts} -> {rc})", None
    if st.get("OOMKilled"):
        return VERDICT_FAIL, "container was OOM-killed", None
    if st.get("Restarting") or status == "restarting":
        return VERDICT_FAIL, f"container is restarting (exit code {st.get('ExitCode', '?')})", None
    if status in ("exited", "dead", "removing") or (not st.get("Running") and status not in ("created", "")):
        err = st.get("Error") or ""
        return VERDICT_FAIL, f"container {status or 'stopped'} (exit code {st.get('ExitCode', '?')}){': ' + err if err else ''}", None
    if not st.get("Running"):
        return VERDICT_WAIT, "container not running yet", None
    if stable_since is None:
        stable_since = now
    if has_healthcheck(inspect):
        health = str(((st.get("Health") or {}).get("Status")) or "starting")
        if health == "unhealthy":
            log = (st.get("Health") or {}).get("Log") or []
            last = ""
            if log:
                last = str((log[-1] or {}).get("Output") or "").strip()[-300:]
            return VERDICT_FAIL, "healthcheck reports unhealthy" + (f": {last}" if last else ""), stable_since
        if health != "healthy":
            return VERDICT_WAIT, f"health: {health}", stable_since
    if now - stable_since < stable_seconds:
        return VERDICT_WAIT, f"running for {now - stable_since:.0f}s of {stable_seconds:.0f}s", stable_since
    return VERDICT_OK, "running" + (" and healthy" if has_healthcheck(inspect) else "") + f", stable for {stable_seconds:.0f}s", stable_since


async def wait_verified(
    inspect: Callable[[], Awaitable[Dict[str, Any]]],
    *,
    timeout: float,
    stable_seconds: float,
    poll_interval: float = 1.0,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> Tuple[bool, str]:
    """Poll ``inspect()`` until the container verifies, fails, or ``timeout`` passes."""
    start = monotonic()
    deadline = start + timeout
    baseline: Optional[int] = None
    stable_since: Optional[float] = None
    reason = "no sample"
    errors = 0
    while True:
        now = monotonic()
        try:
            insp = await inspect()
            errors = 0
        except Exception as err:  # noqa: BLE001 - transient engine error
            errors += 1
            reason = f"inspect failed: {err}"
            if errors >= 5:
                return False, reason
            insp = None
        if insp is not None:
            if baseline is None:
                baseline = restart_count(insp)
            verdict, reason, stable_since = evaluate(insp, baseline, stable_since, now, stable_seconds)
            if verdict == VERDICT_OK:
                return True, reason
            if verdict == VERDICT_FAIL:
                return False, reason
        if now >= deadline:
            return False, f"not healthy after {timeout:.0f}s ({reason})"
        await sleep(min(poll_interval, max(0.0, deadline - now)) or poll_interval)
