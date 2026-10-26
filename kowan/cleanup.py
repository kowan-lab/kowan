"""Guided disk cleanup, and cleanup on a schedule.

``plan`` turns a host's ``docker system df`` (with the unused objects
listed) into categories of things that can go, each item with its size and a
default choice: stopped containers that no deployment owns, untagged images,
unused tagged images, unused anonymous volumes and idle build cache are picked;
stopped containers of a deployment (stopped on purpose, recreated on the
next deploy) and named volumes (they may hold data) are listed but left
unpicked. ``run`` removes what was picked after checking again that each item
is still unused, and reports what was freed.

A host can also clean itself on a schedule (daily or weekly at an hour) with
the safe categories only: never named volumes, never a deployment's
containers. The policy and the last result live in the settings table.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from kowan.events import Event
from kowan.models import DiskUsage
from kowan.workloads import LABEL_DEPLOYMENT, LABEL_PROJECT, compose_deployments

log = logging.getLogger("kowan.cleanup")

_ANON_VOLUME = re.compile(r"^[0-9a-f]{64}$")
# Unused tagged images younger than this are listed but not picked.
KEEP_RECENT_DAYS = 30
_CHECK_EVERY = 600.0


def _managed_by(labels: Dict[str, str], host_id: str, compose_deps: Dict[Any, str]) -> str:
    dep = labels.get(LABEL_DEPLOYMENT, "")
    if not dep and labels.get(LABEL_PROJECT):
        dep = compose_deps.get((host_id, labels[LABEL_PROJECT]), "")
    return dep


def plan(du: DiskUsage, host_id: str, store: Any, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Categories of removable objects with sizes and default choices."""
    now = now or datetime.now(timezone.utc)
    compose_deps = compose_deployments(store)
    names = {d.id: d.name for d in store.list_deployments()}
    cats: List[Dict[str, Any]] = []

    items = []
    for c in du.stopped_containers:
        dep = _managed_by(c.labels, host_id, compose_deps)
        items.append({"id": c.id, "name": c.name, "size": c.size, "detail": f"{c.image} · {c.state}",
                      "selected": not dep,
                      "note": f"part of {names.get(dep, 'a deployment')}, stopped on purpose; its next deploy recreates it" if dep else ""})
    cats.append({"id": "containers", "title": "Stopped containers",
                 "help": "Their own writable layer goes; images and volumes stay.", "items": items})

    dangling, unused = [], []
    for i in du.unused_images:
        age = (now - datetime.fromtimestamp(i.created, tz=timezone.utc)).days if i.created else None
        item = {"id": i.id, "name": i.name or i.id.replace("sha256:", "")[:12], "size": i.size,
                "detail": _age(age), "ageDays": age, "selected": True, "note": ""}
        if i.name and (age is None or age < KEEP_RECENT_DAYS):
            item["selected"] = False  # pulled or built lately: likely wanted again soon
        (unused if i.name else dangling).append(item)
    cats.append({"id": "dangling", "title": "Untagged images",
                 "help": "Leftovers of rebuilt or re-pulled images; nothing can use them.", "items": dangling})
    cats.append({"id": "images", "title": "Unused images",
                 "help": f"Tagged images no container uses; a deploy pulls them again when needed. "
                         f"Those newer than {KEEP_RECENT_DAYS} days are not picked.", "items": unused})

    vols = []
    for v in du.unused_volumes:
        anon = bool(_ANON_VOLUME.match(v.name))
        vols.append({"id": v.name, "name": v.name[:12] if anon else v.name, "size": v.size,
                     "detail": "anonymous" if anon else "named", "selected": anon,
                     "note": "" if anon else "may hold data no container is using right now"})
    cats.append({"id": "volumes", "title": "Unused volumes",
                 "help": "No container mounts them. Removing a volume deletes its data for good.", "items": vols})

    cache = [{"id": "build-cache", "name": "Build cache", "size": du.build_cache_reclaimable, "detail": "not in use",
              "selected": True, "note": ""}] if du.build_cache_reclaimable else []
    cats.append({"id": "build", "title": "Build cache", "help": "Rebuilt on the next image build.", "items": cache})

    for c in cats:
        c["total"] = sum(i["size"] for i in c["items"])
        c["selectedTotal"] = sum(i["size"] for i in c["items"] if i["selected"])
    return {
        "categories": cats,
        "total": sum(c["total"] for c in cats),
        "selectedTotal": sum(c["selectedTotal"] for c in cats),
        "disk": {"images": du.images_size, "containers": du.containers_size, "volumes": du.volumes_size,
                 "buildCache": du.build_cache_size},
    }


