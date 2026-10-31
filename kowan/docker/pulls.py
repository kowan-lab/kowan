"""Progress of the image pulls in flight, whoever asked for them.

Every pull goes through a host's :class:`~kowan.docker.imagecache.CachingOps`:
the Pull button, an update, a deploy, the auto-updater, a scan. Its progress
stream (the Engine's NDJSON, the same over an agent) is read on its way to
whoever consumes it and folded into one summary per pull: bytes downloaded
of the known total, layers done, the last status line or the error. The
summaries go to the UI over the host's events WebSocket (``type=pull``,
:mod:`kowan.api.events`), a few times a second at most. Who may see a pull:
anyone who sees the whole host, and the user who started it (a team member
sees their own pulls; one started by Kowan itself has no user). A pull that ended
stays listed for a moment, so a page opened just after still sees how it went.

The stream is passed through untouched: tracking only reads it.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

# A summary is sent at most this often per pull (the first and last always).
EMIT_INTERVAL = 0.25
# A finished pull is still reported to new subscribers for this long.
LINGER = 15.0
# Summaries waiting for a slow subscriber; older ones are dropped, as the
# next one supersedes them anyway.
QUEUE_SIZE = 64

# Layer statuses after which the layer is fully downloaded.
_DOWNLOADED = ("Download complete", "Verifying Checksum", "Extracting")
# ...and after which it is in place.
_DONE = ("Pull complete", "Already exists")

_ids = itertools.count(1)


@dataclass
class _Layer:
    current: int = 0
    total: int = 0
    done: bool = False


@dataclass
class Pull:
    """One pull and what its stream said so far."""

    host_id: str
    ref: str
    user_id: str = ""  # who started it, "" for Kowan itself
    id: str = field(default_factory=lambda: f"pull-{next(_ids)}")
    started: float = field(default_factory=time.time)
    status: str = "pulling"  # pulling | done | error
    message: str = ""
    layers: Dict[str, _Layer] = field(default_factory=dict)
    ended: Optional[float] = None  # clock time it ended
    last_emit: float = 0.0

    def apply(self, msg: Dict[str, Any]) -> None:
        """Fold one progress line into the summary."""
        if msg.get("error"):
            self.status, self.message = "error", str(msg["error"])
            return
        status = str(msg.get("status") or "")
        lid = str(msg.get("id") or "")
        if not lid or status.startswith("Pulling from"):
            # "Digest: …", "Status: Downloaded newer image for …".
            if status:
                self.message = status.removeprefix("Status: ")
            return
        layer = self.layers.setdefault(lid, _Layer())
        detail = msg.get("progressDetail") or {}
        if status == "Downloading":
            layer.current = int(detail.get("current") or 0)
            layer.total = int(detail.get("total") or layer.total or 0)
        elif status in _DOWNLOADED:
            layer.current = layer.total
        elif status in _DONE:
            layer.current, layer.done = layer.total, True
        self.message = status

    def snapshot(self) -> Dict[str, Any]:
        sized = [lay for lay in self.layers.values() if lay.total]
        return {
            "type": "pull", "id": self.id, "hostId": self.host_id, "ref": self.ref, "status": self.status,
            "message": self.message,
            "current": sum(lay.current for lay in sized), "total": sum(lay.total for lay in sized),
            "layers": len(self.layers), "layersDone": sum(1 for lay in self.layers.values() if lay.done),
            "startedAt": int(self.started * 1000),
        }


class PullTracker:
    """The pulls in flight per host and who follows them."""

    def __init__(self, interval: float = EMIT_INTERVAL, linger: float = LINGER,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.interval = interval
        self.linger = linger
        self._clock = clock
        self._pulls: Dict[str, Pull] = {}
        # host -> {queue: the only user whose pulls it gets, or None for all}
        self._subs: Dict[str, Dict["asyncio.Queue[Dict[str, Any]]", Optional[str]]] = {}

    def track(self, host_id: str, ref: str, stream: Any, user_id: str = "") -> "TrackedStream":
        """Wrap a pull's progress stream so its progress is reported."""
        self._prune()
        pull = Pull(host_id=host_id, ref=ref, user_id=user_id)
        self._pulls[pull.id] = pull
        self._emit(pull, force=True)
        return TrackedStream(stream, self, pull)

    def active(self, host_id: str, user_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Summaries of the host's pulls, running or just ended; with
        ``user_id``, only the ones that user started."""
        self._prune()
        return [p.snapshot() for p in self._pulls.values()
                if p.host_id == host_id and (user_id is None or p.user_id == user_id)]

    def subscribe(self, host_id: str, user_id: Optional[str] = None) -> "asyncio.Queue[Dict[str, Any]]":
        """A queue of the host's pull summaries, starting with the current
        ones; with ``user_id``, only the pulls that user started."""
        q: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue(QUEUE_SIZE)
        for snap in self.active(host_id, user_id):
            q.put_nowait(snap)
        self._subs.setdefault(host_id, {})[q] = user_id
        return q

    def unsubscribe(self, host_id: str, q: "asyncio.Queue[Dict[str, Any]]") -> None:
        subs = self._subs.get(host_id)
        if subs is not None:
            subs.pop(q, None)
            if not subs:
                del self._subs[host_id]

    # -- internal ---------------------------------------------------------------

    def _feed(self, pull: Pull, line: bytes) -> None:
        line = line.strip()
        if not line:
            return
        try:
            msg = json.loads(line)
        except ValueError:
            return
        if isinstance(msg, dict):
            pull.apply(msg)
            self._emit(pull)

    def _finish(self, pull: Pull, complete: bool) -> None:
        if pull.ended is not None:
            return
        pull.ended = self._clock()
        if pull.status == "pulling":
            pull.status = "done" if complete else "error"
            if not complete:
                pull.message = "the pull was stopped before the end"
        self._emit(pull, force=True)

    def _emit(self, pull: Pull, force: bool = False) -> None:
        now = self._clock()
        if not force and now - pull.last_emit < self.interval:
            return
        pull.last_emit = now
        snap = pull.snapshot()
        for q, only in list(self._subs.get(pull.host_id, {}).items()):
            if only is not None and only != pull.user_id:
                continue
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(snap)

    def _prune(self) -> None:
        now = self._clock()
        for pid in [pid for pid, p in self._pulls.items() if p.ended is not None and now - p.ended > self.linger]:
            del self._pulls[pid]


class TrackedStream:
    """A pull's progress stream, read through by the tracker."""

    def __init__(self, inner: Any, tracker: PullTracker, pull: Pull) -> None:
        self._inner = inner
        self._tracker = tracker
        self._pull = pull
        self._buf = b""

    def _see(self, chunk: Any) -> None:
        if not chunk:
            return
        self._buf += chunk if isinstance(chunk, bytes) else str(chunk).encode()
        *lines, self._buf = self._buf.split(b"\n")
        for line in lines:
            self._tracker._feed(self._pull, line)

    def _end(self, complete: bool) -> None:
        if self._buf:
            self._tracker._feed(self._pull, self._buf)
            self._buf = b""
        self._tracker._finish(self._pull, complete)

    async def read(self, n: int = -1) -> bytes:
        data = await self._inner.read(n)
        if data:
            self._see(data)
        else:
            self._end(True)
        return data

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        complete = False
        try:
            async for chunk in self._inner:
                self._see(chunk)
                yield chunk
            complete = True
        finally:
            self._end(complete)

    async def close(self) -> None:
        try:
            close = getattr(self._inner, "close", None)
            if close is not None:
                res = close()
                if hasattr(res, "__await__"):
                    await res
        finally:
            self._end(False)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


TRACKER = PullTracker()


def current_user_id() -> str:
    """The id of the user whose request is running, "" outside a request."""
    from kowan.auth import authorize

    ctx = authorize.current()
    return str(getattr(getattr(ctx, "user", None), "id", "") or "") if ctx is not None else ""


def visible_to(host_id: str) -> Optional[str]:
    """Whose pulls on ``host_id`` the current request may follow: None for
    all of them (no restriction, or a grant on the whole host), else only
    the user's own."""
    from kowan.auth import authorize

    ctx = authorize.current()
    if ctx is None:
        return None
    try:
        if ctx.unrestricted or ctx.host_rank(host_id) > 0:
            return None
    except Exception:  # noqa: BLE001 - fail closed: own pulls only
        pass
    return current_user_id()


def pull_ref(args: tuple, kwargs: Dict[str, Any]) -> Optional[str]:
    """The reference of an ``Ops.pull_image(ref, registry_auth)`` call."""
    if args:
        return str(args[0])
    ref = kwargs.get("ref")
    return None if ref is None else str(ref)
