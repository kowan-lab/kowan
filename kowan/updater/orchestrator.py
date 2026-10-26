"""Control-plane side of self-update: status, lock, pull, preflight, snapshot, launch.

:class:`UpdateManager` is attached to the services container as ``svc.updates``
(``kowan/server.py``). The HTTP handlers call :meth:`UpdateManager.status`,
:meth:`UpdateManager.start` and :meth:`UpdateManager.start_batch`.

Flow of one update (``docs/updates-design.md`` §1), all against the TARGET's
Docker daemon through the generic :class:`kowan.docker.base.Ops` interface (the
local client for the server, the tunnel ``RemoteClient`` for an agent):

1. lock (an ``updates`` row with status ``running``; stale after 30 min) —
   synchronous, so a conflict is a 409 on the POST;
2. respond 202 and continue in a background task, recording each phase:
3. resolve + pull the image, check OS/arch against the daemon;
4. server: snapshot the database (SQLite online backup) — taken BEFORE the
   preflight so the preflight can migrate a throwaway copy of it;
5. preflight: run the NEW image once with ``preflight`` (60 s budget);
6. launch the helper from the CURRENT image id.

Anything failing in 3–5 ends as ``aborted`` with nothing touched; a helper that
cannot be created ends as ``failed`` (still nothing touched). After the launch,
the outcome is written by :mod:`kowan.updater.reconcile` from the helper's
journal.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from . import (
    COMPONENT_AGENT,
    COMPONENT_SERVER,
    DOCKER_SOCK,
    LABEL_ROLE,
    LABEL_UPDATE_ID,
    LABEL_VERSION,
    MODE_ROLLBACK,
    MODE_UPDATE,
    STATUS_ABORTED,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_UPDATED,
    helper_name,
    preflight_name,
    previous_name,
)
from . import dbsnap
from .plan import encode_plan, now_rfc3339

log = logging.getLogger("kowan.updater")

LOCK_STALE_SECONDS = 30 * 60
PREFLIGHT_TIMEOUT = 60.0
LATEST_CACHE_TTL = 300.0
BATCH_STEP_TIMEOUT = 20 * 60.0
SNAPSHOT_KEEP = 3

_SUFFIXES = ("-previous", "-retired-", "-failed-", "-next-")

_ARCH_ALIASES = {
    "x86_64": "amd64", "amd64": "amd64",
    "aarch64": "arm64", "arm64": "arm64",
    "armv7l": "arm", "armhf": "arm", "arm": "arm",
    "i386": "386", "i686": "386", "386": "386",
    "s390x": "s390x", "ppc64le": "ppc64le", "riscv64": "riscv64",
}


class UpdateError(Exception):
    """A request the caller should see as 4xx (``status`` is the HTTP code)."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class _Abort(Exception):
    pass


def norm_arch(arch: str) -> str:
    return _ARCH_ALIASES.get((arch or "").strip().lower(), (arch or "").strip().lower())


def _ts_minus(seconds: float) -> str:
    t = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    return t.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_ts(ts: str) -> Optional[float]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def image_version(image_inspect: Dict[str, Any]) -> str:
    labels = ((image_inspect or {}).get("Config") or {}).get("Labels") or {}
    return str(labels.get(LABEL_VERSION) or "")


def image_digest(image_inspect: Dict[str, Any], ref: str = "") -> str:
    """Registry digest of an image (``sha256:...``) or, for a local-only build,
    its image id so the history still identifies it."""
    from ..agentupdate import _running_digest, _split_ref

    dig = _running_digest(image_inspect or {}, _split_ref(ref)[1] if ref else "")
    return dig or str((image_inspect or {}).get("Id") or "")


def user_env(target_inspect: Dict[str, Any], image_inspect: Dict[str, Any]) -> List[Tuple[str, str]]:
    img_env = set(((image_inspect or {}).get("Config") or {}).get("Env") or [])
    out: List[Tuple[str, str]] = []
    for e in ((target_inspect.get("Config") or {}).get("Env") or []):
        if e in img_env or "=" not in e:
            continue
        k, _, v = e.partition("=")
        out.append((k, v))
    return out


