"""ScanService: run Trivy on a host through its Docker Ops and store results.

Flow of :meth:`ScanService.scan` for ``(host, image)``:

  1. ``inspect_image`` resolves the image id (the "digest" results are keyed
     by) and its tags/repo digests;
  2. a stored ``ok`` result for that id younger than ``FRESH_FOR`` (24 h) is
     returned as-is unless ``force``;
  3. otherwise one scan runs (concurrent callers for the same host+image share
     it): make sure the Trivy image is present (pull once), create + start the
     scan container, poll ``inspect_container`` until it exits, read stdout
     from its logs, remove it, parse the report and upsert the row.

Only Ops methods every client implements are used (create/inspect/logs/remove,
pull/inspect image), so it works the same against the local socket and
against a remote agent over the tunnel. ``clock``/``sleep`` are injectable for
tests.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from . import trivy

log = logging.getLogger("kowan.scan")

FRESH_FOR = timedelta(hours=24)
SCAN_TIMEOUT = 20 * 60.0  # seconds; first run downloads the DB
POLL_EVERY = 2.0
MAX_LOG_BYTES = 64 << 20
# At most this many scans run at once per host (Trivy is CPU/RAM hungry).
PER_HOST_CONCURRENCY = 1


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _rfc3339(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def is_fresh(rec: Optional[Dict[str, Any]], now: Optional[datetime] = None, fresh_for: timedelta = FRESH_FOR) -> bool:
    """A stored result can be re-used: status ok and scanned within ``fresh_for``."""
    if not rec or rec.get("status") != "ok":
        return False
    t = _parse_time(rec.get("scannedAt", ""))
    if t is None:
        return False
    return (now or _now()) - t < fresh_for


async def _drain(stream: Any, limit: int = MAX_LOG_BYTES) -> bytes:
    buf = bytearray()
    try:
        async for chunk in stream:
            buf += chunk if isinstance(chunk, (bytes, bytearray)) else str(chunk).encode()
            if len(buf) > limit:
                raise ValueError("trivy output too large")
    finally:
        close = getattr(stream, "close", None)
        if close is not None:
            res = close()
            if hasattr(res, "__await__"):
                await res
    return bytes(buf)


def pick_target(image_insp: Dict[str, Any], requested: str) -> str:
    """What to hand Trivy: a tag of the image when it has one (Trivy resolves
    names more reliably than bare ids), else the image id."""
    tags = [t for t in (image_insp.get("RepoTags") or []) if t and t != "<none>:<none>"]
    if requested in tags:
        return requested
    if tags:
        return tags[0]
    return str(image_insp.get("Id") or requested)


class ScanService:
    def __init__(
        self,
        svc: Any,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        timeout: float = SCAN_TIMEOUT,
        poll: float = POLL_EVERY,
    ) -> None:
        self.svc = svc
        self._clock = clock
        self._sleep = sleep
        self.timeout = timeout
        self.poll = poll
        self._inflight: Dict[Tuple[str, str], "asyncio.Future[Dict[str, Any]]"] = {}
        self._host_sems: Dict[str, asyncio.Semaphore] = {}
        # (host, image id) -> started_at, for the UI's "scanning..." state.
        self.running: Dict[Tuple[str, str], str] = {}

    @property
    def store(self) -> Any:
        return self.svc.store

    def ops_for(self, host_id: str) -> Any:
        host = self.store.get_host(host_id)
        return self.svc.docker.ops(host)

    def is_running(self, host_id: str, image_id: str) -> bool:
        return (host_id, image_id) in self.running

    async def resolve_image(self, ops: Any, image: str) -> Dict[str, Any]:
        insp = await ops.inspect_image(image)
        if not isinstance(insp, dict) or not insp.get("Id"):
            raise ValueError(f"image {image!r} not found on host")
        return insp

    async def scan(
        self, host_id: str, image: str, *, force: bool = False, ops: Any = None,
        image_insp: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Scan (or re-use a fresh scan of) ``image`` on ``host_id``; returns the
        stored record (with findings). Errors are recorded as a ``status=error``
        row and re-raised as ``RuntimeError``."""
        ops = ops if ops is not None else self.ops_for(host_id)
        insp = image_insp or await self.resolve_image(ops, image)
        image_id = str(insp["Id"])
        if not force:
            cached = self.store.get_image_scan(image_id)
            if is_fresh(cached):
                return cached  # type: ignore[return-value]
        key = (host_id, image_id)
        pending = self._inflight.get(key)
        if pending is not None:
            return await asyncio.shield(pending)
        fut: "asyncio.Future[Dict[str, Any]]" = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        self.running[key] = _rfc3339(_now())
        try:
            try:
                rec = await self._run(host_id, ops, insp, image)
                fut.set_result(rec)
            except BaseException as err:  # noqa: BLE001
                fut.set_exception(err)
                # Nobody else may be awaiting; mark retrieved to avoid warnings.
                fut.exception()
                raise
            return rec
        finally:
            self._inflight.pop(key, None)
            self.running.pop(key, None)

    def start_background(self, host_id: str, image: str, *, force: bool = False) -> "asyncio.Task[Any]":
        """Fire-and-forget scan (the API returns immediately, the UI polls)."""
        async def run() -> None:
            try:
                await self.scan(host_id, image, force=force)
            except Exception as err:  # noqa: BLE001 - recorded in the row
                log.warning("scan %s on %s failed: %s", image, host_id, err)
        return asyncio.ensure_future(run())

    async def _ensure_scanner(self, ops: Any, image: str) -> None:
        try:
            await ops.inspect_image(image)
            return
        except Exception:  # noqa: BLE001 - not present: pull it
            pass
        from kowan.api.updates import _drain_pull

        await _drain_pull(await ops.pull_image(image, ""))

    async def _run(self, host_id: str, ops: Any, insp: Dict[str, Any], requested: str) -> Dict[str, Any]:
        sem = self._host_sems.setdefault(host_id, asyncio.Semaphore(PER_HOST_CONCURRENCY))
        image_id = str(insp["Id"])
        target = pick_target(insp, requested)
        started = self._clock()
        rec: Dict[str, Any] = {
            "imageId": image_id,
            "imageRef": target if not target.startswith("sha256:") else "",
            "repoDigests": list(insp.get("RepoDigests") or []),
            "hostId": host_id,
            "scanner": "trivy",
            "scannerVersion": trivy.scanner_version_from_image(trivy.trivy_image()),
            "scannedAt": _rfc3339(_now()),
        }
        async with sem:
            try:
                raw = await self._run_container(ops, target)
                summary = trivy.parse_trivy_report(trivy.extract_json(raw))
            except Exception as err:  # noqa: BLE001
                rec.update(status="error", error=str(err) or err.__class__.__name__,
                           durationMs=int((self._clock() - started) * 1000))
                try:
                    old = self.store.get_image_scan(image_id, with_findings=False)
                    # Never overwrite a good result with a failed re-scan.
                    if not old or old.get("status") != "ok":
                        self.store.upsert_image_scan(rec)
                except Exception:  # noqa: BLE001
                    pass
                raise RuntimeError(f"scan {target}: {rec['error']}") from err
        rec.update(
            status="ok", error="", os=summary.os, counts=summary.counts, total=summary.total,
            fixable=summary.fixable, findings=summary.findings,
            durationMs=int((self._clock() - started) * 1000),
        )
        if summary.scanner_version:
            rec["scannerVersion"] = summary.scanner_version
        if summary.image_id and summary.image_id != image_id:
            log.info("scan: trivy reported image id %s for %s (expected %s)", summary.image_id, target, image_id)
        self.store.upsert_image_scan(rec)
        return self.store.get_image_scan(image_id) or rec

    async def _run_container(self, ops: Any, target: str) -> bytes:
        image = trivy.trivy_image()
        await self._ensure_scanner(ops, image)
        spec = trivy.build_scan_spec(target, image=image)
        name = trivy.container_prefix() + uuid.uuid4().hex[:10]
        cid = await ops.create_container(name, spec, True)
        try:
            deadline = self._clock() + self.timeout
            state: Dict[str, Any] = {}
            while True:
                insp = await ops.inspect_container(cid) or {}
                state = insp.get("State") or {}
                if not state.get("Running") and state.get("Status") not in ("created", "restarting", "running"):
                    break
                if self._clock() > deadline:
                    raise TimeoutError(f"trivy did not finish within {int(self.timeout)}s")
                await self._sleep(self.poll)
            raw = await _drain(await ops.container_logs(cid, False, 0))
            code = int(state.get("ExitCode") or 0)
            if code != 0:
                tail = trivy.strip_log_timestamps(raw).decode("utf-8", "replace").strip().splitlines()[-3:]
                raise RuntimeError(f"trivy exited with code {code}: {' | '.join(tail)[:500]}")
            return raw
        finally:
            try:
                await ops.remove_container(cid, True)
            except Exception:  # noqa: BLE001 - best effort
                pass
