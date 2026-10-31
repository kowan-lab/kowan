"""Crash snapshots: what a container looked like when it crashed.

A per-host loop follows the Docker events of containers (``die``, ``kill``,
``oom``). When a container dies with a non-zero exit code and nobody stopped
it (a ``docker stop`` or ``docker kill`` sends a ``kill`` event just before
the ``die``; a crash or the kernel's OOM killer does not), Kowan saves at once,
before a restart policy or a redeploy replaces it:

* the exit code, OOM flag, error, start/finish times and restart count;
* the last log lines;
* image, command, restart policy, limits, mounts, networks, ports and health;
* the environment, with the values of secret-looking names hidden;
* the workload's CPU and memory of the last half hour.

A container that keeps crashing gets one snapshot per ``REPEAT_WINDOW``; the
crashes in between are counted on it. ``kowan.snapshot=off`` on a container,
and Kowan's own throwaway containers, are skipped.

Python notes: each host has its own ``asyncio`` task reading the events
stream line by line; a reconcile loop starts tasks for new hosts, stops them
for deleted ones and restarts a stream that ended (host offline, agent
restarted) after a pause.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from kowan.workloads import compose_deployments, workload_of

log = logging.getLogger("kowan.crashes")

LABEL_OFF = "kowan.snapshot"
LABEL_SUPPORT = "kowan.support"
RECONCILE_INTERVAL = 30.0
RETRY_PAUSE = 15.0
# A kill this close before a die means the container was stopped on purpose.
KILL_WINDOW = 30.0
# One snapshot per container in this window; later crashes count on it.
REPEAT_WINDOW = 300.0
LOG_LINES = 200
MAX_LOGS = 64 * 1024
METRICS_MINUTES = 30

_SECRET = re.compile(r"(PASS|SECRET|TOKEN|KEY|PWD|CREDENTIAL|AUTH|PRIVATE|CERT|SALT|DSN|DATABASE_URL)", re.I)
_STAMP = re.compile(r"^\d{4}-\d\d-\d\dT[0-9:.]+(Z|[+-]\d\d:\d\d) ")


def redact_env(env: List[str]) -> List[Dict[str, str]]:
    """``KEY=value`` entries as ``{name, value}``, values of secret-looking
    names replaced (a snapshot is read by people who could not read them)."""
    out = []
    for kv in env or []:
        name, _, value = str(kv).partition("=")
        out.append({"name": name, "value": "••••••" if value and _SECRET.search(name) else value})
    return out


def _since(minutes: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


class CrashWatcher:
    def __init__(self, svc: Any, clock=time.monotonic) -> None:
        self.svc = svc
        self.store = svc.store
        self._clock = clock
        self._hosts: Dict[str, "asyncio.Task[None]"] = {}
        self._kills: Dict[str, float] = {}
        self._loop: Optional["asyncio.Task[None]"] = None
        self.snapshots_taken = 0
        self.started_at = ""
        self.connected: Dict[str, bool] = {}

    # -- lifecycle ---------------------------------------------------------------
    def start(self) -> None:
        if self._loop is None:
            self.started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            self._loop = asyncio.ensure_future(self._reconcile_loop())

    async def stop(self) -> None:
        tasks = [t for t in [self._loop, *self._hosts.values()] if t is not None]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._loop = None
        self._hosts.clear()

    async def _reconcile_loop(self) -> None:
        while True:
            try:
                self.reconcile()
            except Exception as err:  # noqa: BLE001
                log.error("crashes: reconcile: %s", err)
            await asyncio.sleep(RECONCILE_INTERVAL)

    def reconcile(self) -> None:
        want = {h.id for h in self.store.list_hosts()}
        for hid in [h for h in self._hosts if h not in want]:
            self._hosts.pop(hid).cancel()
            self.connected.pop(hid, None)
        for hid in want:
            t = self._hosts.get(hid)
            if t is None or t.done():
                self._hosts[hid] = asyncio.ensure_future(self._follow(hid))

    async def _follow(self, host_id: str) -> None:
        """Follow one host's container events; returns (to be restarted by
        the next reconcile) after a pause when the stream ends or fails."""
        stream = None
        try:
            ops = self.svc.docker.ops(self.store.get_host(host_id))
            stream = await ops.events(None, None, {"type": ["container"], "event": ["die", "kill", "oom"]})
            self.connected[host_id] = True
            buf = b""
            while True:
                chunk = await stream.read(65536)
                if not chunk:
                    break
                buf += chunk
                *lines, buf = buf.split(b"\n")
                for line in lines:
                    await self.on_event(host_id, ops, line)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - offline host, agent gone
            log.debug("crashes: events of %s: %s", host_id, err)
        finally:
            self.connected[host_id] = False
            if stream is not None:
                try:
                    res = stream.close()
                    if hasattr(res, "__await__"):
                        await res
                except Exception:  # noqa: BLE001
                    pass
        await asyncio.sleep(RETRY_PAUSE)

    # -- events --------------------------------------------------------------------
    async def on_event(self, host_id: str, ops: Any, line: bytes) -> Optional[Dict[str, Any]]:
        from kowan.api.events import parse_event_line

        ev = parse_event_line(line)
        if ev is None or ev.get("type") != "container":
            return None
        cid = ev.get("actorId", "")
        now = self._clock()
        if ev.get("action") == "kill":
            self._kills[cid] = now
            return None
        if ev.get("action") != "die":
            return None
        killed = self._kills.pop(cid, None)
        for k in [k for k, t in self._kills.items() if now - t > KILL_WINDOW]:
            del self._kills[k]
        code = str((ev.get("attributes") or {}).get("exitCode") or "0")
        if code == "0" or (killed is not None and now - killed <= KILL_WINDOW):
            return None
        try:
            return await self.snapshot(host_id, ops, cid, int(code))
        except Exception as err:  # noqa: BLE001 - gone already, host dropped
            log.warning("crashes: snapshot of %s on %s: %s", cid[:12], host_id, err)
            return None

    async def snapshot(self, host_id: str, ops: Any, cid: str, code: int) -> Optional[Dict[str, Any]]:
        insp = await asyncio.wait_for(ops.inspect_container(cid), timeout=15) or {}
        cfg = insp.get("Config") or {}
        labels = cfg.get("Labels") or {}
        if str(labels.get(LABEL_OFF, "")).lower() in ("off", "false", "0", "no") or labels.get(LABEL_SUPPORT) == "true":
            return None
        name = str(insp.get("Name") or cid[:12]).lstrip("/")
        last = self.store.last_crash_snapshot(host_id, name)
        if last is not None and _age(last["lastAt"]) < REPEAT_WINDOW:
            self.store.note_crash_repeat(last["id"], code)
            return None
        state = insp.get("State") or {}
        hc = insp.get("HostConfig") or {}
        workload, _, _ = workload_of(labels, host_id, name, compose_deployments(self.store))
        health = ((state.get("Health") or {}).get("Log") or [])[-1:] or [None]
        data = {
            "exitCode": code, "oomKilled": bool(state.get("OOMKilled")), "error": state.get("Error") or "",
            "startedAt": state.get("StartedAt") or "", "finishedAt": state.get("FinishedAt") or "",
            "restartCount": insp.get("RestartCount") or 0,
            "restartPolicy": (hc.get("RestartPolicy") or {}).get("Name") or "no",
            "image": cfg.get("Image") or "", "imageId": insp.get("Image") or "",
            "command": cfg.get("Cmd") or [], "entrypoint": cfg.get("Entrypoint") or [],
            "workingDir": cfg.get("WorkingDir") or "", "user": cfg.get("User") or "",
            "memoryLimit": hc.get("Memory") or 0, "cpus": (hc.get("NanoCpus") or 0) / 1e9,
            "mounts": [{"type": m.get("Type", ""), "source": m.get("Name") or m.get("Source", ""),
                        "destination": m.get("Destination", ""), "rw": bool(m.get("RW"))} for m in insp.get("Mounts") or []],
            "networks": sorted(((insp.get("NetworkSettings") or {}).get("Networks") or {}).keys()),
            "health": {"status": (state.get("Health") or {}).get("Status", ""),
                       "lastOutput": (health[0] or {}).get("Output", "")[-2000:] if health[0] else ""},
            "env": redact_env(cfg.get("Env") or []),
            "labels": {k: v for k, v in labels.items() if not _SECRET.search(k)},
            "logs": await self._logs(ops, cid),
            "metrics": self._metrics(workload),
        }
        snap = self.store.add_crash_snapshot({
            "hostId": host_id, "containerId": insp.get("Id") or cid, "containerName": name, "workload": workload,
            "image": data["image"], "exitCode": code, "oomKilled": data["oomKilled"], "data": data,
        })
        self.snapshots_taken += 1
        log.info("crash snapshot of %s on %s (exit %s)", name, host_id, code)
        return snap

    async def _logs(self, ops: Any, cid: str) -> str:
        try:
            stream = await asyncio.wait_for(ops.container_logs(cid, False, LOG_LINES), timeout=15)
        except Exception as err:  # noqa: BLE001
            return f"(logs not available: {err})"
        out = b""
        try:
            while len(out) < MAX_LOGS * 2:
                chunk = await asyncio.wait_for(stream.read(65536), timeout=15)
                if not chunk:
                    break
                out += chunk
        except Exception:  # noqa: BLE001 - keep what came
            pass
        finally:
            try:
                res = stream.close()
                if hasattr(res, "__await__"):
                    await res
            except Exception:  # noqa: BLE001
                pass
        text = out.decode("utf-8", "replace")
        return "\n".join(_STAMP.sub("", line) for line in text.splitlines())[-MAX_LOGS:]

    def _metrics(self, workload: str) -> List[Dict[str, Any]]:
        try:
            return self.store.list_workload_metrics(workload, _since(METRICS_MINUTES), METRICS_MINUTES / 60)
        except Exception:  # noqa: BLE001
            return []


def _age(rfc3339: str) -> float:
    try:
        t = datetime.strptime(rfc3339, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return float("inf")
    return (datetime.now(timezone.utc) - t).total_seconds()


__all__ = ["CrashWatcher", "redact_env"]