def data_mounts(target_inspect: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The target's mounts other than the Docker socket, as VolumeMapping kwargs."""
    out = []
    for m in target_inspect.get("Mounts") or []:
        dest = str(m.get("Destination") or "")
        if not dest or dest == DOCKER_SOCK:
            continue
        src = str(m.get("Name") or "") if m.get("Type") == "volume" else str(m.get("Source") or "")
        if not src:
            continue
        out.append({"source": src, "target": dest, "read_only": m.get("RW") is False})
    return out


def socket_source(target_inspect: Dict[str, Any]) -> str:
    for m in target_inspect.get("Mounts") or []:
        if str(m.get("Destination") or "") == DOCKER_SOCK:
            return str(m.get("Source") or DOCKER_SOCK)
    return ""


def path_on_mount(target_inspect: Dict[str, Any], path: str) -> bool:
    path = os.path.normpath(path)
    for m in target_inspect.get("Mounts") or []:
        dest = os.path.normpath(str(m.get("Destination") or ""))
        if dest and dest != "/" and (path == dest or path.startswith(dest + "/")):
            return True
    return False


async def drain_pull(stream: Any) -> str:
    """Consume a pull progress stream; return the first error message, or ''."""
    err = ""
    buf = b""
    try:
        async for chunk in stream:
            if not chunk:
                continue
            buf += chunk if isinstance(chunk, (bytes, bytearray)) else str(chunk).encode()
            *lines, buf = buf.split(b"\n")
            for line in lines:
                err = err or _pull_line_error(line)
        if buf:
            err = err or _pull_line_error(buf)
    finally:
        close = getattr(stream, "close", None)
        if close is not None:
            res = close()
            if hasattr(res, "__await__"):
                await res
    return err


def _pull_line_error(line: bytes) -> str:
    line = line.strip()
    if not line.startswith(b"{"):
        return ""
    try:
        doc = json.loads(line)
    except ValueError:
        return ""
    if isinstance(doc, dict):
        if doc.get("error"):
            return str(doc["error"])
        detail = doc.get("errorDetail") or {}
        if isinstance(detail, dict) and detail.get("message"):
            return str(detail["message"])
    return ""


async def read_logs(ops: Any, ref: str, tail: int = 0) -> str:
    stream = await ops.container_logs(ref, False, tail)
    parts: List[bytes] = []
    try:
        async for chunk in stream:
            if chunk:
                parts.append(chunk if isinstance(chunk, (bytes, bytearray)) else str(chunk).encode())
    finally:
        close = getattr(stream, "close", None)
        if close is not None:
            res = close()
            if hasattr(res, "__await__"):
                await res
    return b"".join(parts).decode("utf-8", "replace")


class Target:
    """The container being updated, as discovered on its daemon."""

    def __init__(self, component: str, host: Any, ops: Any, inspect: Dict[str, Any],
                 image_inspect: Dict[str, Any]) -> None:
        self.component = component
        self.host = host
        self.host_id: Optional[str] = None if component == COMPONENT_SERVER else getattr(host, "id", None)
        self.ops = ops
        self.inspect = inspect
        self.image_inspect = image_inspect or {}
        self.name = str(inspect.get("Name") or "").lstrip("/")
        self.image_ref = str((inspect.get("Config") or {}).get("Image") or "")
        self.image_id = str(inspect.get("Image") or "")

    @property
    def version(self) -> str:
        return image_version(self.image_inspect)

    @property
    def digest(self) -> str:
        return image_digest(self.image_inspect, self.image_ref)


def _is_aux_container(name: str, labels: Dict[str, str]) -> bool:
    if (labels or {}).get(LABEL_ROLE):
        return True
    return any(s in name for s in _SUFFIXES)


class UpdateManager:
    """Owns update lifecycles for the server and every agent."""

    def __init__(self, svc: Any, *, helper_wait: float = 3.0, poll: float = 1.0,
                 preflight_timeout: float = PREFLIGHT_TIMEOUT) -> None:
        self.svc = svc
        self.helper_wait = helper_wait
        self.poll = poll
        self.preflight_timeout = preflight_timeout
        self._tasks: Dict[str, asyncio.Task] = {}
        # Ids whose record the orchestrator still owns (before/while launching
        # the helper, or queued in a batch). The reconciler leaves them alone.
        self._busy: set = set()
        self._latest: Dict[str, Tuple[float, str, str]] = {}
        self._mutex = asyncio.Lock()

    # ------------------------------------------------------------ plumbing --
    @property
    def store(self) -> Any:
        return self.svc.store

    def live(self, update_id: str) -> bool:
        return update_id in self._busy

    def local_ops(self) -> Optional[Any]:
        from ..api.hosts import LocalHostID

        try:
            host = self.store.get_host(LocalHostID)
            return self.svc.docker.ops(host)
        except Exception as err:  # noqa: BLE001
            log.debug("updates: local ops unavailable: %s", err)
            return None

    def host_ops(self, host: Any) -> Any:
        return self.svc.docker.ops(host)

    def host_reachable(self, host: Any) -> bool:
        """Whether an agent host has a live tunnel session (edge or stoa)."""
        hub = getattr(self.svc, "hub", None)
        if hub is None:
            return False
        try:
            return bool(hub.connected(host.id))
        except Exception:  # noqa: BLE001
            return False

    def data_dir(self) -> str:
        return os.path.abspath(getattr(self.svc.config, "data_dir", "./data") or "./data")

    # ----------------------------------------------------------- discovery --
    async def _find_server(self, ops: Any) -> Optional[Dict[str, Any]]:
        candidates = []
        override = os.environ.get("KOWAN_SERVER_CONTAINER", "").strip()
        if override:
            candidates.append(override)
        hostname = os.environ.get("HOSTNAME", "").strip()
        if hostname:
            candidates.append(hostname)
        candidates.append("kowan-server")
        for ref in candidates:
            try:
                insp = await ops.inspect_container(ref)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(insp, dict) and insp.get("Id"):
                name = str(insp.get("Name") or "").lstrip("/")
                if not _is_aux_container(name, (insp.get("Config") or {}).get("Labels") or {}):
                    return insp
        return None

    async def _find_agent(self, ops: Any, host: Any = None) -> Optional[Dict[str, Any]]:
        """Locate THIS host's agent container on its daemon.

        Several agents can share a daemon (tests, co-located edge agents), so the
        decisive signal is the host's own token in the container env
        (``KOWAN_AGENT_TOKEN``). Without a token match it falls back to the
        conventional ``kowan-agent`` name, else to the single container that looks
        like an agent (``kowan.component=agent`` label or the agent image repo).
        """
        from ..agentupdate import _agent_image_default, _split_ref

        token = str(getattr(host, "token", "") or "")
        want = f"KOWAN_AGENT_TOKEN={token}" if token else ""

        def _has_token(insp: Dict[str, Any]) -> bool:
            return bool(want) and want in ((insp.get("Config") or {}).get("Env") or [])

        named: Optional[Dict[str, Any]] = None
        try:
            insp = await ops.inspect_container("kowan-agent")
            if isinstance(insp, dict) and insp.get("Id"):
                if _has_token(insp):
                    return insp
                named = insp
        except Exception:  # noqa: BLE001
            pass
        try:
            containers = await ops.list_containers(True)
        except Exception:  # noqa: BLE001
            return named
        repo = _split_ref(_agent_image_default())[1]
        likely, others = [], []
        for c in containers:
            name = ((getattr(c, "names", None) or [""])[0] or "").lstrip("/")
            labels = getattr(c, "labels", None) or {}
            if _is_aux_container(name, labels) or name == "kowan-agent":
                continue
            img = getattr(c, "image", "") or ""
            if labels.get("kowan.component") == "agent" or (repo and _split_ref(img)[1] == repo):
                likely.append(c)
            elif getattr(c, "state", "") == "running":
                others.append(c)
        likely_insp: List[Dict[str, Any]] = []
        for c in likely + (others[:100] if want else []):
            try:
                insp = await ops.inspect_container(getattr(c, "id", ""))
            except Exception:  # noqa: BLE001
                continue
            if _has_token(insp):
                return insp
            if c in likely:
                likely_insp.append(insp)
        if named is not None:
            return named
        return likely_insp[0] if len(likely_insp) == 1 else None

    async def resolve_target(self, component: str, host: Any = None) -> Tuple[Optional[Target], str]:
        """Return ``(target, reason)``; ``target`` is None when unsupported."""
        if component == COMPONENT_SERVER:
            ops = self.local_ops()
            if ops is None:
                return None, "the local Docker daemon is not available to the server"
            try:
                await asyncio.wait_for(ops.ping(), timeout=5)
            except Exception as err:  # noqa: BLE001
                return None, f"the Docker socket is not reachable from the server ({err}); mount /var/run/docker.sock"
            insp = await self._find_server(ops)
            if insp is None:
                return None, ("the server is not running in a container this server can find "
                              "(running from source, or set KOWAN_SERVER_CONTAINER to its container name)")
        else:
            from ..models.host import HostTypeAgent

            if host is None or getattr(host, "type", "") != HostTypeAgent:
                return None, "not an agent host"
            if getattr(self.svc, "hub", None) is not None and not self.host_reachable(host):
                return None, "the agent is not connected"
            try:
                ops = self.host_ops(host)
                insp = await asyncio.wait_for(self._find_agent(ops, host), timeout=30)
            except asyncio.TimeoutError:
                return None, "agent did not answer"
            except Exception as err:  # noqa: BLE001
                return None, f"agent unreachable: {err}"
            if insp is None:
                return None, "agent container not found on the host (is the agent running in Docker?)"
        if not socket_source(insp):
            return None, f"container {str(insp.get('Name') or '').lstrip('/')} has no Docker socket mount"
        if component == COMPONENT_SERVER:
            dd = self.data_dir()
            if not path_on_mount(insp, dd):
                return None, (f"the data directory {dd} is not on a volume or bind mount; "
                              "an update would lose the database")
        try:
            img = await ops.inspect_image(str(insp.get("Image") or ""))
        except Exception:  # noqa: BLE001
            img = {}
        return Target(component, host, ops, insp, img), ""

    # -------------------------------------------------------------- status --
    async def _latest_digest(self, image_ref: str, refresh: bool = False) -> Tuple[str, str]:
        from .. import agentupdate

        now = time.monotonic()
        hit = self._latest.get(image_ref)
        if hit and hit[0] > now and not refresh:
            return hit[1], hit[2]
        try:
            dig, err = await asyncio.wait_for(agentupdate._remote_digest(self.store, image_ref), 15), ""
        except Exception as err2:  # noqa: BLE001
            dig, err = "", f"check registry: {err2}"
        self._latest[image_ref] = (now + (LATEST_CACHE_TTL if not err else 60.0), dig, err)
        return dig, err

    async def previous_info(self, target: Target) -> Optional[Dict[str, str]]:
        try:
            prev = await target.ops.inspect_container(previous_name(target.name))
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(prev, dict) or not prev.get("Id"):
            return None
        ref = str((prev.get("Config") or {}).get("Image") or "")
        try:
            img = await target.ops.inspect_image(str(prev.get("Image") or ref))
        except Exception:  # noqa: BLE001
            img = {"Id": prev.get("Image") or ""}
        return {"image": ref, "version": image_version(img), "digest": image_digest(img, ref)}

    def _records(self, component: str, host_id: Optional[str]) -> Tuple[Optional[dict], Optional[dict]]:
        in_progress = None
        last = None
        try:
            for rec in self.store.list_updates(component, host_id, 10):
                if rec["status"] == STATUS_RUNNING:
                    in_progress = in_progress or rec
                elif last is None:
                    last = rec
        except Exception as err:  # noqa: BLE001
            log.debug("updates: history unavailable: %s", err)
        return in_progress, last

    async def status(self, component: str, host: Any = None, refresh: bool = False) -> Dict[str, Any]:
        """The update state of the server or an agent. Besides digests (what
        the self-update swaps), it carries the release versions: the running
        one and the newest published (``kowan.releases``), known even when
        self-update is not possible, e.g. a server run from source.
        ``refresh`` skips the registry caches (the panel's Check button)."""
        host_id = None if component == COMPONENT_SERVER else getattr(host, "id", None)
        in_progress, last = self._records(component, host_id)
        res: Dict[str, Any] = {
            "supported": False, "reason": "", "error": "", "image": "", "container": "",
            "version": "", "running": "", "latest": "", "updateAvailable": False,
            "canRollback": False, "previous": None, "inProgress": in_progress, "last": last,
            "currentVersion": "", "latestVersion": "", "newVersion": False, "releaseError": "",
        }
        releases = getattr(self.svc, "releases", None)
        if releases is not None:
            res.update(await releases.status(refresh))
        if component != COMPONENT_SERVER:
            res["currentVersion"] = ""  # the agent's comes from its image label below
        target, reason = await self.resolve_target(component, host)
        if target is None:
            res["reason"] = reason
            res["error"] = reason
            res["newVersion"] = res["newVersion"] and component == COMPONENT_SERVER
            return res
        if component != COMPONENT_SERVER:
            from ..releases import is_newer

            res["currentVersion"] = target.version
            res["newVersion"] = is_newer(res["latestVersion"], target.version)
        res.update({
            "supported": True, "image": target.image_ref, "container": target.name,
            "version": target.version, "running": target.digest,
        })
        res["previous"] = await self.previous_info(target)
        res["canRollback"] = res["previous"] is not None and in_progress is None
        if target.image_ref and not target.image_ref.startswith("sha256:"):
            latest, err = await self._latest_digest(target.image_ref, refresh)
            res["latest"] = latest
            if err:
                res["error"] = err
            from ..agentupdate import _running_digest, _split_ref

            running_repo = _running_digest(target.image_inspect, _split_ref(target.image_ref)[1])
            if latest and running_repo:
                res["updateAvailable"] = latest != running_repo
        res["updateAvailable"] = res["updateAvailable"] or res["newVersion"]
        return res

    # --------------------------------------------------------------- start --
    def _new_record(self, target: Optional[Target], component: str, host_id: Optional[str], mode: str,
                    image: str, user: str, batch_id: str = "") -> Dict[str, Any]:
        rec = {
            "id": uuid.uuid4().hex[:12] + uuid.uuid4().hex[:4],
            "component": component, "hostId": host_id, "mode": mode, "status": STATUS_RUNNING,
            "fromImage": target.image_ref if target else "",
            "fromDigest": target.digest if target else "",
            "fromVersion": target.version if target else "",
            "toImage": image or "", "toDigest": "", "toVersion": "",
            "phases": [], "error": "", "logs": [], "startedAt": now_rfc3339(), "finishedAt": "",
            "startedBy": user or "", "batchId": batch_id,
        }
        return rec

    def _phase(self, rec: Dict[str, Any], phase: str, ok: bool, detail: str = "", save: bool = True) -> None:
        rec["phases"].append({"phase": phase, "ok": bool(ok), "at": now_rfc3339(), "detail": detail})
        if save:
            try:
                self.store.save_update(rec)
            except Exception as err:  # noqa: BLE001
                log.warning("updates: save %s: %s", rec["id"], err)

    def _finish(self, rec: Dict[str, Any], status: str, error: str = "") -> None:
        rec["status"] = status
        rec["error"] = error
        rec["finishedAt"] = now_rfc3339()
        try:
            self.store.save_update(rec)
            self.store.prune_updates(rec["component"], rec.get("hostId"), 50)
        except Exception as err:  # noqa: BLE001
            log.warning("updates: finish %s: %s", rec["id"], err)

    async def start(self, component: str, host: Any = None, *, mode: str = MODE_UPDATE,
                    image: str = "", user: str = "", restore_db: bool = True) -> str:
        """Validate, take the lock and start the update in the background. Returns the id."""
        if mode not in (MODE_UPDATE, MODE_ROLLBACK):
            raise UpdateError("mode must be update or rollback")
        image = (image or "").strip()
        if image and (any(c.isspace() for c in image) or len(image) > 512):
            raise UpdateError("invalid image reference")
        target, reason = await self.resolve_target(component, host)
        if target is None:
            raise UpdateError(reason)
        if mode == MODE_ROLLBACK and await self.previous_info(target) is None:
            raise UpdateError(f"nothing to roll back to: no {previous_name(target.name)} container", 409)
        rec = self._new_record(target, component, target.host_id, mode, image, user)
        rec["phases"].append({"phase": "lock", "ok": True, "at": rec["startedAt"],
                              "detail": f"{component} {target.name}"})
        async with self._mutex:
            blocking = self.store.acquire_update(rec, _ts_minus(LOCK_STALE_SECONDS))
        if blocking is not None:
            raise UpdateError(f"an update of this {component} is already running ({blocking['id']})", 409)
        self._spawn(rec, target, restore_db)
        return rec["id"]

    def _spawn(self, rec: Dict[str, Any], target: Target, restore_db: bool = True) -> asyncio.Task:
        self._busy.add(rec["id"])
        task = asyncio.ensure_future(self._drive(rec, target, restore_db))
        self._tasks[rec["id"]] = task
        task.add_done_callback(lambda _t, i=rec["id"]: self._tasks.pop(i, None))
        return task

    # --------------------------------------------------------------- drive --
    async def _drive(self, rec: Dict[str, Any], target: Target, restore_db: bool = True) -> None:
        self._busy.add(rec["id"])
        try:
            await self._drive_inner(rec, target, restore_db)
        finally:
            self._busy.discard(rec["id"])

    def _drop_snapshot(self, rec: Dict[str, Any]) -> None:
        """An update that never reached the swap does not need its snapshot."""
        if rec.get("component") != COMPONENT_SERVER:
            return
        snap = dbsnap.snapshot_path(self.data_dir(), rec["id"])
        for p in [snap] + [snap.with_name(snap.name[:-3] + "." + k) for k in dbsnap.KEY_FILES]:
            try:
                p.unlink()
            except OSError:
                pass

    async def _drive_inner(self, rec: Dict[str, Any], target: Target, restore_db: bool) -> None:
        try:
            plan = await self._prepare(rec, target, restore_db)
        except _Abort as ab:
            self._drop_snapshot(rec)
            self._finish(rec, STATUS_ABORTED, str(ab))
            return
        except asyncio.CancelledError:
            self._finish(rec, STATUS_ABORTED, "cancelled before the helper was launched")
            raise
        except Exception as err:  # noqa: BLE001
            log.exception("updates: %s prepare failed", rec["id"])
            self._drop_snapshot(rec)
            self._finish(rec, STATUS_ABORTED, f"unexpected error before the swap: {err}")
            return
        try:
            await self._launch(rec, target, plan)
        except Exception as err:  # noqa: BLE001
            self._drop_snapshot(rec)
            self._phase(rec, "launch", False, str(err), save=False)
            self._finish(rec, STATUS_FAILED, f"helper could not be started (nothing was changed): {err}")

    async def _pull(self, rec: Dict[str, Any], target: Target, ref: str) -> Dict[str, Any]:
        from ..registry import docker_auth_for

        ops = target.ops
        pull_err = ""
        if not ref.startswith("sha256:"):
            try:
                auth = await docker_auth_for(self.store, ref)
            except Exception:  # noqa: BLE001
                auth = ""
            try:
                stream = await ops.pull_image(ref, auth)
                pull_err = await drain_pull(stream)
            except Exception as err:  # noqa: BLE001
                pull_err = str(err)
        try:
            img = await ops.inspect_image(ref)
        except Exception as err:  # noqa: BLE001
            self._phase(rec, "pull", False, pull_err or str(err))
            raise _Abort(f"pull {ref} failed: {pull_err or err}")
        if pull_err:
            self._phase(rec, "pull", True, f"registry pull failed ({pull_err}); using the image present on the host")
        else:
            self._phase(rec, "pull", True, f"{ref} {str(img.get('Id') or '')[:19]}")
        return img

    async def _check_arch(self, rec: Dict[str, Any], target: Target, img: Dict[str, Any]) -> None:
        img_arch = norm_arch(str(img.get("Architecture") or ""))
        img_os = str(img.get("Os") or "").lower()
        try:
            info = await target.ops.system_info()
            host_arch = norm_arch(getattr(info, "arch", "") or "")
        except Exception:  # noqa: BLE001
            host_arch = ""
        if img_os and img_os != "linux":
            self._phase(rec, "platform", False, f"image OS {img_os}")
            raise _Abort(f"image is built for {img_os}, not linux")
        if img_arch and host_arch and img_arch != host_arch:
            self._phase(rec, "platform", False, f"image {img_arch}, host {host_arch}")
            raise _Abort(f"image architecture {img_arch} does not match the host ({host_arch})")
        self._phase(rec, "platform", True, f"{img_os or '?'}/{img_arch or '?'} on {host_arch or '?'}")

    async def _prepare(self, rec: Dict[str, Any], target: Target, restore_db: bool) -> Dict[str, Any]:
        mode = rec["mode"]
        plan: Dict[str, Any] = {
            "id": rec["id"], "component": target.component, "mode": mode,
            "target": target.name, "previous": previous_name(target.name),
            "waitSeconds": self.helper_wait,
        }
        if mode == MODE_UPDATE:
            ref = rec.get("toImage") or target.image_ref
            if not ref:
                raise _Abort("cannot tell which image the target runs; pass an explicit image")
            rec["toImage"] = ref
            self._phase(rec, "resolve", True, ref)
            img = await self._pull(rec, target, ref)
            await self._check_arch(rec, target, img)
            new_id = str(img.get("Id") or "")
            rec["toDigest"] = image_digest(img, ref)
            rec["toVersion"] = image_version(img)
            if new_id and new_id == target.image_id:
                self._phase(rec, "compare", False, "the target already runs this image")
                raise _Abort("already up to date: the target already runs this image")
            plan["image"] = ref
            plan["imageId"] = new_id
        else:
            prev = await self.previous_info(target)
            if prev is None:
                raise _Abort("no previous container to roll back to")
            rec["toImage"], rec["toDigest"], rec["toVersion"] = prev["image"], prev["digest"], prev["version"]
            self._phase(rec, "resolve", True, f"roll back to {prev['image']} {prev['version']}".strip())

        snapshot = ""
        if target.component == COMPONENT_SERVER:
            snapshot = await self._snapshot(rec, plan, restore_db)
        if mode == MODE_UPDATE:
            await self._preflight(rec, target, plan["imageId"] or plan["image"], snapshot)
        plan["record"] = {k: rec.get(k) for k in (
            "hostId", "startedAt", "startedBy", "batchId", "fromImage", "fromDigest", "fromVersion",
            "toImage", "toDigest", "toVersion")}
        plan["record"]["phases"] = list(rec["phases"])
        return plan

    async def _snapshot(self, rec: Dict[str, Any], plan: Dict[str, Any], restore_db: bool) -> str:
        data_dir = self.data_dir()
        db_path = os.path.join(data_dir, "kowan.db")
        try:
            snap = await asyncio.to_thread(dbsnap.take_snapshot, db_path, data_dir, rec["id"])
        except Exception as err:  # noqa: BLE001
            self._phase(rec, "snapshot", False, str(err))
            raise _Abort(f"database snapshot failed: {err}")
        db: Dict[str, Any] = {"path": db_path, "backupDir": str(dbsnap.backups_dir(data_dir)),
                              "rollbackRestore": snap, "swapRestore": None}
        detail = f"{snap}"
        if rec["mode"] == MODE_ROLLBACK:
            if restore_db:
                producer = self.store.last_update(COMPONENT_SERVER, None, [STATUS_UPDATED])
                cand = str(dbsnap.snapshot_path(data_dir, producer["id"])) if producer else ""
                if cand and os.path.isfile(cand):
                    db["swapRestore"] = cand
                    detail += f"; previous version's database: {cand}"
                else:
                    detail += "; no snapshot of the previous version's database, keeping the current one"
            else:
                detail += "; database restore disabled for this rollback"
        plan["db"] = db
        self._phase(rec, "snapshot", True, detail)
        return snap

    async def _preflight(self, rec: Dict[str, Any], target: Target, image: str, snapshot: str) -> None:
        from ..models.deployment import ContainerSpec, KeyValue, VolumeMapping

        comp = target.component
        cmd = ["preflight"]
        if comp == COMPONENT_SERVER and snapshot:
            copy = await asyncio.to_thread(dbsnap.copy_for_preflight, snapshot, self.data_dir(), rec["id"])
            cmd += ["--db", copy]
        else:
            copy = ""
        spec = ContainerSpec(
            image=image,
            entrypoint=["python", "-m", f"kowan.{comp}"],
            command=cmd,
            env=[KeyValue(key=k, value=v) for k, v in user_env(target.inspect, target.image_inspect)],
            volumes=[VolumeMapping(source=m["source"], target=m["target"], read_only=True)
                     for m in data_mounts(target.inspect)],
            restart_policy="no",
            labels={LABEL_ROLE: f"{comp}-preflight", LABEL_UPDATE_ID: rec["id"]},
        )
        name = preflight_name(comp, rec["id"])
        ops = target.ops
        cid = ""
        try:
            try:
                cid = await ops.create_container(name, spec, True)
            except Exception as err:  # noqa: BLE001
                self._phase(rec, "preflight", False, f"could not start: {err}")
                raise _Abort(f"preflight could not start: {err}")
            deadline = time.monotonic() + self.preflight_timeout
            state: Dict[str, Any] = {}
            while True:
                insp = await ops.inspect_container(cid)
                state = insp.get("State") or {}
                if state.get("Status") in ("exited", "dead"):
                    break
                if time.monotonic() >= deadline:
                    self._phase(rec, "preflight", False, f"timed out after {self.preflight_timeout:.0f}s")
                    raise _Abort(f"preflight timed out after {self.preflight_timeout:.0f}s")
                await asyncio.sleep(self.poll)
            code = int(state.get("ExitCode") or 0)
            try:
                text = await read_logs(ops, cid, 200)
            except Exception as err:  # noqa: BLE001
                text = f"(logs unavailable: {err})"
            summary = _preflight_summary(text)
            if code != 0:
                rec["logs"] = [ln for ln in text.splitlines()][-200:]
                self._phase(rec, "preflight", False, f"exit {code}: {summary}")
                raise _Abort(f"preflight of the new image failed (exit {code}): {summary}")
            self._phase(rec, "preflight", True, summary or "ok")
        finally:
            if cid:
                try:
                    await ops.remove_container(cid, True)
                except Exception:  # noqa: BLE001
                    pass
            if copy:
                try:
                    os.remove(copy)
                except OSError:
                    pass

    async def _launch(self, rec: Dict[str, Any], target: Target, plan: Dict[str, Any]) -> None:
        from ..models.deployment import ContainerSpec, VolumeMapping

        comp = target.component
        volumes = [VolumeMapping(source=socket_source(target.inspect), target=DOCKER_SOCK)]
        if comp == COMPONENT_SERVER:
            volumes += [VolumeMapping(source=m["source"], target=m["target"], read_only=m["read_only"])
                        for m in data_mounts(target.inspect)]
        # Record the launch BEFORE creating the helper: once the helper runs, the
        # server (or the agent's tunnel) may disappear at any moment.
        plan["record"]["phases"] = list(rec["phases"]) + [
            {"phase": "launch", "ok": True, "at": now_rfc3339(), "detail": helper_name(comp, rec["id"])}]
        token = encode_plan(plan)
        spec = ContainerSpec(
            image=target.image_id or target.image_ref,
            entrypoint=["python", "-m", f"kowan.{comp}"],
            command=["self-update", "--plan", token],
            volumes=volumes,
            restart_policy="no",
            labels={LABEL_ROLE: f"{comp}-updater", LABEL_UPDATE_ID: rec["id"]},
        )
        rec["phases"] = plan["record"]["phases"]
        self.store.save_update(rec)
        cid = await target.ops.create_container(helper_name(comp, rec["id"]), spec, True)
        log.warning("updates: %s %s %s launched helper %s", comp, rec["mode"], rec["id"], str(cid)[:12])

    # --------------------------------------------------------------- batch --
    async def start_batch(self, hosts: List[Any], *, image: str = "", user: str = "") -> Dict[str, Any]:
        if not hosts:
            raise UpdateError("hostIds must not be empty")
        batch_id = "b" + uuid.uuid4().hex[:15]
        recs: List[Dict[str, Any]] = []
        async with self._mutex:
            for h in hosts:
                rec = self._new_record(None, COMPONENT_AGENT, h.id, MODE_UPDATE, image, user, batch_id)
                rec["phases"].append({"phase": "queued", "ok": True, "at": rec["startedAt"],
                                      "detail": f"batch {batch_id}, position {len(recs) + 1}/{len(hosts)}"})
                blocking = self.store.acquire_update(rec, _ts_minus(LOCK_STALE_SECONDS))
                if blocking is not None:
                    for r in recs:
                        self._finish(r, STATUS_ABORTED, "batch not started: another host was busy")
                    raise UpdateError(f"host {getattr(h, 'name', h.id)} already has an update running "
                                      f"({blocking['id']})", 409)
                recs.append(rec)
        for r in recs:
            self._busy.add(r["id"])
        task = asyncio.ensure_future(self._run_batch(list(zip(hosts, recs))))
        self._tasks[batch_id] = task

        def _done(_t: asyncio.Task, ids=[r["id"] for r in recs]) -> None:
            self._tasks.pop(batch_id, None)
            for i in ids:
                self._busy.discard(i)

        task.add_done_callback(_done)
        return {"batchId": batch_id, "ids": [r["id"] for r in recs]}

    async def wait_final(self, update_id: str, timeout: float, interval: float = 3.0) -> Dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            rec = self.store.get_update(update_id)
            if rec["status"] != STATUS_RUNNING or time.monotonic() >= deadline:
                return rec
            await asyncio.sleep(interval)

    async def _run_batch(self, pairs: List[Tuple[Any, Dict[str, Any]]]) -> None:
        stopped_by = ""
        for i, (host, rec) in enumerate(pairs):
            if stopped_by:
                self._finish(rec, STATUS_ABORTED, f"rolling update stopped: {stopped_by}")
                self._busy.discard(rec["id"])
                continue
            try:
                target, reason = await self.resolve_target(COMPONENT_AGENT, host)
            except Exception as err:  # noqa: BLE001
                target, reason = None, str(err)
            if target is None:
                self._phase(rec, "lock", False, reason, save=False)
                self._finish(rec, STATUS_ABORTED, reason)
                self._busy.discard(rec["id"])
                stopped_by = f"host {getattr(host, 'name', host.id)}: {reason}"
                continue
            rec.update({"fromImage": target.image_ref, "fromDigest": target.digest,
                        "fromVersion": target.version})
            self._phase(rec, "lock", True, f"agent {target.name}")
            await self._drive(rec, target)
            final = await self.wait_final(rec["id"], BATCH_STEP_TIMEOUT)
            if final["status"] != STATUS_UPDATED:
                stopped_by = (f"host {getattr(host, 'name', host.id)} ended as {final['status']}"
                              + (f" ({final.get('error')})" if final.get("error") else ""))


def _preflight_summary(text: str) -> str:
    """The preflight's JSON summary line if present, else the last log line."""
    last = ""
    for raw in reversed((text or "").splitlines()):
        line = raw.strip()
        if not line:
            continue
        head, sep, rest = line.partition(" ")
        if line[0].isdigit() and sep and "T" in head:
            line = rest.strip()
        if line.startswith("{"):
            try:
                doc = json.loads(line)
                if isinstance(doc, dict) and "ok" in doc:
                    if doc.get("ok"):
                        return "; ".join(str(c.get("name")) for c in doc.get("checks") or [] if c.get("ok"))
                    bad = [f"{c.get('name')}: {c.get('error')}" for c in doc.get("checks") or [] if not c.get("ok")]
                    return "; ".join(bad) or str(doc.get("error") or "failed")
            except ValueError:
                pass
        last = last or line
    return last[-300:]