@dataclass
class Picked:
    containers: List[str] = field(default_factory=list)
    images: List[str] = field(default_factory=list)
    volumes: List[str] = field(default_factory=list)
    build_cache: bool = False


async def run(ops: Any, picked: Picked) -> Dict[str, Any]:
    """Remove what was picked that is still unused; returns freed bytes,
    counts and the items skipped or failed."""
    du = await ops.disk_usage()
    stopped = {c.id: c for c in du.stopped_containers}
    images = {i.id: i for i in du.unused_images}
    volumes = {v.name: v for v in du.unused_volumes}
    freed = 0
    removed = {"containers": 0, "images": 0, "volumes": 0, "buildCache": 0}
    problems: List[Dict[str, str]] = []

    for cid in picked.containers:
        c = stopped.get(cid)
        if c is None:
            problems.append({"id": cid, "error": "running again or gone"})
            continue
        try:
            await ops.remove_container(cid, False)
            freed += c.size
            removed["containers"] += 1
        except Exception as err:  # noqa: BLE001
            problems.append({"id": cid, "error": str(err)})
    # Removing containers can free their images: look again before images.
    if removed["containers"] and any(i not in images for i in picked.images):
        images = {i.id: i for i in (await ops.disk_usage()).unused_images}
    for iid in picked.images:
        i = images.get(iid)
        if i is None:
            problems.append({"id": iid, "error": "in use again or gone"})
            continue
        try:
            await ops.remove_image(iid, False)
            freed += i.size
            removed["images"] += 1
        except Exception as err:  # noqa: BLE001
            problems.append({"id": iid, "error": str(err)})
    for name in picked.volumes:
        v = volumes.get(name)
        if v is None:
            problems.append({"id": name, "error": "in use again or gone"})
            continue
        try:
            await ops.remove_volume(name, False)
            freed += v.size
            removed["volumes"] += 1
        except Exception as err:  # noqa: BLE001
            problems.append({"id": name, "error": str(err)})
    if picked.build_cache:
        try:
            freed += int(await ops.prune_build_cache() or 0)
            removed["buildCache"] = 1
        except Exception as err:  # noqa: BLE001
            problems.append({"id": "build-cache", "error": str(err)})
    return {"freed": freed, "removed": removed, "problems": problems}


# ---------------------------------------------------------------------------
# schedule
# ---------------------------------------------------------------------------

DEFAULT_POLICY: Dict[str, Any] = {
    "enabled": False, "every": "weekly", "weekday": 6, "hour": 4,
    "containers": True, "dangling": True, "imagesOlderThanDays": 0, "anonymousVolumes": False, "buildCache": True,
}


def _policy_key(host_id: str) -> str:
    return f"cleanup.policy.{host_id}"


def _last_key(host_id: str) -> str:
    return f"cleanup.last.{host_id}"


def get_policy(store: Any, host_id: str) -> Dict[str, Any]:
    raw = store.get_setting(_policy_key(host_id))
    out = dict(DEFAULT_POLICY)
    if raw:
        out.update(json.loads(raw))
    return out


