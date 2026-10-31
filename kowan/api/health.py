"""Health of Kowan itself (see docs/health.md).

    GET /system/health   (admin)

What the control plane is doing and whether it keeps up:

* the server: version, running since, Python;
* storage: the databases (with their write-ahead logs) and free space in the
  data folder;
* hosts: online, the round trip of a ping (through the agent's tunnel for a
  remote host), last seen, whether crash watching follows it;
* background loops: when each last ran against how often it should, so a
  stuck one shows as late;
* work in flight: runbooks running, images being pulled, open WebSockets.

Answers 200 with ``status`` ok / degraded (a loop late, a host offline, the
disk almost full); it never fails because one part could not be read.
"""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from aiohttp import web

from kowan import __version__
from kowan.web import api_route, bind_svc, json_response, require_role

STARTED_AT = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
_STARTED = time.monotonic()
PING_TIMEOUT = 3.0
# A loop is late when it has not run for this many of its intervals.
LATE_AFTER = 3
LOW_DISK = 0.05


def _age(ts: str) -> Optional[float]:
    try:
        t = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return (datetime.now(timezone.utc) - t).total_seconds()


def _loop(name: str, description: str, last: str, every: float) -> Dict[str, Any]:
    age = _age(last)
    late = age is None and time.monotonic() - _STARTED > every * LATE_AFTER + 60 or (age is not None and age > every * LATE_AFTER + 30)
    return {"name": name, "description": description, "lastRun": last, "everySeconds": every, "late": bool(late)}


def _files(data_dir: str) -> List[Dict[str, Any]]:
    out = []
    for base, what in (("kowan.db", "Configuration, history, metrics"), ("logs.db", "Retained logs")):
        size = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                size += os.path.getsize(os.path.join(data_dir, base + suffix))
            except OSError:
                pass
        out.append({"name": base, "description": what, "bytes": size})
    return out


async def _hosts(svc: Any, crashes: Any) -> List[Dict[str, Any]]:
    async def one(h: Any) -> Dict[str, Any]:
        row = {"id": h.id, "name": h.name, "type": h.type, "tags": h.tags, "lastSeen": h.last_seen,
               "online": False, "pingMs": None, "error": "",
               "crashWatch": bool(getattr(crashes, "connected", {}).get(h.id)) if crashes is not None else False}
        start = time.monotonic()
        try:
            await asyncio.wait_for(svc.docker.ops(h).ping(), timeout=PING_TIMEOUT)
            row["online"] = True
            row["pingMs"] = round((time.monotonic() - start) * 1000, 1)
        except asyncio.TimeoutError:
            row["error"] = f"no answer in {PING_TIMEOUT:.0f}s"
        except Exception as err:  # noqa: BLE001
            row["error"] = str(err) or err.__class__.__name__
        return row

    return list(await asyncio.gather(*(one(h) for h in svc.store.list_hosts())))


async def _health(svc, request: web.Request) -> web.Response:
    from kowan.docker import pulls
    from kowan.web.wsregistry import WS_KEY

    data_dir = getattr(getattr(svc, "config", None), "data_dir", "") or "."
    crashes = getattr(svc, "crashes", None)
    monitor = getattr(svc, "monitor", None)
    au = getattr(svc, "autoupdater", None)
    loops = []
    if monitor is not None:
        loops.append(_loop("Monitor", "Alert rules, host status, metrics", getattr(monitor, "last_tick_at", ""),
                           float(getattr(monitor, "_interval", 15))))
    if au is not None:
        from kowan.autoupdate.runner import load_settings

        loops.append(_loop("Auto-update", "Image checks and updates", au.last_tick_at or "",
                           float(load_settings(svc.store).get("intervalSeconds") or 3600)))
    uptime = getattr(svc, "uptime", None)
    if uptime is not None:
        loops.append(_loop("Uptime checks", "HTTP and TCP checks", getattr(uptime, "last_tick_at", ""), 1.0))
    collector = getattr(svc, "log_collector", None)
    if collector is not None:
        loops.append(_loop("Log retention", "Follows the logs it keeps", getattr(collector, "last_sync_at", ""), 5.0))

    try:
        du = shutil.disk_usage(data_dir)
        disk = {"path": os.path.abspath(data_dir), "total": du.total, "free": du.free}
    except OSError:
        disk = {"path": os.path.abspath(data_dir), "total": 0, "free": 0}
    hosts = await _hosts(svc, crashes)
    runbooks = getattr(svc, "runbooks", None)
    work = {
        "runbooksRunning": len(getattr(runbooks, "_running", {}) or {}),
        "pulls": sum(1 for h in hosts for p in pulls.TRACKER.active(h["id"]) if p["status"] == "pulling"),
        # Registered on the root application: config_dict reaches it.
        "websockets": len(request.config_dict.get(WS_KEY, ()) or ()),
        "crashSnapshotsTaken": getattr(crashes, "snapshots_taken", 0),
    }
    problems = []
    problems += [f"{lp['name']} has not run for a while" for lp in loops if lp["late"]]
    problems += [f"host {h['name']} is offline" for h in hosts if not h["online"]]
    if disk["total"] and disk["free"] / disk["total"] < LOW_DISK:
        problems.append("the data folder's disk is almost full")
    return json_response({
        "status": "degraded" if problems else "ok", "problems": problems,
        "server": {"version": __version__, "startedAt": STARTED_AT, "python": platform.python_version()},
        "storage": {"files": _files(data_dir), "disk": disk},
        "hosts": hosts, "loops": loops, "work": work,
    })


def register(app: web.Application, svc) -> None:
    api_route(app, "GET", "/system/health", require_role("admin")(bind_svc(svc)(_health)))