def validate_policy(body: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(DEFAULT_POLICY)
    unknown = set(body) - set(DEFAULT_POLICY)
    if unknown:
        raise ValueError(f"unknown field {sorted(unknown)[0]!r}")
    out.update(body)
    if out["every"] not in ("daily", "weekly"):
        raise ValueError('every must be "daily" or "weekly"')
    for key, lo, hi in (("hour", 0, 23), ("weekday", 0, 6), ("imagesOlderThanDays", 0, 3650)):
        if not isinstance(out[key], int) or isinstance(out[key], bool) or not lo <= out[key] <= hi:
            raise ValueError(f"{key} must be a whole number between {lo} and {hi}")
    for key in ("enabled", "containers", "dangling", "anonymousVolumes", "buildCache"):
        if not isinstance(out[key], bool):
            raise ValueError(f"{key} must be true or false")
    return out


def set_policy(store: Any, host_id: str, policy: Dict[str, Any]) -> None:
    store.set_setting(_policy_key(host_id), json.dumps(policy, sort_keys=True))


def last_run(store: Any, host_id: str) -> Optional[Dict[str, Any]]:
    raw = store.get_setting(_last_key(host_id))
    return json.loads(raw) if raw else None


def due(policy: Dict[str, Any], last_at: str, now: datetime) -> bool:
    """Whether a scheduled cleanup should run now: the right hour (and day),
    and not already done in this slot."""
    if not policy["enabled"] or now.hour != policy["hour"]:
        return False
    if policy["every"] == "weekly" and now.weekday() != policy["weekday"]:
        return False
    if not last_at:
        return True
    last = datetime.strptime(last_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return now - last > timedelta(hours=20)


def scheduled_pick(p: Dict[str, Any], policy: Dict[str, Any], now: datetime) -> Picked:
    """What a scheduled run removes: the safe default choices of the plan,
    limited to the categories the policy turns on."""
    by = {c["id"]: c for c in p["categories"]}
    pick = Picked()
    if policy["containers"]:
        pick.containers = [i["id"] for i in by["containers"]["items"] if i["selected"]]
    if policy["dangling"]:
        pick.images += [i["id"] for i in by["dangling"]["items"]]
    if policy["imagesOlderThanDays"]:
        limit = policy["imagesOlderThanDays"]
        pick.images += [i["id"] for i in by["images"]["items"] if i["ageDays"] is not None and i["ageDays"] >= limit]
    if policy["anonymousVolumes"]:
        pick.volumes = [i["id"] for i in by["volumes"]["items"] if i["detail"] == "anonymous"]
    pick.build_cache = bool(policy["buildCache"] and by["build"]["items"])
    return pick


class Scheduler:
    def __init__(self, svc: Any) -> None:
        self._svc = svc
        self._task: Optional[asyncio.Task] = None

    async def run_host(self, host: Any, now: Optional[datetime] = None) -> Dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        store = self._svc.store
        policy = get_policy(store, host.id)
        started = _time.monotonic()
        at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            ops = self._svc.docker.ops(host)
            p = plan(await ops.disk_usage(), host.id, store, now)
            result = await run(ops, scheduled_pick(p, policy, now))
            record = {"at": at, "freed": result["freed"], "removed": result["removed"],
                      "problems": len(result["problems"]), "error": "", "seconds": round(_time.monotonic() - started, 1)}
        except Exception as err:  # noqa: BLE001
            record = {"at": at, "freed": 0, "removed": {}, "problems": 0, "error": str(err), "seconds": 0}
        store.set_setting(_last_key(host.id), json.dumps(record))
        bus = getattr(self._svc, "bus", None)
        if bus is not None:
            title = f"Cleanup on {host.name} failed" if record["error"] else f"Cleanup on {host.name} freed {_human(record['freed'])}"
            bus.publish(Event(type="notice", source="cleanup", payload={
                "level": "warning" if record["error"] else "info", "title": title,
                "body": record["error"] or f"{sum(record['removed'].values())} objects removed.", "hostId": host.id,
                "source": "cleanup", "refId": host.id}))
        return record

    async def tick(self, now: Optional[datetime] = None) -> int:
        now = now or datetime.now(timezone.utc)
        n = 0
        for host in self._svc.store.list_hosts():
            policy = get_policy(self._svc.store, host.id)
            last = last_run(self._svc.store, host.id)
            if due(policy, (last or {}).get("at", ""), now):
                await self.run_host(host, now)
                n += 1
        return n

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

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as err:  # noqa: BLE001
                log.error("cleanup: %s", err)
            await asyncio.sleep(_CHECK_EVERY)


def _age(days: Optional[int]) -> str:
    if days is None:
        return ""
    if days < 1:
        return "from today"
    return "1 day old" if days == 1 else f"{days} days old"


def _human(n: float) -> str:
    """Bytes in 1024 steps, like the UI."""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"
