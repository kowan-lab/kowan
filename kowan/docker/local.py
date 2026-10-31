"""The local Docker client: :class:`LocalClient` implementing :class:`Ops`.

This talks to the **Docker Engine HTTP API directly over the unix socket** (the
frozen contract forbids docker-py). Requests go through an
:class:`aiohttp.ClientSession` bound to an :class:`aiohttp.UnixConnector`; the
Engine's REST surface is stable enough to drive by hand.

Design notes:

- We start from a conservative API version prefix (``/v1.43``) and, on first connect, clamp it into
  the daemon's advertised ``[MinAPIVersion, ApiVersion]`` window (see
  ``_negotiate_version``) so we talk to both old and Engine-28+ daemons — then
  decode the raw JSON the Engine returns, mapping it to ``kowan.models`` types.
- Streams (logs, stats stream, pull, volume downloads) are
  :class:`_ResponseStream` — an async byte stream over an aiohttp response body
  that closes the response (and any owned cleanup) on ``close()``.
- The 8-byte stdout/stderr multiplexing framing Docker uses when a container
  has no TTY is decoded by :func:`_demux_stdcopy` /
  :class:`_DemuxStream`.
- compose / swarm / build shell out to the ``docker`` / ``git`` CLIs via
  ``asyncio.create_subprocess_exec``.
- Volume browsing mounts the volume into a throwaway, never-started helper
  container and ``docker cp``s the tree out as a tar (Engine
  ``GET /containers/{id}/archive``), then parses the tar in Python.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import posixpath
import re
import struct
import tarfile
import tempfile
import time
import zipfile
from collections import OrderedDict
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, Tuple

import aiohttp

from .. import jsonx, models
from ..errors import BadGateway, Conflict, NotFound, Validation
from ..models import (
    BuildOptions,
    BuildResult,
    NetworkCreateOptions,
    PluginInfo,
    PluginInstallOptions,
    VolumeCreateOptions,
)
from .base import AsyncByteStream, ExecStream, Ops

log = logging.getLogger("kowan.docker.local")

# Highest Engine API version we're built against. We never REQUEST a version
# newer than this. On first connect we query the daemon's own supported window
# (GET /version -> {MinAPIVersion, ApiVersion}) and clamp our prefix into it:
#   * down to ApiVersion, so an OLD daemon (e.g. 20.10, max 1.41) doesn't 400
#     "client version 1.43 is too new";
#   * up to MinAPIVersion, so a NEW daemon (Engine 28+ dropped API < 1.44) doesn't
#     400 "client version 1.43 is too old" — the case this ceiling alone can't
#     cover, since it only bounds us from above.
# 1.43 stays a conservative floor for what we actually call; the up-clamp lets us
# still talk to daemons that no longer serve it, without pinning everyone high.
_API_VERSION_CEILING = "1.43"


def _parse_api_version(s: str) -> Tuple[int, int]:
    """Parse a ``MAJOR.MINOR`` Engine API version string to a comparable tuple."""
    major, _, minor = s.strip().lstrip("v").partition(".")
    return (int(major), int(minor or 0))


def _min_api_version(a: str, b: str) -> str:
    """Return the lower of two ``MAJOR.MINOR`` Engine API version strings.

    Falls back to ``a`` if either is unparseable, so a weird ``/version`` payload
    never crashes negotiation.
    """
    try:
        return a if _parse_api_version(a) <= _parse_api_version(b) else b
    except (ValueError, AttributeError):
        return a


def _max_api_version(a: str, b: str) -> str:
    """Return the higher of two ``MAJOR.MINOR`` Engine API version strings.

    Same lenient fallback as :func:`_min_api_version`.
    """
    try:
        return a if _parse_api_version(a) >= _parse_api_version(b) else b
    except (ValueError, AttributeError):
        return a

# Engine API version that introduced ``one-shot=true`` on the stats endpoint
# (Docker 20.10). With it the daemon returns a single sample immediately instead
# of waiting ~1-2 s to collect a second one for ``precpu_stats``.
_STATS_ONESHOT_MIN_API = (1, 41)


class CpuSampleCache:
    """Per-container CPU baseline for one-shot stats.

    A one-shot stats sample carries no ``precpu_stats``, so CPU% can't be derived
    from the sample alone. Instead we keep the previous ``cpu_stats`` of each
    container in memory and compute the delta against it (the delta ``docker
    stats`` takes between consecutive samples, just over the caller's interval —
    15 s for the monitor — instead of ~1 s), as a share of the whole host like
    :func:`_calculate_cpu_percent`.

    Bounded two ways so it can't grow on hosts with container churn: entries not
    refreshed within ``ttl`` seconds are evicted lazily, and at most
    ``max_entries`` are kept (least recently sampled dropped first). An
    :class:`OrderedDict` ordered by last-sample time makes both O(1) amortized.
    """

    # Two samples closer than this are too noisy to diff (e.g. the monitor and an
    # API call landing together); reuse the last computed value instead.
    MIN_INTERVAL = 0.25

    def __init__(
        self,
        *,
        max_entries: int = 2048,
        ttl: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max = max(1, int(max_entries))
        self._ttl = float(ttl)
        self._clock = clock
        # id -> (total_usage, system_cpu_usage, sampled_at, last_cpu)
        self._samples: "OrderedDict[str, Tuple[float, float, float, Optional[float]]]" = OrderedDict()

    def __len__(self) -> int:
        return len(self._samples)

    def __contains__(self, cid: object) -> bool:
        return cid in self._samples

    @staticmethod
    def _extract(raw: Dict[str, Any]) -> Optional[Tuple[float, float]]:
        cpu_stats = (raw or {}).get("cpu_stats") or {}
        total = (cpu_stats.get("cpu_usage") or {}).get("total_usage")
        system = cpu_stats.get("system_cpu_usage")
        if total is None or not system:
            # Windows daemons don't report system_cpu_usage: no delta possible.
            return None
        return float(total), float(system)

    def _evict(self, now: float) -> None:
        # Oldest entries sit at the front; drop the expired ones, then trim to cap.
        while self._samples:
            cid, entry = next(iter(self._samples.items()))
            if now - entry[2] <= self._ttl:
                break
            del self._samples[cid]
        while len(self._samples) > self._max:
            self._samples.popitem(last=False)

    def record(self, cid: str, raw: Dict[str, Any], cpu: Optional[float] = None) -> None:
        """Store ``raw``'s ``cpu_stats`` as the new baseline for ``cid``."""
        cur = self._extract(raw)
        if cur is None:
            return
        now = self._clock()
        self._samples[cid] = (cur[0], cur[1], now, cpu)
        self._samples.move_to_end(cid)
        self._evict(now)

    def compute(self, cid: str, raw: Dict[str, Any]) -> Optional[float]:
        """CPU% of ``raw`` against the previous sample of ``cid``.

        Returns ``None`` when there's no usable baseline (first sight, evicted,
        counter reset after a restart, or no ``system_cpu_usage``) so the caller
        can fall back to the slower two-sample call. On success the sample
        becomes the new baseline.
        """
        cur = self._extract(raw)
        now = self._clock()
        self._evict(now)
        if cur is None:
            return None
        prev = self._samples.get(cid)
        if prev is not None and now - prev[2] < self.MIN_INTERVAL and prev[3] is not None:
            return prev[3]
        cpu: Optional[float] = None
        if prev is not None:
            cpu_delta = cur[0] - prev[0]
            system_delta = cur[1] - prev[1]
            if cpu_delta >= 0.0 and system_delta > 0.0:
                cpu = cpu_delta / system_delta * 100.0
        self._samples[cid] = (cur[0], cur[1], now, cpu)
        self._samples.move_to_end(cid)
        return cpu


# The unix socket the Docker daemon listens on by default.
DEFAULT_SOCKET = "/var/run/docker.sock"

# Where the browsed volume is mounted inside the throwaway helper container.
_VOLUME_MOUNT_BASE = "/__kowan_vol"

# Upper bound on the bytes read_volume_file returns.
_MAX_VOLUME_FILE_BYTES = 2 << 20  # 2 MiB


# ---------------------------------------------------------------------------
# Streaming adapters (io.ReadCloser analogues)
# ---------------------------------------------------------------------------


class _ResponseStream:
    """An :class:`AsyncByteStream` over an aiohttp response body.

    Owns the response (and, when driving a background-connected daemon request,
    the session's request context manager) and releases both on ``close()``. The
    optional ``on_close`` callback runs after the response is released — the
    volume-download paths use it to remove the helper container once the client
    has finished streaming.
    """

    def __init__(
        self,
        resp: aiohttp.ClientResponse,
        *,
        ctx: Any = None,
        on_close: Any = None,
    ) -> None:
        self._resp = resp
        self._ctx = ctx
        self._on_close = on_close
        self._closed = False

    async def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            return await self._resp.content.read()
        return await self._resp.content.read(n)

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._resp.content.iter_chunked(64 * 1024)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._resp.release()
        except Exception:
            pass
        if self._ctx is not None:
            try:
                await self._ctx.__aexit__(None, None, None)
            except Exception:
                pass
        if self._on_close is not None:
            res = self._on_close()
            if asyncio.iscoroutine(res):
                await res


class _GenStream:
    """An :class:`AsyncByteStream` over an async generator of chunks.

    Hands back transformed content (a file cut out of a tar, a zip built from
    one) as it is produced, so a large volume never sits in memory.
    ``on_close`` runs on ``close()`` and releases the source and helper.
    """

    def __init__(self, gen: AsyncIterator[bytes], *, on_close: Any = None) -> None:
        self._gen = gen
        self._rest = b""
        self._on_close = on_close
        self._closed = False

    async def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            out = bytearray(self._rest)
            self._rest = b""
            async for chunk in self._gen:
                out.extend(chunk)
            return bytes(out)
        while not self._rest:
            try:
                self._rest = await self._gen.__anext__()
            except StopAsyncIteration:
                return b""
        out, self._rest = self._rest[:n], self._rest[n:]
        return out

    async def __aiter__(self) -> AsyncIterator[bytes]:  # type: ignore[override]
        while True:
            chunk = await self.read(64 * 1024)
            if not chunk:
                return
            yield chunk

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._gen.aclose()  # type: ignore[attr-defined]
        if self._on_close is not None:
            res = self._on_close()
            if asyncio.iscoroutine(res):
                await res


# Most exec output kept in memory; the rest of a chatty command is dropped.
_EXEC_OUTPUT_MAX = 4 << 20

async def _demux_stdcopy(src: AsyncByteStream, limit: int = _EXEC_OUTPUT_MAX) -> bytes:
    """Strip Docker's 8-byte stdout/stderr multiplexing headers from ``src``.

    Reimplements ``stdcopy.StdCopy``: when a container has no TTY the Engine
    frames its output as repeated ``[stream(1) 0 0 0 size(4, big-endian)]`` +
    ``size`` payload bytes, interleaving stdout and stderr. We concatenate both
    streams' payloads in order, as if both were written into one pipe. The per-line RFC3339Nano timestamp stays in the payload.

    Frames are decoded as chunks arrive and reading stops once more than
    ``limit`` payload bytes are out, so a chatty command costs ``limit`` bytes
    of memory, not its whole output. The result is cut to ``limit``.
    """
    buf = bytearray()   # undecoded input: at most one partial frame
    out = bytearray()
    need = 0            # payload bytes still owed to the current frame
    async for chunk in src:
        buf.extend(chunk)
        while buf:
            if need:
                take = min(need, len(buf))
                out.extend(buf[:take])
                del buf[:take]
                need -= take
            elif len(buf) >= 8:
                need = struct.unpack(">I", bytes(buf[4:8]))[0]
                del buf[:8]
            else:
                break
        if len(out) > limit:
            break
    return bytes(out[:limit])


class _RawParser:
    """Payload parser for an upgraded connection: pass bytes through untouched.

    After a ``101`` response aiohttp stops parsing HTTP and buffers the raw bytes
    until a parser is installed with ``ResponseHandler.set_parser`` (that is how
    its own WebSocket client works). This one feeds them into a StreamReader.
    """

    def __init__(self, reader: aiohttp.StreamReader) -> None:
        self._reader = reader

    def feed_data(self, data: bytes) -> Tuple[bool, bytes]:
        if data:
            self._reader.feed_data(data)
        return False, b""

    def feed_eof(self) -> None:
        self._reader.feed_eof()


class _ExecConn:
    """A bidirectional :class:`ExecStream` over a hijacked exec connection.

    The Engine's exec-attach endpoint answers ``101 UPGRADED`` and turns the
    HTTP connection into a raw duplex byte stream. We install a pass-through
    parser on the connection's protocol and read from it, write to the
    transport, and close everything on ``close()``. A daemon that answers
    ``200`` instead streams the output as the response body, which still works
    for reading.
    """

    def __init__(self, resp: aiohttp.ClientResponse, ctx: Any) -> None:
        self._resp = resp
        self._ctx = ctx
        conn = resp.connection
        self._transport = conn.transport if conn else None
        self._closed = False
        self._reader: Optional[aiohttp.StreamReader] = None
        if resp.status == 101 and conn is not None and conn.protocol is not None:
            proto = conn.protocol
            reader = aiohttp.StreamReader(proto, 2**16, loop=asyncio.get_running_loop())
            proto.set_parser(_RawParser(reader), reader)
            self._reader = reader

    async def read(self, n: int = -1) -> bytes:
        src = self._reader if self._reader is not None else self._resp.content
        if n is None or n < 0:
            return await src.read()
        return await src.read(n)

    async def write(self, data: bytes) -> None:
        if self._transport is None:
            raise BadGateway("docker: exec connection is not writable")
        self._transport.write(data)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._transport is not None:
                self._transport.close()
        except Exception:
            pass
        try:
            self._resp.release()
        except Exception:
            pass
        if self._ctx is not None:
            try:
                await self._ctx.__aexit__(None, None, None)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Registry auth helpers
# ---------------------------------------------------------------------------


def build_registry_auth(username: str, password: str, server_address: str) -> str:
    """Encode registry credentials into the base64 JSON X-Registry-Auth token.

    Returns "" when all three are empty so
    anonymous pulls stay unauthenticated.
    """
    if not username and not password and not server_address:
        return ""
    cfg: Dict[str, str] = {}
    if username:
        cfg["username"] = username
    if password:
        cfg["password"] = password
    if server_address:
        cfg["serveraddress"] = server_address
    raw = json.dumps(cfg, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_registry_auth(token: str) -> Tuple[str, str, str]:
    """Reverse :func:`build_registry_auth`."""
    if not token:
        return "", "", ""
    raw = base64.urlsafe_b64decode(token)
    cfg = json.loads(raw)
    return cfg.get("username", ""), cfg.get("password", ""), cfg.get("serveraddress", "")


def registry_host_from_ref(ref: str) -> str:
    """Extract the registry host from an image reference."""
    docker_hub = "docker.io"
    at = ref.find("@")
    if at >= 0:
        ref = ref[:at]
    slash = ref.find("/")
    if slash < 0:
        return docker_hub
    candidate = ref[:slash]
    if candidate == "localhost" or "." in candidate or ":" in candidate:
        return candidate
    return docker_hub


# ---------------------------------------------------------------------------
# Volume tar helpers
# ---------------------------------------------------------------------------


def _clean_volume_sub_path(sub: str) -> str:
    """Normalize a caller-supplied sub-path to a safe, slash-rooted path.

    Rejects any ``..`` escape attempt.
    """
    p = posixpath.normpath("/" + (sub or "").strip())
    if p == ".":
        p = "/"
    if ".." in p:
        raise Validation("invalid path")
    return p


# Volume browse stops after this many entries or this much tar read.
_VOLUME_LIST_MAX = 5000
_VOLUME_SCAN_MAX = 1 << 30


async def _list_tar_children(src: Any, clean: str, limit: int = _VOLUME_LIST_MAX,
                             max_scan_bytes: int = _VOLUME_SCAN_MAX) -> models.VolumeListing:
    """Return only the IMMEDIATE children of a docker-cp tar stream; the rest
    of the archive is skipped, never buffered. Stops, marking the listing
    truncated, after ``limit`` children or ``max_scan_bytes`` of tar read: the
    Engine tars the whole subtree, so a huge directory would otherwise be read
    to the end."""
    from .containerfs import AsyncTarReader

    # rstrip the trailing slash before basename: at the volume root clean == "/"
    # so the raw path is "/__kowan_vol/", whose posixpath.basename is "" (we want
    # "__kowan_vol"). An empty base would make the child-prefix filter match
    # nothing, so the root listing would come back empty.
    base = posixpath.basename((_VOLUME_MOUNT_BASE + clean).rstrip("/"))
    prefix = base + "/"
    out: List[models.VolumeFile] = []
    reader = AsyncTarReader(src)
    async for h in reader:
        if reader.consumed > max_scan_bytes:
            return models.VolumeListing(entries=out, truncated=True)
        nm = h.name.rstrip("/")
        if not nm.startswith(prefix):
            continue
        rel = nm[len(prefix) :]
        if rel == "" or "/" in rel:
            continue
        if len(out) >= limit:
            return models.VolumeListing(entries=out, truncated=True)
        out.append(
            models.VolumeFile(
                name=rel,
                path=posixpath.join(clean, rel),
                size=h.size,
                mode=_tar_mode_string(h.mode, h.type),
                is_dir=h.type == tarfile.DIRTYPE,
                mod_time=int(h.mtime),
            )
        )
    return models.VolumeListing(entries=out)


def _tar_mode_string(mode: int, kind: bytes) -> str:
    """Render an ``ls -l``-style mode string.

    A leading type char (``d`` for dirs, ``-`` otherwise) followed by
    the rwx permission triads. Good enough for the UI, which only distinguishes
    directories from files.
    """
    perm = mode & 0o777
    first = "d" if kind == tarfile.DIRTYPE else ("L" if kind == tarfile.SYMTYPE else "-")
    bits = ""
    for shift in (6, 3, 0):
        triad = (perm >> shift) & 0o7
        bits += "r" if triad & 0o4 else "-"
        bits += "w" if triad & 0o2 else "-"
        bits += "x" if triad & 0o1 else "-"
    return first + bits


async def _first_tar_file(src: Any) -> Any:
    """Advance a docker-cp tar stream to its first regular file; return the
    reader positioned on its content."""
    from .containerfs import AsyncTarReader

    reader = AsyncTarReader(src)
    async for h in reader:
        if h.type == tarfile.DIRTYPE:
            raise Validation("path is a directory")
        if h.is_regular:
            return reader
    raise NotFound("file not found")


def _sort_volume_files(files: List[models.VolumeFile]) -> None:
    """Directories first, then alphabetical."""
    files.sort(key=lambda f: (not f.is_dir, f.name))


class _ZipSink:
    """The write end zipfile streams into: it keeps only what was written
    since the last ``take()`` (no tell/seek, so zipfile writes data
    descriptors instead of seeking back)."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def write(self, b: Any) -> int:
        self._buf.extend(b)
        return len(b)

    def flush(self) -> None:
        pass

    def take(self) -> bytes:
        out = bytes(self._buf)
        self._buf.clear()
        return out


async def _tar_to_zip(src: Any, clean: str) -> AsyncIterator[bytes]:
    """Transcode a docker-cp tar stream into a zip archive, chunk by chunk."""
    from .containerfs import AsyncTarReader

    # Same basename rule as _list_tar_children.
    base = posixpath.basename((_VOLUME_MOUNT_BASE + clean).rstrip("/"))
    sink = _ZipSink()
    reader = AsyncTarReader(src)
    with zipfile.ZipFile(sink, "w", zipfile.ZIP_DEFLATED) as zw:  # type: ignore[arg-type]
        async for h in reader:
            name = h.name.rstrip("/")
            if name.startswith(base):
                name = name[len(base) :].lstrip("/")
            else:
                name = posixpath.basename(name)
            if name == "":
                if h.type == tarfile.DIRTYPE:
                    continue
                name = posixpath.basename(h.name.rstrip("/"))
            if h.type == tarfile.DIRTYPE:
                zw.writestr(name + "/", b"")
            elif h.is_regular:
                with zw.open(name, "w", force_zip64=True) as dst:
                    while True:
                        chunk = await reader.read_data()
                        if not chunk:
                            break
                        dst.write(chunk)
                        if len(sink._buf) >= 64 * 1024:
                            yield sink.take()
            out = sink.take()
            if out:
                yield out
    out = sink.take()
    if out:
        yield out


class _Bounded:
    """``read(n)`` over at most ``size`` bytes of an async byte stream."""

    def __init__(self, src: AsyncByteStream, size: int) -> None:
        self._src = src
        self._left = size

    async def read(self, n: int = 64 * 1024) -> bytes:
        if self._left <= 0:
            return b""
        chunk = await self._src.read(min(n, self._left))
        self._left -= len(chunk)
        return chunk


def _parse_bind(bind: str) -> Optional[models.VolumeMapping]:
    """Parse ``source:target[:opts]`` into a VolumeMapping."""
    parts = bind.split(":")
    if len(parts) < 2:
        return None
    vm = models.VolumeMapping(source=parts[0], target=parts[1])
    if len(parts) > 2:
        for opt in parts[2].split(","):
            if opt == "ro":
                vm.read_only = True
    return vm


def _last_non_empty_line(s: str) -> str:
    """Return the trimmed last non-empty line."""
    for line in reversed(s.strip().split("\n")):
        t = line.strip()
        if t:
            return t
    return ""


# Docker volume names (the Engine's own validation regex). Anything else in a
# bind spec — notably an absolute path — is a HOST bind mount, not a volume.
_VOLUME_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _check_engine_path(path: str) -> None:
    """Reject Engine API paths a caller-supplied id/name could have subverted.

    Container ids, volume names, image refs ... are interpolated straight into
    the request path, and aiohttp route params arrive URL-decoded (``%2F`` ->
    ``/``, ``%3F`` -> ``?``). A name like ``../containers/<id>/archive?path=/x``
    would otherwise be normalised by yarl into a DIFFERENT Engine endpoint (dot
    segments collapse, ``?`` starts a query), letting e.g. a viewer's volume
    inspect reach arbitrary GET endpoints. Legitimate paths never contain dot
    segments, ``?`` or ``#`` (query params always go through ``params=``).
    """
    if "?" in path or "#" in path or any(seg in (".", "..") for seg in path.split("/")):
        raise Validation("invalid docker resource identifier")


# ---------------------------------------------------------------------------
# Subprocess helper (compose / swarm / build CLI paths)
# ---------------------------------------------------------------------------


# A compose up or image build may pull gigabytes, so the bound is generous; it
# exists so a hung CLI cannot pin a request (and its process) forever.
COMMAND_TIMEOUT = 60 * 60
# Output kept from a command; the rest is drained and dropped.
COMMAND_OUTPUT_LIMIT = 2 << 20


async def _run_command(
    name: str,
    *args: str,
    stdin: Optional[bytes] = None,
    env: Optional[Dict[str, str]] = None,
    timeout: float = COMMAND_TIMEOUT,
) -> Tuple[str, Optional[int]]:
    """Run ``name args...`` and return (combined output, returncode).

    stdout and stderr are merged. The exit code is returned rather than raised,
    so callers that want output-on-failure
    (build) and callers that want an error (compose/swarm) can each decide.
    The command is killed after ``timeout`` seconds, and at most
    COMMAND_OUTPUT_LIMIT bytes of its output are kept.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            name,
            *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
    except FileNotFoundError as exc:
        # A missing CLI otherwise surfaces as a bare "[Errno 2] ... 'docker'".
        # Compose/swarm/build shell out (they aren't Engine API endpoints), so
        # the host needs the CLI even though container ops go over the socket.
        raise BadGateway(
            f"docker: {name!r} not found on PATH — compose/swarm/build "
            f"deployments require the {name!r} CLI to be installed on this host"
        ) from exc

    kept = bytearray()
    dropped = 0

    async def collect() -> None:
        nonlocal dropped
        if stdin is not None and proc.stdin is not None:
            proc.stdin.write(stdin)
            await proc.stdin.drain()
            proc.stdin.close()
        while True:
            chunk = await proc.stdout.read(64 << 10)
            if not chunk:
                break
            room = COMMAND_OUTPUT_LIMIT - len(kept)
            kept.extend(chunk[:room])
            dropped += max(0, len(chunk) - room)
        await proc.wait()

    try:
        await asyncio.wait_for(collect(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        kept.extend(f"\n[{name} killed after {int(timeout)}s]\n".encode())
    out = kept.decode("utf-8", "replace")
    if dropped:
        out += f"\n[{dropped} more bytes of output not kept]\n"
    return out, proc.returncode


def _contained(root: str, subdir: str) -> str:
    """``root/subdir``, refusing anything that resolves outside ``root`` (an
    absolute path, ``..``, or a symlink in a cloned repo): otherwise the build
    context could be the server's data directory."""
    if not subdir:
        return root
    base = os.path.realpath(root)
    path = os.path.realpath(os.path.join(base, subdir))
    if path != base and not path.startswith(base + os.sep):
        raise Validation("build context must stay inside the repository")
    return path


# ---------------------------------------------------------------------------
# LocalClient
# ---------------------------------------------------------------------------


class LocalClient(Ops):
    """:class:`Ops` implemented against the local Docker daemon over its unix socket.

    A single :class:`aiohttp.ClientSession` (bound to an
    :class:`aiohttp.UnixConnector`) is reused for every request; it is created
    lazily so constructing a ``LocalClient`` never touches the socket. Because
    aiohttp needs an authority in the URL even for a unix transport, every path is
    prefixed with ``http://docker/<api-version>``; the connector routes it to the
    socket regardless of host.
    """

    def __init__(self, socket_path: str = DEFAULT_SOCKET) -> None:
        self._socket_path = socket_path
        self._session: Optional[aiohttp.ClientSession] = None
        self._lock = asyncio.Lock()
        # Version prefix used on every request; negotiated down to the daemon's
        # max on first connect (see _negotiate_version). Starts at our ceiling.
        self._api_version = "v" + _API_VERSION_CEILING
        # Image last used for the volume-browse helper container (see
        # _create_volume_helper); "" until first use.
        self._helper_image = ""
        # Previous CPU sample per container, for one-shot stats (see
        # container_stats). Lives here so the agent (which runs a LocalClient)
        # gets the same fast path for remote hosts.
        self._cpu_samples = CpuSampleCache()
        # container id -> stopped on purpose, for the exited-with-error ones
        # (see _mark_manual_stops); an entry lives while its container does.
        self._manual_stops: Dict[str, bool] = {}

    def _supports_stats_oneshot(self) -> bool:
        try:
            return _parse_api_version(self._api_version) >= _STATS_ONESHOT_MIN_API
        except (ValueError, AttributeError):
            return False

    # -- session / low-level request helpers --------------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            async with self._lock:
                if self._session is None or self._session.closed:
                    connector = aiohttp.UnixConnector(path=self._socket_path)
                    # No total timeout: log/stats streams and image pulls run
                    # arbitrarily long; per-request timeouts are applied by callers
                    # via asyncio if needed.
                    session = aiohttp.ClientSession(
                        connector=connector,
                        timeout=aiohttp.ClientTimeout(total=None, sock_connect=30),
                    )
                    # Cap the API version to what this daemon supports, so an
                    # older Engine (e.g. 20.10, max API 1.41) doesn't 400 every
                    # request. Best-effort: on failure we keep the ceiling.
                    # Negotiate BEFORE publishing self._session, otherwise a
                    # concurrent caller (which checks self._session outside the
                    # lock) could grab it and issue a request at the un-negotiated
                    # ceiling, 400ing against an older daemon during the window.
                    try:
                        await self._negotiate_version(session)
                    except BaseException:
                        # Cancelled mid-negotiation (e.g. shutdown cancelling the
                        # monitor's first ping): don't orphan the session.
                        await session.close()
                        raise
                    self._session = session
        return self._session

    async def _negotiate_version(self, session: aiohttp.ClientSession) -> None:
        """Clamp our API prefix into the daemon's supported version window.

        Uses the UNVERSIONED ``/version`` endpoint, which every daemon answers
        regardless of API version, and reads both bounds it reports:
        ``ApiVersion`` (max) and ``MinAPIVersion`` (min). We take our ceiling,
        drop it to the daemon's max, then raise it to the daemon's min — so we
        never 400 as "too new" (old daemon) or "too old" (Engine 28+, min 1.44).
        Any error leaves the ceiling in place.
        """
        try:
            async with session.get("http://docker/version") as resp:
                if resp.status >= 400:
                    return
                doc = json.loads(await resp.read())
        except Exception as exc:  # noqa: BLE001 - negotiation is best-effort
            log.debug("docker api version negotiation failed (keeping %s): %s",
                      self._api_version, exc)
            return
        daemon_max = str((doc or {}).get("ApiVersion") or "").strip()
        daemon_min = str((doc or {}).get("MinAPIVersion") or "").strip()
        if not daemon_max:
            return
        chosen = _min_api_version(daemon_max, _API_VERSION_CEILING)
        # Engine 28+ no longer serves API < 1.44; honour the daemon's floor so a
        # conservative ceiling doesn't 400 as "client version ... is too old".
        if daemon_min:
            chosen = _max_api_version(chosen, daemon_min)
        self._api_version = "v" + chosen
        log.info("docker api version negotiated: daemon window=[%s, %s], using=%s",
                 daemon_min or "?", daemon_max, self._api_version)

    def _url(self, path: str) -> str:
        _check_engine_path(path)
        return f"http://docker/{self._api_version}{path}"

    async def close(self) -> None:
        """Close the underlying session. Idempotent."""
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Any = None,
        json_body: Any = None,
        data: Any = None,
        headers: Any = None,
    ) -> Tuple[int, bytes]:
        """Perform a request and return (status, body). Non-2xx maps to an error."""
        session = await self._get_session()
        try:
            async with session.request(
                method,
                self._url(path),
                params=params,
                json=json_body,
                data=data,
                headers=headers,
            ) as resp:
                body = await resp.read()
        except aiohttp.ClientError as exc:
            raise BadGateway(f"docker: {method} {path}: {exc}") from exc
        if resp.status >= 400:
            raise self._engine_error(method, path, resp.status, body)
        return resp.status, body

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Any = None,
        json_body: Any = None,
    ) -> Any:
        _, body = await self._request(method, path, params=params, json_body=json_body)
        if not body:
            return None
        return jsonx.loads(body)

    async def _open_stream(
        self,
        method: str,
        path: str,
        *,
        params: Any = None,
        json_body: Any = None,
        headers: Any = None,
    ) -> _ResponseStream:
        """Open a streaming request; return a :class:`_ResponseStream` the caller closes."""
        session = await self._get_session()
        ctx = session.request(
            method,
            self._url(path),
            params=params,
            json=json_body,
            headers=headers,
        )
        try:
            resp = await ctx.__aenter__()
        except aiohttp.ClientError as exc:
            raise BadGateway(f"docker: {method} {path}: {exc}") from exc
        if resp.status >= 400:
            body = await resp.read()
            await ctx.__aexit__(None, None, None)
            raise self._engine_error(method, path, resp.status, body)
        return _ResponseStream(resp, ctx=ctx)

    @staticmethod
    def _engine_error(method: str, path: str, status: int, body: bytes) -> Exception:
        """Map an Engine error response to a typed application error."""
        msg = ""
        try:
            doc = json.loads(body)
            if isinstance(doc, dict):
                msg = doc.get("message", "")
        except Exception:
            msg = body.decode("utf-8", "replace")
        detail = f"docker: {method} {path}: {status} {msg}".strip()
        if status == 404:
            return NotFound(msg or detail)
        # The daemon refused the request itself (a running container, a name in
        # use, a bad parameter): that is the caller's problem to fix, not a
        # broken upstream, so it must not surface as 502.
        if status == 409:
            return Conflict(msg or detail)
        if status == 400:
            return Validation(msg or detail)
        return BadGateway(detail)

    # -- reachability -------------------------------------------------------

    async def ping(self) -> None:
        await self._request("GET", "/_ping")

    # -- containers ---------------------------------------------------------

    async def list_containers(self, all: bool) -> List[models.Container]:
        raw = await self._request_json(
            "GET", "/containers/json", params={"all": "1" if all else "0"}
        )
        out: List[models.Container] = []
        for s in raw or []:
            ports = [
                models.Port(
                    private_port=p.get("PrivatePort", 0),
                    public_port=p.get("PublicPort", 0),
                    type=p.get("Type", ""),
                    ip=p.get("IP", ""),
                )
                for p in (s.get("Ports") or [])
            ]
            out.append(
                models.Container(
                    id=s.get("Id", ""),
                    names=s.get("Names") or [],
                    image=s.get("Image", ""),
                    image_id=s.get("ImageID", "") or "",
                    state=s.get("State", ""),
                    status=s.get("Status", ""),
                    created=s.get("Created", 0),
                    ports=ports,
                    labels=s.get("Labels") or {},
                )
            )
        await self._mark_manual_stops(out)
        return out

    async def _mark_manual_stops(self, containers: List[models.Container]) -> None:
        """Set ``manually_stopped`` on the containers that exited with an
        error code although nothing crashed: with an always/unless-stopped
        restart policy Docker restarts a crashed container, so one still
        exited was stopped (docker stop). The list has no restart policy, so
        those few are inspected, once each: a container leaves the exited
        state only by starting again, which drops its entry."""
        failed = [c for c in containers if c.state == "exited" and _exit_code(c.status) not in (None, 0)]
        live = {c.id for c in failed}
        for cid in [k for k in self._manual_stops if k not in live]:
            del self._manual_stops[cid]

        async def one(c: models.Container) -> None:
            try:
                doc = await self.inspect_container(c.id)
            except Exception:  # noqa: BLE001 - unknown: keep it a problem
                return
            policy = ((doc.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name") or ""
            self._manual_stops[c.id] = policy in ("always", "unless-stopped")

        await asyncio.gather(*(one(c) for c in failed if c.id not in self._manual_stops))
        for c in failed:
            c.manually_stopped = self._manual_stops.get(c.id, False)

    async def inspect_container(self, id: str) -> Dict[str, Any]:
        return await self._request_json("GET", f"/containers/{id}/json")

    async def start_container(self, id: str) -> None:
        await self._request("POST", f"/containers/{id}/start")

    async def stop_container(self, id: str) -> None:
        await self._request("POST", f"/containers/{id}/stop")

    async def restart_container(self, id: str) -> None:
        await self._request("POST", f"/containers/{id}/restart")

    async def pause_container(self, id: str) -> None:
        await self._request("POST", f"/containers/{id}/pause")

    async def kill_container(self, id: str, signal: str = "SIGKILL") -> None:
        await self._request("POST", f"/containers/{id}/kill", params={"signal": signal})

    async def unpause_container(self, id: str) -> None:
        await self._request("POST", f"/containers/{id}/unpause")

    async def remove_container(self, id: str, force: bool) -> None:
        await self._request(
            "DELETE", f"/containers/{id}", params={"force": "1" if force else "0"}
        )

    async def container_logs(self, id: str, follow: bool, tail: int) -> AsyncByteStream:
        tail_spec = str(tail) if tail > 0 else "all"
        params = {
            "stdout": "1",
            "stderr": "1",
            "follow": "1" if follow else "0",
            "tail": tail_spec,
            "timestamps": "1",
        }
        stream = await self._open_stream(
            "GET", f"/containers/{id}/logs", params=params
        )
        # Follow needs incremental demuxing; a one-shot tail can be demuxed whole.
        # The container may have a TTY, in which case there is no multiplexing
        # framing and the body is raw — the demux wrapper detects and passes those
        # through.
        return _DemuxStream(stream)

    async def container_stats(self, id: str) -> models.ContainerStats:
        # Make sure the API version is negotiated before choosing the call shape.
        await self._get_session()
        if self._supports_stats_oneshot():
            # Fast path: one sample, returned immediately; CPU% from the delta
            # against this container's previous sample (kept in memory).
            raw = await self._request_json(
                "GET",
                f"/containers/{id}/stats",
                params={"stream": "false", "one-shot": "true"},
            ) or {}
            cpu = self._cpu_samples.compute(id, raw)
            if cpu is not None:
                stats = _stats_from_response(raw)
                stats.cpu_percent = cpu
                return stats
            # No baseline yet (first sight / restart): fall through to the
            # two-sample call once, so the first answer is still accurate. Its
            # cpu_stats seed the baseline for the next, fast call.
        raw = await self._request_json(
            "GET", f"/containers/{id}/stats", params={"stream": "0"}
        ) or {}
        stats = _stats_from_response(raw)
        if self._supports_stats_oneshot():
            self._cpu_samples.record(id, raw, stats.cpu_percent)
        return stats

    async def container_stats_stream(self, id: str) -> AsyncByteStream:
        stream = await self._open_stream(
            "GET", f"/containers/{id}/stats", params={"stream": "1"}
        )
        return _StatsNDJSONStream(stream)

    async def events(
        self,
        since: Optional[int] = None,
        until: Optional[int] = None,
        filters: Optional[Dict[str, List[str]]] = None,
    ) -> AsyncByteStream:
        # The Engine already emits one JSON object per line, so the raw body is
        # returned as-is; normalisation happens in kowan.api.events.
        params: Dict[str, str] = {}
        if since:
            params["since"] = str(int(since))
        if until:
            params["until"] = str(int(until))
        if filters:
            params["filters"] = json.dumps(filters, separators=(",", ":"))
        return await self._open_stream("GET", "/events", params=params)

    async def container_spec(self, id: str) -> models.ContainerSpec:
        insp = await self._request_json("GET", f"/containers/{id}/json")
        return _container_spec_from_inspect(insp or {})

    async def create_container(self, name: str, spec: Any, start: bool) -> str:
        return await self._create_container(name, spec, start)

    async def clone_recreate(
        self, target: str, new_image: str, *, stop_timeout: int = 10, start: bool = True
    ) -> str:
        """Recreate container ``target`` from ``new_image``, cloning its config.

        Inspect the container, then create a replacement with the SAME name and
        the SAME ``Config``/``HostConfig`` (env, binds, published ports, restart
        policy, labels, network mode) but the new image. Any additional attached
        networks are reconnected. ``new_image`` must already be present locally.
        Returns the new container id.

        Rollback-safe, like :meth:`container_relabel`: the original is renamed out
        of the way and stopped, not removed, until the clone has been created
        (and started, when ``start``). Any failure removes the clone, renames the
        original back and restarts it if it was running, then re-raises, so a
        failed update never loses the container.

        This is a raw clone (feed inspect's Config/HostConfig back to create),
        not a Kowan ``ContainerSpec`` round-trip, so operator-added flags on the
        original container survive the update.
        """
        from kowan.updater.clone import clone_body

        info = await self._request_json("GET", f"/containers/{target}/json")
        if not isinstance(info, dict):
            raise BadGateway(f"docker: inspect {target}: unexpected response")
        old_id = str(info.get("Id") or target)
        name = (info.get("Name") or target).lstrip("/")
        was_running = bool((info.get("State") or {}).get("Running"))

        # Build the create body with the shared cloner: values that came from the
        # OLD image (Cmd, Entrypoint, Healthcheck, Env, labels…) are dropped so the
        # new image's defaults apply, anonymous volumes are re-bound by name, and
        # network aliases are kept.
        old_image_config: Optional[Dict[str, Any]] = None
        try:
            old_img = await self._request_json("GET", f"/images/{info.get('Image')}/json")
            if isinstance(old_img, dict):
                old_image_config = old_img.get("Config") or {}
        except Exception:  # noqa: BLE001 - old image already removed
            old_image_config = None
        body, extra_networks = clone_body(info, old_image_config, new_image)

        tmp_name = f"{name}-kowan-old-{int(time.time())}"
        await self._request("POST", f"/containers/{old_id}/rename", params={"name": tmp_name})
        cid = ""
        try:
            try:
                await self._request(
                    "POST", f"/containers/{old_id}/stop", params={"t": str(stop_timeout)}
                )
            except Exception:  # noqa: BLE001 - already stopped
                pass
            created = await self._request_json(
                "POST", "/containers/create", params={"name": name}, json_body=body
            )
            cid = created["Id"]
            for net, endpoint in extra_networks:
                try:
                    await self.connect_container_endpoint(net, cid, endpoint)
                except Exception:  # noqa: BLE001 - non-fatal
                    log.warning("clone_recreate: reconnect network %s failed", net)
            if start:
                await self._request("POST", f"/containers/{cid}/start")
        except BaseException:
            if cid:
                try:
                    await self._request("DELETE", f"/containers/{cid}", params={"force": "true"})
                except Exception:  # noqa: BLE001
                    log.warning("clone_recreate rollback: remove clone %s failed", cid)
            try:
                await self._request("POST", f"/containers/{old_id}/rename", params={"name": name})
                if was_running:
                    await self._request("POST", f"/containers/{old_id}/start")
            except Exception:  # noqa: BLE001
                log.error("clone_recreate rollback: restore %s (%s) failed", name, old_id)
            raise
        try:
            await self._request("DELETE", f"/containers/{old_id}", params={"force": "true"})
        except Exception:  # noqa: BLE001 - the clone is live; a leftover is cosmetic
            log.warning("clone_recreate: remove original %s failed", old_id)
        return cid

    # -- self-update primitives (kowan/updater) ------------------------------
    # Small raw Engine calls the transactional self-update helper needs: rename
    # a container, change its restart policy in place, stop with an explicit
    # timeout, create from a raw (cloned) body, connect with an endpoint config
    # (aliases), and read a bounded log tail as text.

    async def rename_container(self, id: str, new_name: str) -> None:
        await self._request("POST", f"/containers/{id}/rename", params={"name": new_name})

    async def update_container_config(self, id: str, body: Dict[str, Any]) -> None:
        """``POST /containers/{id}/update`` (resources / restart policy)."""
        await self._request("POST", f"/containers/{id}/update", json_body=body)

    async def stop_container_timeout(self, id: str, timeout: int) -> None:
        """Stop with an explicit grace period (304 "already stopped" is fine)."""
        await self._request("POST", f"/containers/{id}/stop", params={"t": str(int(timeout))})

    async def create_container_raw(self, name: str, body: Dict[str, Any]) -> str:
        """Create from a raw Engine body (no start, no name-conflict removal)."""
        created = await self._request_json(
            "POST", "/containers/create", params={"name": name}, json_body=body
        )
        return str((created or {}).get("Id") or "")

    async def connect_container_endpoint(
        self, network_id: str, container_id: str, endpoint_config: Dict[str, Any]
    ) -> None:
        body: Dict[str, Any] = {"Container": container_id}
        if endpoint_config:
            body["EndpointConfig"] = endpoint_config
        await self._request("POST", f"/networks/{network_id}/connect", json_body=body)

    async def container_logs_tail(self, id: str, tail: int) -> List[str]:
        """The last ``tail`` log lines (stdout+stderr, no timestamps) as text."""
        stream = await self._open_stream(
            "GET",
            f"/containers/{id}/logs",
            params={"stdout": "1", "stderr": "1", "follow": "0",
                    "tail": str(tail) if tail > 0 else "all", "timestamps": "0"},
        )
        demux = _DemuxStream(stream)
        buf = bytearray()
        try:
            async for chunk in demux:
                buf.extend(chunk)
                if len(buf) > 4 * 1024 * 1024:
                    break
        finally:
            await demux.close()
        return buf.decode("utf-8", "replace").splitlines()

    # -- exec ---------------------------------------------------------------

    async def exec(self, id: str, cmd: List[str]) -> str:
        created = await self._request_json(
            "POST",
            f"/containers/{id}/exec",
            json_body={
                "AttachStdout": True,
                "AttachStderr": True,
                "Cmd": cmd,
            },
        )
        exec_id = created["Id"]
        # Start the exec attached (not detached); the Engine hijacks the
        # connection and streams multiplexed stdout/stderr, which we demux.
        stream = await self._open_stream(
            "POST",
            f"/exec/{exec_id}/start",
            json_body={"Detach": False, "Tty": False},
            headers={"Content-Type": "application/json"},
        )
        try:
            data = await _demux_stdcopy(stream)
        finally:
            await stream.close()
        return data.decode("utf-8", "replace")

    async def exec_attach(self, id: str, cmd: List[str], tty: bool) -> ExecStream:
        created = await self._request_json(
            "POST",
            f"/containers/{id}/exec",
            json_body={
                "AttachStdin": True,
                "AttachStdout": True,
                "AttachStderr": True,
                "Tty": tty,
                "Cmd": cmd,
            },
        )
        exec_id = created["Id"]
        session = await self._get_session()
        # Request a hijacked (raw upgraded) connection so we can both read and
        # write the interactive session.
        ctx = session.request(
            "POST",
            self._url(f"/exec/{exec_id}/start"),
            json={"Detach": False, "Tty": tty},
            headers={
                "Content-Type": "application/json",
                "Connection": "Upgrade",
                "Upgrade": "tcp",
            },
        )
        try:
            resp = await ctx.__aenter__()
        except aiohttp.ClientError as exc:
            raise BadGateway(f"docker: exec attach {id}: {exc}") from exc
        if resp.status >= 400:
            body = await resp.read()
            await ctx.__aexit__(None, None, None)
            raise self._engine_error("POST", f"/exec/{exec_id}/start", resp.status, body)
        return _ExecConn(resp, ctx)

    # -- images -------------------------------------------------------------

    async def list_images(self) -> List[models.Image]:
        raw = await self._request_json(
            "GET", "/images/json", params={"all": "0"}
        )
        out: List[models.Image] = []
        for s in raw or []:
            out.append(
                models.Image(
                    id=s.get("Id", ""),
                    repo_tags=s.get("RepoTags") or [],
                    size=s.get("Size", 0),
                    created=s.get("Created", 0),
                )
            )
        return out

    async def pull_image(self, ref: str, registry_auth: str) -> AsyncByteStream:
        headers = {}
        if registry_auth:
            headers["X-Registry-Auth"] = registry_auth
        return await self._open_stream(
            "POST", "/images/create", params={"fromImage": ref}, headers=headers
        )

    async def remove_image(self, id: str, force: bool) -> None:
        await self._request(
            "DELETE",
            f"/images/{id}",
            params={"force": "1" if force else "0", "noprune": "0"},
        )

    async def inspect_image(self, id: str) -> Dict[str, Any]:
        return await self._request_json("GET", f"/images/{id}/json")

    async def image_history(self, id: str) -> List[models.ImageHistoryLayer]:
        raw = await self._request_json("GET", f"/images/{id}/history")
        out: List[models.ImageHistoryLayer] = []
        for it in raw or []:
            out.append(
                models.ImageHistoryLayer(
                    created=it.get("Created", 0),
                    created_by=it.get("CreatedBy", ""),
                    size=it.get("Size", 0),
                    comment=it.get("Comment", ""),
                    tags=it.get("Tags") or [],
                )
            )
        return out

    async def build_image(self, opts: BuildOptions) -> BuildResult:
        return await self._build_image(opts)

    # -- volumes ------------------------------------------------------------

    async def list_volumes(self) -> List[models.Volume]:
        raw = await self._request_json("GET", "/volumes")
        out: List[models.Volume] = []
        for v in (raw or {}).get("Volumes") or []:
            if v is None:
                continue
            out.append(_volume_from_engine(v))
        return out

    async def create_volume(self, opts: VolumeCreateOptions) -> models.Volume:
        v = await self._request_json(
            "POST",
            "/volumes/create",
            json_body={
                "Name": opts.name,
                "Driver": opts.driver,
                "DriverOpts": opts.options,
                "Labels": opts.labels,
            },
        )
        return _volume_from_engine(v)

    async def remove_volume(self, name: str, force: bool) -> None:
        await self._request(
            "DELETE", f"/volumes/{name}", params={"force": "1" if force else "0"}
        )

    async def inspect_volume(self, name: str) -> Dict[str, Any]:
        return await self._request_json("GET", f"/volumes/{name}")

    async def browse_volume(self, name: str, sub_path: str) -> models.VolumeListing:
        clean = _clean_volume_sub_path(sub_path)
        out = await self._with_volume_archive(name, clean, lambda src: _list_tar_children(src, clean))
        _sort_volume_files(out.entries)
        return out

    async def read_volume_file(self, name: str, sub_path: str) -> bytes:
        clean = _clean_volume_sub_path(sub_path)
        if clean == "/":
            raise Validation("not a file")

        async def first(src: Any) -> bytes:
            reader = await _first_tar_file(src)
            out = bytearray()
            while len(out) < _MAX_VOLUME_FILE_BYTES:
                chunk = await reader.read_data(_MAX_VOLUME_FILE_BYTES - len(out))
                if not chunk:
                    break
                out.extend(chunk)
            return bytes(out)

        return await self._with_volume_archive(name, clean, first)

    async def download_volume_file(self, name: str, sub_path: str) -> AsyncByteStream:
        clean = _clean_volume_sub_path(sub_path)
        if clean == "/":
            raise Validation("not a file")
        src, close = await self._open_volume_archive(name, clean)
        try:
            reader = await _first_tar_file(src)
        except BaseException:
            await close()
            raise

        async def content() -> AsyncIterator[bytes]:
            while True:
                chunk = await reader.read_data()
                if not chunk:
                    return
                yield chunk

        return _GenStream(content(), on_close=close)

    async def download_volume_archive(self, name: str, sub_path: str) -> AsyncByteStream:
        clean = _clean_volume_sub_path(sub_path)
        src, close = await self._open_volume_archive(name, clean)
        return _GenStream(_tar_to_zip(src, clean), on_close=close)

    async def upload_volume_file(
        self,
        name: str,
        dest_dir: str,
        filename: str,
        size: int,
        content: AsyncByteStream,
    ) -> None:
        from . import containerfs

        clean_dir = _clean_volume_sub_path(dest_dir)
        fn = posixpath.basename(filename.replace("\\", "/"))
        if fn in (".", "/", "..", ""):
            raise Validation("invalid file name")

        # Wrap the (size-bounded) content in a one-entry tar as it streams and
        # docker-cp it into a read-write helper mount.
        tar = containerfs.TarFileStream(fn, size, _Bounded(content, size))

        async def body() -> AsyncIterator[bytes]:
            async for chunk in tar:
                yield chunk

        cid, remove = await self._create_volume_helper(name, read_only=False)
        try:
            await self._request(
                "PUT",
                f"/containers/{cid}/archive",
                params={"path": _VOLUME_MOUNT_BASE + clean_dir},
                data=body(),
                headers={"Content-Type": "application/x-tar"},
            )
        finally:
            await remove()

    # -- plugins ------------------------------------------------------------

    async def list_plugins(self) -> List[PluginInfo]:
        raw = await self._request_json("GET", "/plugins")
        out: List[PluginInfo] = []
        for p in raw or []:
            if p is None:
                continue
            cfg = p.get("Config") or {}
            out.append(
                PluginInfo(
                    id=p.get("Id", ""),
                    name=p.get("Name", ""),
                    enabled=p.get("Enabled", False),
                    reference=p.get("PluginReference", ""),
                    description=cfg.get("Description", ""),
                )
            )
        return out

    async def install_plugin(self, opts: PluginInstallOptions) -> None:
        if not (opts.remote or "").strip():
            raise Validation("docker: install plugin: remote reference is required")
        alias = opts.alias or opts.remote

        # The daemon rejects the pull with "incorrect privileges" unless we grant
        # exactly the privilege set the plugin declares (every volume driver such
        # as s3fs/sshfs needs mount/network/capability privileges), i.e. accept
        # all permissions: GET the declared privileges, then POST that list as the body of /plugins/pull.
        privileges = await self._request_json(
            "GET", "/plugins/privileges", params={"remote": opts.remote}
        )
        if not isinstance(privileges, list):
            privileges = []

        # POST /plugins/pull creates the plugin in the DISABLED state and streams
        # progress; drain it so the pull completes. The Engine has no "disabled"
        # pull parameter — whether to enable afterwards is a client-side choice,
        # handled below.
        try:
            stream = await self._open_stream(
                "POST",
                "/plugins/pull",
                params={"remote": opts.remote, "name": alias},
                headers={"X-Registry-Auth": ""},
                json_body=privileges,
            )
        except NotFound:
            raise
        except (BadGateway, Conflict) as exc:
            # Treat an already-installed plugin as success (idempotent install).
            if "already exists" in str(exc).lower():
                return
            raise
        try:
            async for _ in stream:
                pass
        finally:
            await stream.close()

        # Configure (if settings were supplied) then enable: a plugin with settings is installed disabled, configured, then
        # enabled; a plugin without settings is enabled straight after the pull.
        if opts.settings:
            args = [f"{k}={v}" for k, v in opts.settings.items()]
            await self._request("POST", f"/plugins/{alias}/set", json_body=args)
        await self._request(
            "POST", f"/plugins/{alias}/enable", params={"timeout": "30"}
        )

    async def remove_plugin(self, name: str, force: bool) -> None:
        await self._request(
            "DELETE", f"/plugins/{name}", params={"force": "1" if force else "0"}
        )

    # -- networks -----------------------------------------------------------

    async def list_networks(self) -> List[models.Network]:
        raw = await self._request_json("GET", "/networks")
        out: List[models.Network] = []
        for n in raw or []:
            out.append(
                models.Network(
                    id=n.get("Id", ""),
                    name=n.get("Name", ""),
                    driver=n.get("Driver", ""),
                    scope=n.get("Scope", ""),
                    internal=n.get("Internal", False),
                    attachable=n.get("Attachable", False),
                    labels=n.get("Labels") or {},
                )
            )
        return out

    async def create_network(self, opts: NetworkCreateOptions) -> models.Network:
        body: Dict[str, Any] = {
            "Name": opts.name,
            "Driver": opts.driver,
            "Internal": opts.internal,
            "Attachable": opts.attachable,
            "Labels": opts.labels,
            "Options": opts.options,
        }
        ipam_cfg = []
        for c in opts.ipam or []:
            subnet = getattr(c, "subnet", "") if not isinstance(c, dict) else c.get("subnet", "")
            gateway = getattr(c, "gateway", "") if not isinstance(c, dict) else c.get("gateway", "")
            ip_range = getattr(c, "ip_range", "") if not isinstance(c, dict) else c.get("ipRange", "")
            entry: Dict[str, Any] = {}
            if subnet:
                entry["Subnet"] = subnet
            if gateway:
                entry["Gateway"] = gateway
            if ip_range:
                entry["IPRange"] = ip_range
            ipam_cfg.append(entry)
        if ipam_cfg:
            body["IPAM"] = {"Config": ipam_cfg}

        resp = await self._request_json("POST", "/networks/create", json_body=body)
        return models.Network(
            id=(resp or {}).get("Id", ""),
            name=opts.name,
            driver=opts.driver,
            internal=opts.internal,
            attachable=opts.attachable,
            labels=opts.labels,
            ipam=list(opts.ipam) if opts.ipam else None,
        )

    async def inspect_network(self, id: str) -> models.Network:
        n = await self._request_json("GET", f"/networks/{id}")
        out = models.Network(
            id=n.get("Id", ""),
            name=n.get("Name", ""),
            driver=n.get("Driver", ""),
            scope=n.get("Scope", ""),
            internal=n.get("Internal", False),
            attachable=n.get("Attachable", False),
            labels=n.get("Labels") or {},
        )
        ipam = (n.get("IPAM") or {}).get("Config") or []
        cfgs = []
        for cfg in ipam:
            cfgs.append(
                models.IPAMConfig(
                    subnet=cfg.get("Subnet", ""),
                    gateway=cfg.get("Gateway", ""),
                    ip_range=cfg.get("IPRange", ""),
                )
            )
        if cfgs:
            out.ipam = cfgs
        containers = []
        for cid, res in (n.get("Containers") or {}).items():
            containers.append(
                models.NetworkContainer(
                    id=cid,
                    name=res.get("Name", ""),
                    ipv4_address=res.get("IPv4Address", ""),
                )
            )
        if containers:
            out.containers = containers
        return out

    async def connect_container(self, network_id: str, container_id: str) -> None:
        await self._request(
            "POST",
            f"/networks/{network_id}/connect",
            json_body={"Container": container_id},
        )

    async def disconnect_container(
        self, network_id: str, container_id: str, force: bool
    ) -> None:
        await self._request(
            "POST",
            f"/networks/{network_id}/disconnect",
            json_body={"Container": container_id, "Force": force},
        )

    async def remove_network(self, id: str) -> None:
        await self._request("DELETE", f"/networks/{id}")

    # -- system / prune -----------------------------------------------------

    async def system_info(self) -> models.SystemInfo:
        info = await self._request_json("GET", "/info")
        os_name = info.get("OperatingSystem") or info.get("OSType", "")
        return models.SystemInfo(
            containers=info.get("Containers", 0),
            containers_running=info.get("ContainersRunning", 0),
            images=info.get("Images", 0),
            server_version=info.get("ServerVersion", ""),
            os=os_name,
            arch=info.get("Architecture", ""),
            mem_total=info.get("MemTotal", 0),
            ncpu=info.get("NCPU", 0),
            insecure_registries=_insecure_registries(info.get("RegistryConfig") or {}),
        )

    async def disk_usage(self) -> models.DiskUsage:
        du = await self._request_json("GET", "/system/df")
        return _disk_usage_from_engine(du or {})

    async def prune_containers(self) -> int:
        report = await self._request_json("POST", "/containers/prune")
        return (report or {}).get("SpaceReclaimed", 0)

    async def prune_images(self, all: bool) -> int:
        filters = {"dangling": ["false"]} if all else {}
        params = {"filters": json.dumps(filters)} if filters else None
        report = await self._request_json("POST", "/images/prune", params=params)
        return (report or {}).get("SpaceReclaimed", 0)

    async def prune_volumes(self) -> int:
        report = await self._request_json("POST", "/volumes/prune")
        return (report or {}).get("SpaceReclaimed", 0)

    async def prune_networks(self) -> None:
        await self._request("POST", "/networks/prune")

    async def prune_build_cache(self) -> int:
        report = await self._request_json("POST", "/build/prune")
        return (report or {}).get("SpaceReclaimed", 0)

    # -- compose ------------------------------------------------------------

    async def compose_up(self, project: str, compose_yaml: str) -> str:
        path, cleanup = _write_temp_compose(compose_yaml)
        try:
            out, code = await _run_command(
                "docker", "compose", "-p", project, "-f", path, "up", "-d"
            )
        finally:
            cleanup()
        if code != 0:
            raise BadGateway(f"docker: compose up: {out}")
        return out

    async def compose_down(self, project: str) -> str:
        out, code = await _run_command("docker", "compose", "-p", project, "down")
        if code != 0:
            raise BadGateway(f"docker: compose down: {out}")
        return out

    # -- swarm --------------------------------------------------------------

    async def swarm_active(self) -> bool:
        info = await self._request_json("GET", "/info")
        return (info.get("Swarm") or {}).get("LocalNodeState") == "active"

    async def list_swarm_nodes(self) -> List[models.SwarmNode]:
        try:
            raw = await self._request_json("GET", "/nodes")
        except BadGateway as exc:
            # A host that is not a swarm manager answers /nodes with HTTP 503
            # ("This node is not a swarm manager"). Report an empty topology
            # rather than an error so the API can pair it with swarm_active().
            if _is_not_swarm_manager(exc):
                return []
            raise
        return _swarm_nodes_from_engine(raw)

    async def swarm_deploy(self, stack: str, compose_yaml: str) -> str:
        path, cleanup = _write_temp_compose(compose_yaml)
        try:
            out, code = await _run_command(
                "docker", "stack", "deploy", "-c", path, stack, "--detach=true"
            )
        finally:
            cleanup()
        if code != 0:
            raise BadGateway(f"docker: stack deploy: {out}")
        return out

    async def swarm_remove(self, stack: str) -> str:
        out, code = await _run_command("docker", "stack", "rm", stack)
        if code != 0:
            raise BadGateway(f"docker: stack rm: {out}")
        return out

    async def update_service_image(self, stack: str, service_id: str, image: str, registry_auth: str) -> None:
        cur = await self._request_json("GET", f"/services/{service_id}")
        spec = cur.get("Spec") or {}
        spec.setdefault("TaskTemplate", {}).setdefault("ContainerSpec", {})["Image"] = image
        version = (cur.get("Version") or {}).get("Index", 0)
        headers = {"X-Registry-Auth": registry_auth} if registry_auth else None
        await self._request("POST", f"/services/{service_id}/update", params={"version": str(version)},
                            json_body=spec, headers=headers)

    # -- internal: container create -----------------------------------------

    async def _create_container(self, name: str, spec: Any, start: bool) -> str:
        # Force-remove any pre-existing container with the same name (best effort).
        # Only when a name is given: an empty name makes the URL "/containers//json"
        # collapse to the LIST endpoint "/containers/json" (returns a list, not an
        # inspect object), and Docker auto-generates a name anyway.
        if name:
            try:
                existing = await self._request_json("GET", f"/containers/{name}/json")
                if isinstance(existing, dict) and existing.get("Id"):
                    await self._request(
                        "DELETE", f"/containers/{existing['Id']}", params={"force": "1"}
                    )
            except NotFound:
                pass

        if getattr(spec, "pull_image", False):
            stream = await self.pull_image(spec.image, "")
            try:
                async for _ in stream:
                    pass
            finally:
                await stream.close()

        # extra_args carries arbitrary ``docker run`` flags that have no
        # Engine-API JSON equivalent, so specs that set it must be created by
        # shelling out to the CLI. This is why a second, CLI-based spec encoder
        # (_create_container_via_run) exists alongside this Engine-API path —
        # do NOT collapse the two: only the CLI can honour extra_args.
        if getattr(spec, "extra_args", None):
            return await self._create_container_via_run(name, spec, start)

        env = [f"{kv.key}={kv.value}" for kv in (spec.env or [])]

        exposed: Dict[str, Any] = {}
        bindings: Dict[str, Any] = {}
        for p in spec.ports or []:
            proto = p.protocol or "tcp"
            key = f"{p.container_port}/{proto}"
            exposed[key] = {}
            bindings.setdefault(key, []).append({"HostPort": str(p.host_port)})

        binds = []
        for v in spec.volumes or []:
            bind = f"{v.source}:{v.target}"
            if v.read_only:
                bind += ":ro"
            binds.append(bind)

        host_cfg: Dict[str, Any] = {
            "PortBindings": bindings,
            "Binds": binds,
            "Privileged": bool(getattr(spec, "privileged", False)),
        }
        if getattr(spec, "restart_policy", ""):
            host_cfg["RestartPolicy"] = {"Name": spec.restart_policy}

        if getattr(spec, "cpu_limit", 0):
            host_cfg["NanoCpus"] = int(spec.cpu_limit * 1e9)
        if getattr(spec, "memory_limit_mb", 0):
            host_cfg["Memory"] = spec.memory_limit_mb * 1024 * 1024
        if getattr(spec, "memory_reservation_mb", 0):
            host_cfg["MemoryReservation"] = spec.memory_reservation_mb * 1024 * 1024

        if getattr(spec, "gpus", ""):
            count = -1
            if spec.gpus != "all":
                try:
                    count = int(spec.gpus)
                except ValueError as exc:
                    raise Validation(
                        f"docker: invalid gpus value {spec.gpus!r}"
                    ) from exc
            host_cfg["DeviceRequests"] = [
                {"Driver": "nvidia", "Count": count, "Capabilities": [["gpu"]]}
            ]

        cfg: Dict[str, Any] = {
            "Image": spec.image,
            "Env": env,
            "Cmd": list(spec.command or []),
            "Entrypoint": list(spec.entrypoint or []) or None,
            "WorkingDir": getattr(spec, "working_dir", ""),
            "User": getattr(spec, "user", ""),
            "Labels": dict(spec.labels or {}),
            "ExposedPorts": exposed,
            "Tty": bool(getattr(spec, "tty", False)),
            "OpenStdin": bool(getattr(spec, "open_stdin", False)),
            "HostConfig": host_cfg,
        }

        networks = list(spec.networks or [])
        if networks:
            # Attach only the FIRST network at create (via NetworkMode). Before
            # API 1.44 the Engine rejects more than one endpoint in
            # EndpointsConfig on POST /containers/create ("Container cannot be
            # connected to network endpoints ..."); we may negotiate 1.44+ now,
            # but attaching one network at create and connecting the rest
            # explicitly below (before start) works on every version, so we keep
            # the single path rather than branching on the negotiated version.
            host_cfg["NetworkMode"] = networks[0]

        created = await self._request_json(
            "POST", "/containers/create", params={"name": name}, json_body=cfg
        )
        cid = created["Id"]
        # Connect any additional networks before starting, so the container comes
        # up already attached to all of them.
        for extra in networks[1:]:
            await self.connect_container(extra, cid)
        if start:
            await self._request("POST", f"/containers/{cid}/start")
        return cid

    async def _create_container_via_run(self, name: str, spec: Any, start: bool) -> str:
        args = ["run", "-d", "--name", name]
        for kv in spec.env or []:
            args += ["-e", f"{kv.key}={kv.value}"]
        for p in spec.ports or []:
            proto = p.protocol or "tcp"
            args += ["-p", f"{p.host_port}:{p.container_port}/{proto}"]
        for v in spec.volumes or []:
            bind = f"{v.source}:{v.target}"
            if v.read_only:
                bind += ":ro"
            args += ["-v", bind]
        for n in spec.networks or []:
            args += ["--network", n]
        if getattr(spec, "restart_policy", ""):
            args += ["--restart", spec.restart_policy]
        if getattr(spec, "working_dir", ""):
            args += ["-w", spec.working_dir]
        if getattr(spec, "user", ""):
            args += ["-u", spec.user]
        if spec.entrypoint:
            args += ["--entrypoint", " ".join(spec.entrypoint)]
        if getattr(spec, "cpu_limit", 0):
            # Shortest decimal form (2.0 -> "2", 0.5 -> "0.5").
            args += ["--cpus", f"{spec.cpu_limit:g}"]
        if getattr(spec, "memory_limit_mb", 0):
            args += ["--memory", f"{spec.memory_limit_mb}m"]
        if getattr(spec, "memory_reservation_mb", 0):
            args += ["--memory-reservation", f"{spec.memory_reservation_mb}m"]
        if getattr(spec, "privileged", False):
            args += ["--privileged"]
        if getattr(spec, "gpus", ""):
            args += ["--gpus", spec.gpus]
        if getattr(spec, "open_stdin", False):
            args += ["-i"]
        if getattr(spec, "tty", False):
            args += ["-t"]
        for k, v in (spec.labels or {}).items():
            args += ["--label", f"{k}={v}"]

        args += list(spec.extra_args or [])
        args.append(spec.image)
        args += list(spec.command or [])

        out, code = await _run_command("docker", *args)
        if code != 0:
            raise BadGateway(f"docker: run {name}: {out}")
        cid = _last_non_empty_line(out)
        if not start and cid:
            await self._request("POST", f"/containers/{cid}/stop")
        return cid

    # -- internal: build ----------------------------------------------------

    async def _build_image(self, opts: BuildOptions) -> BuildResult:
        res = BuildResult()
        if not (opts.tag or "").strip():
            raise Validation("docker: build image: tag is required")
        if not opts.git_url and not (opts.dockerfile or "").strip():
            raise Validation(
                "docker: build image: a Git URL or an inline Dockerfile is required"
            )

        tmp = tempfile.mkdtemp(prefix="kowan-build-")
        output = io.StringIO()
        dockerfile_path = ""
        try:
            if opts.git_url:
                # Same rules as deployment clones (kowan.deployment.gitsource):
                # a validated URL and ref, "--" before the URL so it can never
                # be read as an option, and only network git protocols.
                from ..deployment.gitsource import ALLOWED_PROTOCOLS, validate_repo_url

                try:
                    git_url = validate_repo_url(opts.git_url)
                except ValueError as err:
                    raise Validation(str(err)) from None
                if opts.git_ref and (opts.git_ref.startswith("-") or any(c.isspace() for c in opts.git_ref)):
                    raise Validation("invalid git ref")
                src_dir = os.path.join(tmp, "src")
                clone_args = ["clone", "--depth", "1"]
                if opts.git_ref:
                    clone_args += ["--branch", opts.git_ref]
                clone_args += ["--", git_url, src_dir]
                git_env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_ALLOW_PROTOCOL=ALLOWED_PROTOCOLS)
                clone_out, clone_code = await _run_command("git", *clone_args, env=git_env)
                output.write(clone_out)
                if clone_code != 0:
                    res.output = output.getvalue()
                    raise BadGateway(f"docker: build image: git clone: {clone_out}")
                build_context = _contained(src_dir, opts.context_subdir)
            else:
                dockerfile_path = os.path.join(tmp, "Dockerfile")
                with open(dockerfile_path, "w", encoding="utf-8") as f:
                    f.write(opts.dockerfile)
                build_context = _contained(tmp, opts.context_subdir)

            build_args = ["build", "-t", opts.tag]
            if opts.no_cache:
                build_args.append("--no-cache")
            for k, v in (opts.build_args or {}).items():
                build_args += ["--build-arg", f"{k}={v}"]
            if dockerfile_path:
                build_args += ["-f", dockerfile_path]
            build_args.append(build_context)

            build_out, build_code = await _run_command("docker", *build_args)
            output.write(build_out)
            if build_code != 0:
                res.output = output.getvalue()
                return res

            id_out, id_code = await _run_command("docker", "images", "-q", opts.tag)
            if id_code == 0:
                res.image_id = _last_non_empty_line(id_out)

            res.success = True

            if opts.push:
                if opts.registry_host or opts.registry_user:
                    login_args = ["login", opts.registry_host, "-u", opts.registry_user, "--password-stdin"]
                    login_out, login_code = await _run_command(
                        "docker", *[a for a in login_args if a != ""],
                        stdin=opts.registry_pass.encode("utf-8"),
                    )
                    output.write(login_out)
                    if login_code != 0:
                        res.success = False
                        res.output = output.getvalue()
                        return res
                push_out, push_code = await _run_command("docker", "push", opts.tag)
                output.write(push_out)
                if push_code != 0:
                    res.success = False
                    res.output = output.getvalue()
                    return res

            res.output = output.getvalue()
            return res
        finally:
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)

    # -- internal: volume helper container ----------------------------------

    async def _any_local_image(self) -> str:
        imgs = await self._request_json("GET", "/images/json", params={"all": "0"})
        for im in imgs or []:
            for t in im.get("RepoTags") or []:
                if t and t != "<none>:<none>":
                    return t
        for im in imgs or []:
            if im.get("Id"):
                return im["Id"]
        raise Validation(
            "no image available on the host to browse the volume; pull any image first"
        )

    async def _create_volume_helper(self, name: str, read_only: bool):
        """Create the inert helper container; return (id, async remove func)."""
        # The name becomes a bind spec: an absolute path (``/``, ``/etc``) would
        # bind-mount the HOST filesystem into the helper instead of a volume.
        if not _VOLUME_NAME_RE.match(name or ""):
            raise Validation("invalid volume name")
        bind = f"{name}:{_VOLUME_MOUNT_BASE}"
        if read_only:
            bind += ":ro"

        async def _create(img: str) -> Any:
            return await self._request_json(
                "POST",
                "/containers/create",
                json_body={
                    "Image": img,
                    "Labels": {models.LabelSupport: "volume-browse"},
                    "HostConfig": {"Binds": [bind]},
                },
            )

        # Reuse the helper image picked last time: GET /images/json is slow on
        # some engines (~0.6s with the containerd image store) and was paid on
        # every click while browsing a volume. If that image has since been
        # removed, the create 404s and we re-pick once.
        img = self._helper_image
        created = None
        if img:
            try:
                created = await _create(img)
            except NotFound:
                created = None
        if created is None:
            img = await self._any_local_image()
            created = await _create(img)
        self._helper_image = img
        cid = created["Id"]

        async def _remove() -> None:
            try:
                await self._request(
                    "DELETE", f"/containers/{cid}", params={"force": "1"}
                )
            except Exception:
                pass

        return cid, _remove

    async def _open_volume_archive(self, name: str, clean: str) -> Tuple[Any, Callable[[], Awaitable[None]]]:
        """docker-cp OUT of a read-only helper: the archive stream of ``clean``
        and a close() that releases it and removes the helper."""
        cid, remove = await self._create_volume_helper(name, read_only=True)
        try:
            src = await self._open_stream(
                "GET", f"/containers/{cid}/archive", params={"path": _VOLUME_MOUNT_BASE + clean}
            )
        except BaseException:
            await remove()
            raise

        async def close() -> None:
            try:
                await src.close()
            finally:
                await remove()

        return src, close

    async def _with_volume_archive(self, name: str, clean: str, action: Callable[[Any], Awaitable[Any]]) -> Any:
        src, close = await self._open_volume_archive(name, clean)
        try:
            return await action(src)
        finally:
            await close()

    # -- container filesystem (archive API; helpers in containerfs.py) -------
    # Everything goes through GET/PUT/HEAD /containers/{id}/archive so it works
    # on stopped containers and on images without a shell. exec is only used as
    # a fast path to list a running container's directory (with ls) and for
    # delete/rename, which the archive API cannot express.

    async def _fs_call(
        self, method: str, id: str, path: str, *, data: Any = None
    ) -> Tuple[int, Any, bytes]:
        """One archive request; returns (status, headers, body) with typed errors.

        Engine 400 -> Validation, 403 (read-only rootfs/volume) and 409 ->
        Conflict, 404 -> NotFound, anything else >= 400 -> BadGateway.
        """
        from ..errors import Conflict

        session = await self._get_session()
        url = self._url(f"/containers/{id}/archive")
        headers = {"Content-Type": "application/x-tar"} if data is not None else None
        try:
            async with session.request(
                method, url, params={"path": path}, data=data, headers=headers
            ) as resp:
                status = resp.status
                hdrs = resp.headers
                body = await resp.read() if method != "HEAD" else b""
        except aiohttp.ClientError as exc:
            raise BadGateway(f"docker: {method} archive {path}: {exc}") from exc
        if status < 400:
            return status, hdrs, body
        msg = ""
        try:
            doc = json.loads(body) if body else {}
            msg = doc.get("message", "") if isinstance(doc, dict) else ""
        except Exception:  # noqa: BLE001
            msg = body.decode("utf-8", "replace")
        if status == 404:
            raise NotFound(msg or f"no such file or directory: {path}")
        if status == 400:
            raise Validation(msg or "invalid path")
        if status in (403, 409):
            raise Conflict(msg or "the target filesystem is read-only")
        raise BadGateway(f"docker: {method} archive {path}: {status} {msg}".strip())

    async def container_path_stat(self, id: str, path: str) -> Dict[str, Any]:
        from . import containerfs

        clean = containerfs.normalize_path(path)
        _, hdrs, _ = await self._fs_call("HEAD", id, clean)
        st = containerfs.decode_path_stat(hdrs.get("X-Docker-Container-Path-Stat", ""))
        return containerfs.entry_from_stat(clean, st)

    async def _fs_resolve(self, id: str, path: str) -> Tuple[str, Dict[str, Any]]:
        """Stat ``path``, following symlinks (at most 8 hops)."""
        from . import containerfs

        cur = containerfs.normalize_path(path)
        for _ in range(8):
            st = await self.container_path_stat(id, cur)
            if not st.get("isLink"):
                return cur, st
            cur = containerfs.resolve_link(cur, st.get("linkTarget") or "")
        raise Validation("too many levels of symbolic links")

    async def container_list_dir(self, id: str, path: str, limit: int) -> Dict[str, Any]:
        from . import containerfs

        limit = max(1, min(int(limit or 1000), 10000))
        resolved, st = await self._fs_resolve(id, path)
        if not st.get("isDir"):
            raise Validation("not a directory")
        info = await self._request_json("GET", f"/containers/{id}/json") or {}
        state = info.get("State") or {}
        if state.get("Running") and not state.get("Paused"):
            listed = await self._fs_list_exec(id, resolved, limit)
            if listed is not None:
                entries, truncated = listed
                return {"path": resolved, "entries": entries, "truncated": truncated, "method": "exec"}
        src = await self._open_stream("GET", f"/containers/{id}/archive", params={"path": resolved})
        try:
            entries, truncated = await containerfs.list_tar_children(
                src, resolved, limit=limit, max_scan_bytes=containerfs.MAX_SCAN_BYTES
            )
        finally:
            await src.close()
        return {"path": resolved, "entries": entries, "truncated": truncated, "method": "archive"}

    async def _fs_list_exec(
        self, id: str, dir_path: str, limit: int
    ) -> Optional[Tuple[List[Dict[str, Any]], bool]]:
        """List via ``ls -1Ap`` + one path-stat per entry; None when ls is unusable."""
        from . import containerfs

        try:
            res = await self.container_exec_status(id, ["ls", "-1Ap", "--", dir_path], "0")
        except Exception:  # noqa: BLE001 - no exec support / no ls: fall back to tar
            return None
        if int(res.get("exitCode", 1)) != 0:
            return None
        names = [ln for ln in str(res.get("output", "")).split("\n") if ln and ln not in ("./", "../")]
        truncated = len(names) > limit
        names = sorted(names, key=lambda n: (n.lower(), n))[:limit]
        sem = asyncio.Semaphore(16)

        async def one(raw: str) -> Dict[str, Any]:
            is_dir = raw.endswith("/")
            name = raw[:-1] if is_dir else raw
            p = containerfs.join(dir_path, name)
            async with sem:
                try:
                    return await self.container_path_stat(id, p)
                except Exception:  # noqa: BLE001 - vanished / unreadable: minimal row
                    return {
                        "name": name, "path": p, "size": 0, "mode": "",
                        "kind": "dir" if is_dir else "file", "isDir": is_dir,
                        "isLink": False, "linkTarget": "", "modTime": 0,
                    }

        try:
            entries = list(await asyncio.gather(*(one(n) for n in names)))
        except Exception:  # noqa: BLE001
            return None
        containerfs.sort_entries(entries)
        return entries, truncated

    async def container_read_file(self, id: str, path: str, max_bytes: int) -> bytes:
        from . import containerfs
        from ..errors import AppError

        resolved, st = await self._fs_resolve(id, path)
        if st.get("kind") != "file":
            raise Validation("not a regular file")
        if int(st.get("size") or 0) > max_bytes:
            raise AppError(
                f"file is too large to open ({st.get('size')} bytes, limit {max_bytes})",
                status=413,
            )
        src = await self._open_stream("GET", f"/containers/{id}/archive", params={"path": resolved})
        try:
            h, data = await containerfs.first_regular_file(src, max_bytes)
        finally:
            await src.close()
        if h is None:
            raise Validation("not a regular file")
        return data

    async def container_get_archive(self, id: str, path: str) -> AsyncByteStream:
        from . import containerfs

        clean = containerfs.normalize_path(path)
        try:
            return await self._open_stream(
                "GET", f"/containers/{id}/archive", params={"path": clean}
            )
        except NotFound:
            raise NotFound(f"no such file or directory: {clean}")

    async def container_put_archive(self, id: str, path: str, content: AsyncByteStream) -> None:
        from . import containerfs

        clean = containerfs.normalize_path(path)

        async def gen() -> AsyncIterator[bytes]:
            read = getattr(content, "read", None)
            if callable(read):
                while True:
                    chunk = await read(64 * 1024)
                    if not chunk:
                        return
                    yield chunk
            else:
                async for chunk in content:  # type: ignore[union-attr]
                    if chunk:
                        yield chunk

        await self._fs_call("PUT", id, clean, data=gen())

    async def container_exec_status(
        self, id: str, cmd: List[str], user: str = ""
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"AttachStdout": True, "AttachStderr": True, "Cmd": list(cmd)}
        if user:
            body["User"] = user
        created = await self._request_json("POST", f"/containers/{id}/exec", json_body=body)
        exec_id = created["Id"]
        stream = await self._open_stream(
            "POST",
            f"/exec/{exec_id}/start",
            json_body={"Detach": False, "Tty": False},
            headers={"Content-Type": "application/json"},
        )
        try:
            data = await _demux_stdcopy(stream)
        finally:
            await stream.close()
        insp = await self._request_json("GET", f"/exec/{exec_id}/json") or {}
        code = insp.get("ExitCode")
        return {
            "exitCode": int(code) if isinstance(code, int) else -1,
            "output": data.decode("utf-8", "replace"),
        }

    async def container_relabel(
        self,
        id: str,
        set_labels: Dict[str, str],
        remove_labels: List[str],
        connect_networks: List[str],
        disconnect_networks: List[str],
    ) -> str:
        """Recreate ``id`` with changed labels/networks (raw clone, rollback-safe).

        Steps: inspect -> rename the original out of the way -> stop it ->
        create the clone under the original name (same Config/HostConfig, new
        labels) -> reattach networks (keeping aliases / static IPs) -> start if
        it was running -> remove the original. Any failure removes the clone,
        renames the original back and restarts it, then re-raises.
        """
        info = await self._request_json("GET", f"/containers/{id}/json")
        if not isinstance(info, dict):
            raise BadGateway(f"docker: inspect {id}: unexpected response")
        old_id = str(info.get("Id") or id)
        name = str(info.get("Name") or "").lstrip("/")
        if not name:
            raise Validation("container has no name")
        config = dict(info.get("Config") or {})
        host_config = dict(info.get("HostConfig") or {})
        state = info.get("State") or {}
        was_running = bool(state.get("Running"))
        nets = dict((info.get("NetworkSettings") or {}).get("Networks") or {})
        net_mode = str(host_config.get("NetworkMode") or "")

        labels = dict(config.get("Labels") or {})
        for k in remove_labels or []:
            labels.pop(k, None)
        labels.update({str(k): str(v) for k, v in (set_labels or {}).items()})
        config["Labels"] = labels
        if config.get("Hostname") == old_id[:12]:
            config.pop("Hostname", None)
        config.pop("MacAddress", None)
        body: Dict[str, Any] = dict(config)
        body["HostConfig"] = host_config

        drop = set(disconnect_networks or [])
        targets: Dict[str, Dict[str, Any]] = {}
        for net, ep in nets.items():
            if net in drop or not net:
                continue
            ep = ep or {}
            cfg: Dict[str, Any] = {}
            aliases = [a for a in (ep.get("Aliases") or []) if a and a != old_id[:12]]
            if aliases:
                cfg["Aliases"] = aliases
            if ep.get("IPAMConfig"):
                cfg["IPAMConfig"] = ep["IPAMConfig"]
            targets[net] = cfg
        for net in connect_networks or []:
            if net and net not in targets:
                targets[net] = {}
        if net_mode in drop:
            raise Validation("cannot detach the container's primary network")

        tmp_name = f"{name}-kowan-old-{int(time.time())}"
        await self._request("POST", f"/containers/{old_id}/rename", params={"name": tmp_name})
        new_id = ""
        try:
            if was_running:
                await self._request("POST", f"/containers/{old_id}/stop", params={"t": "10"})
            primary = net_mode if net_mode not in ("", "default") else "bridge"
            shared_ns = net_mode.startswith("container:") or net_mode in ("host", "none")
            if not shared_ns and primary in targets:
                # Keep the primary network's aliases (compose service names).
                body["NetworkingConfig"] = {"EndpointsConfig": {primary: targets[primary]}}
            created = await self._request_json(
                "POST", "/containers/create", params={"name": name}, json_body=body
            )
            new_id = str((created or {}).get("Id") or "")
            for net, cfg in targets.items():
                if shared_ns or net == primary:
                    continue
                ep_body: Dict[str, Any] = {"Container": new_id}
                if cfg:
                    ep_body["EndpointConfig"] = cfg
                await self._request("POST", f"/networks/{net}/connect", json_body=ep_body)
            if was_running:
                await self._request("POST", f"/containers/{new_id}/start")
        except BaseException:
            if new_id:
                try:
                    await self._request("DELETE", f"/containers/{new_id}", params={"force": "true"})
                except Exception:  # noqa: BLE001
                    log.warning("relabel rollback: remove clone %s failed", new_id)
            try:
                await self._request("POST", f"/containers/{old_id}/rename", params={"name": name})
                if was_running:
                    await self._request("POST", f"/containers/{old_id}/start")
            except Exception:  # noqa: BLE001
                log.error("relabel rollback: restore %s (%s) failed", name, old_id)
            raise
        try:
            await self._request("DELETE", f"/containers/{old_id}", params={"force": "true"})
        except Exception:  # noqa: BLE001 - the clone is live; a leftover is cosmetic
            log.warning("relabel: remove original %s failed", old_id)
        return new_id

# ---------------------------------------------------------------------------
# Demux + stats stream wrappers
# ---------------------------------------------------------------------------


class _DemuxStream:
    """Strips Docker's stdout/stderr multiplexing framing on the fly.

    Wraps the raw log :class:`_ResponseStream`. The Engine frames non-TTY output
    as ``[stream 0 0 0 size(4 BE)]`` + payload; a TTY container emits raw bytes
    with no header. We parse headers incrementally; if the first bytes don't look
    like a valid frame header (stream byte in 0..2) we pass everything through
    raw, matching how ``stdcopy`` degrades on TTY streams.
    """

    def __init__(self, src: _ResponseStream) -> None:
        self._src = src
        self._buf = bytearray()
        self._eof = False
        self._raw = None  # unknown until first bytes arrive

    async def _fill(self) -> None:
        chunk = await self._src.read(64 * 1024)
        if not chunk:
            self._eof = True
        else:
            self._buf.extend(chunk)

    async def read(self, n: int = -1) -> bytes:
        out = bytearray()
        while True:
            while not self._eof and len(self._buf) < 8:
                await self._fill()
            if self._raw is None and self._buf:
                self._raw = self._buf[0] not in (0, 1, 2)
            if self._raw:
                if self._buf:
                    out.extend(self._buf)
                    self._buf.clear()
                if self._eof and not out:
                    return b""
                if out or self._eof:
                    return bytes(out)
                continue
            # Framed path.
            if len(self._buf) < 8:
                # EOF with a partial/no header: emit any leftover raw.
                if out:
                    return bytes(out)
                return bytes(self._buf) if self._buf else b""
            size = struct.unpack(">I", bytes(self._buf[4:8]))[0]
            while not self._eof and len(self._buf) < 8 + size:
                await self._fill()
            payload = bytes(self._buf[8 : 8 + size])
            del self._buf[: 8 + size]
            out.extend(payload)
            if out:
                return bytes(out)
            if self._eof and not self._buf:
                return b""

    async def __aiter__(self) -> AsyncIterator[bytes]:  # type: ignore[override]
        while True:
            chunk = await self.read()
            if not chunk:
                return
            yield chunk

    async def close(self) -> None:
        await self._src.close()


class _StatsNDJSONStream:
    """Re-emits Docker's streaming stats as newline-delimited normalized ContainerStats.

    Decodes each JSON stats sample from
    the Engine stream and yields our ``ContainerStats`` JSON + ``\\n`` (NDJSON),
    so an accurate per-second CPU percentage is computed from consecutive samples.
    """

    def __init__(self, src: _ResponseStream) -> None:
        self._src = src
        self._buf = bytearray()
        self._out = bytearray()
        self._eof = False

    async def _produce(self) -> None:
        # Read raw bytes, split on newlines (Engine emits one JSON object per line
        # in stream mode), and convert each into our NDJSON line.
        while not self._out and not self._eof:
            chunk = await self._src.read(64 * 1024)
            if not chunk:
                self._eof = True
                break
            self._buf.extend(chunk)
            while True:
                nl = self._buf.find(b"\n")
                if nl < 0:
                    break
                line = bytes(self._buf[:nl])
                del self._buf[: nl + 1]
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = jsonx.loads(line)
                except ValueError:
                    continue
                stats = _stats_from_response(raw)
                self._out.extend(jsonx.dumps(stats.model_dump(by_alias=True)))
                self._out.append(0x0A)

    async def read(self, n: int = -1) -> bytes:
        await self._produce()
        if n is None or n < 0:
            data = bytes(self._out)
            self._out.clear()
            return data
        data = bytes(self._out[:n])
        del self._out[:n]
        return data

    async def __aiter__(self) -> AsyncIterator[bytes]:  # type: ignore[override]
        while True:
            chunk = await self.read()
            if not chunk:
                return
            yield chunk

    async def close(self) -> None:
        await self._src.close()


# ---------------------------------------------------------------------------
# Mapping helpers (Engine JSON -> kowan.models)
# ---------------------------------------------------------------------------


def _volume_from_engine(v: Dict[str, Any]) -> models.Volume:
    return models.Volume(
        name=v.get("Name", ""),
        driver=v.get("Driver", ""),
        mountpoint=v.get("Mountpoint", ""),
        created_at=v.get("CreatedAt", ""),
        labels=v.get("Labels") or None,
        options=v.get("Options") or None,
    )


def _insecure_registries(cfg: Dict[str, Any]) -> List[str]:
    """The registries ``/info``'s RegistryConfig marks as plain HTTP: the
    named ones (IndexConfigs with Secure false) and the CIDR ranges."""
    names = [name for name, idx in (cfg.get("IndexConfigs") or {}).items()
             if isinstance(idx, dict) and idx.get("Secure") is False]
    return sorted(names) + [str(c) for c in (cfg.get("InsecureRegistryCIDRs") or [])]


def _is_not_swarm_manager(exc: BadGateway) -> bool:
    """Recognise the Engine "not a swarm manager" 503 (mapped to BadGateway).

    ``_engine_error`` folds a 503 into a :class:`BadGateway` whose message keeps
    both the status and the Engine text (``... 503 This node is not a swarm
    manager``), so match on either signal and swallow only this case.
    """
    msg = str(getattr(exc, "message", "") or exc).lower()
    return "not a swarm manager" in msg or " 503 " in f" {msg} "


def _swarm_node_from_engine(n: Dict[str, Any]) -> models.SwarmNode:
    """Flatten one Engine ``/nodes`` entry into a :class:`models.SwarmNode`.

    Every nested block is optional — workers carry no ``ManagerStatus`` and a
    down node may lack ``Engine`` — so read defensively, defaulting missing
    values to ""/False. The node address prefers ``Status.Addr`` and falls back
    to the manager's advertised address.
    """
    n = n or {}
    desc = n.get("Description") or {}
    spec = n.get("Spec") or {}
    status = n.get("Status") or {}
    mgr = n.get("ManagerStatus") or {}
    engine = desc.get("Engine") or {}
    return models.SwarmNode(
        id=n.get("ID", "") or "",
        hostname=desc.get("Hostname", "") or "",
        role=spec.get("Role", "") or "",
        leader=bool(mgr.get("Leader", False)),
        availability=spec.get("Availability", "") or "",
        status=status.get("State", "") or "",
        address=(status.get("Addr") or mgr.get("Addr") or ""),
        engine_version=engine.get("EngineVersion", "") or "",
    )


def _swarm_nodes_from_engine(raw: Any) -> List[models.SwarmNode]:
    """Map the Engine ``/nodes`` array (a list of Node objects) to SwarmNodes."""
    out: List[models.SwarmNode] = []
    for n in raw or []:
        if n is None:
            continue
        out.append(_swarm_node_from_engine(n))
    return out


_EXITED = re.compile(r"Exited \((-?\d+)\)")


def _exit_code(status: str) -> Optional[int]:
    """The exit code in a list status ("Exited (1) 2 hours ago"), if any."""
    m = _EXITED.search(status or "")
    return int(m.group(1)) if m else None


def _stats_from_response(v: Dict[str, Any]) -> models.ContainerStats:
    """Normalize a raw Engine stats sample."""
    cpu = _calculate_cpu_percent(v)
    mem_usage = _memory_usage(v)
    mem_stats = v.get("memory_stats") or {}
    mem_limit = mem_stats.get("limit", 0) or 0
    pids_stats = v.get("pids_stats") or {}

    stats = models.ContainerStats(
        cpu_percent=cpu,
        mem_usage=mem_usage,
        mem_limit=mem_limit,
        pids=pids_stats.get("current", 0) or 0,
    )
    if mem_limit > 0:
        stats.mem_percent = float(mem_usage) / float(mem_limit) * 100.0

    for n in (v.get("networks") or {}).values():
        stats.net_rx += n.get("rx_bytes", 0) or 0
        stats.net_tx += n.get("tx_bytes", 0) or 0

    blkio = v.get("blkio_stats") or {}
    for e in blkio.get("io_service_bytes_recursive") or []:
        op = (e.get("op") or "").lower()
        if op == "read":
            stats.blk_read += e.get("value", 0) or 0
        elif op == "write":
            stats.blk_write += e.get("value", 0) or 0

    return stats


def _calculate_cpu_percent(v: Dict[str, Any]) -> float:
    """CPU% as a share of the whole host: 100 is every core busy.

    ``system_cpu_usage`` already counts all the host's cores, so the ratio of
    the two deltas is that share. ``docker stats`` multiplies it by
    ``online_cpus`` (100 per core, 400 on a busy 4-core host); Kowan does not,
    so container, workload and host figures share one 0-100 scale."""
    cpu_stats = v.get("cpu_stats") or {}
    pre = v.get("precpu_stats") or {}
    cpu_usage = cpu_stats.get("cpu_usage") or {}
    pre_usage = pre.get("cpu_usage") or {}

    cpu_delta = float(cpu_usage.get("total_usage", 0) or 0) - float(
        pre_usage.get("total_usage", 0) or 0
    )
    system_delta = float(cpu_stats.get("system_cpu_usage", 0) or 0) - float(
        pre.get("system_cpu_usage", 0) or 0
    )

    if system_delta > 0.0 and cpu_delta > 0.0:
        return cpu_delta / system_delta * 100.0
    return 0.0


def _memory_usage(v: Dict[str, Any]) -> int:
    """Working-set memory usage."""
    mem = v.get("memory_stats") or {}
    usage = mem.get("usage", 0) or 0
    stats = mem.get("stats") or {}
    cache = stats.get("cache")
    if cache is not None and cache <= usage:
        return usage - cache
    inactive = stats.get("inactive_file")
    if inactive is not None and inactive <= usage:
        return usage - inactive
    return usage


def _disk_usage_from_engine(du: Dict[str, Any]) -> models.DiskUsage:
    """Compute a ``docker system df`` breakdown."""
    layers_size = du.get("LayersSize", 0) or 0
    out = models.DiskUsage(layers_size=layers_size)

    images = du.get("Images") or []
    out.images_count = len(images)
    out.images_size = layers_size
    for img in images:
        if img is None:
            continue
        if (img.get("Containers", 0) or 0) == 0:
            size = (img.get("Size", 0) or 0) - (img.get("SharedSize", 0) or 0)
            if size < 0:
                size = 0
            out.images_reclaimable += size
            tags = [t for t in img.get("RepoTags") or [] if t and t != "<none>:<none>"]
            out.unused_images.append(models.DiskItem(
                id=img.get("Id", ""), name=", ".join(tags), size=size, created=int(img.get("Created", 0) or 0)))

    containers = du.get("Containers") or []
    out.containers_count = len(containers)
    for ctr in containers:
        if ctr is None:
            continue
        out.containers_size += ctr.get("SizeRw", 0) or 0
        state = ctr.get("State", "") or ""
        if state not in ("running", "paused", "restarting"):
            out.stopped_containers.append(models.DiskItem(
                id=ctr.get("Id", ""), name=((ctr.get("Names") or [""])[0] or "").lstrip("/"),
                size=ctr.get("SizeRw", 0) or 0, image=ctr.get("Image", "") or "", state=state,
                labels=ctr.get("Labels") or {}, created=int(ctr.get("Created", 0) or 0)))

    volumes = du.get("Volumes") or []
    out.volumes_count = len(volumes)
    for vol in volumes:
        if vol is None:
            continue
        usage = vol.get("UsageData")
        if not usage:
            continue
        size = max(usage.get("Size", 0) or 0, 0)  # -1: not measured
        out.volumes_size += size
        if (usage.get("RefCount", 0) or 0) == 0:
            out.volumes_reclaimable += size
            out.unused_volumes.append(models.DiskItem(
                id=vol.get("Name", ""), name=vol.get("Name", ""), size=size, labels=vol.get("Labels") or {}))

    for bc in du.get("BuildCache") or []:
        if bc is None:
            continue
        out.build_cache_size += bc.get("Size", 0) or 0
        if not bc.get("InUse"):
            out.build_cache_reclaimable += bc.get("Size", 0) or 0

    return out


def _container_spec_from_inspect(insp: Dict[str, Any]) -> models.ContainerSpec:
    """Reconstruct a ContainerSpec from an inspect doc."""
    spec = models.ContainerSpec()

    cfg = insp.get("Config") or {}
    if cfg:
        spec.image = cfg.get("Image", "")
        spec.working_dir = cfg.get("WorkingDir", "")
        spec.user = cfg.get("User", "")
        spec.tty = cfg.get("Tty", False)
        spec.open_stdin = cfg.get("OpenStdin", False)
        spec.command = list(cfg.get("Cmd") or [])
        spec.entrypoint = list(cfg.get("Entrypoint") or [])

        for e in cfg.get("Env") or []:
            if "=" in e:
                k, val = e.split("=", 1)
                spec.env.append(models.KeyValue(key=k, value=val))
            else:
                spec.env.append(models.KeyValue(key=e))

        for k, val in (cfg.get("Labels") or {}).items():
            if k.startswith("kowan.") or k.startswith("com.docker.compose."):
                continue
            spec.labels[k] = val

    hc = insp.get("HostConfig") or {}
    if hc:
        for port_key, binds in (hc.get("PortBindings") or {}).items():
            # port_key is like "80/tcp"
            proto = "tcp"
            portnum = port_key
            if "/" in port_key:
                portnum, proto = port_key.split("/", 1)
            try:
                container_port = int(portnum)
            except ValueError:
                container_port = 0
            if not binds:
                spec.ports.append(
                    models.PortMapping(container_port=container_port, protocol=proto)
                )
                continue
            for b in binds:
                try:
                    host_port = int(b.get("HostPort", "") or 0)
                except ValueError:
                    host_port = 0
                spec.ports.append(
                    models.PortMapping(
                        host_port=host_port,
                        container_port=container_port,
                        protocol=proto,
                    )
                )

        restart = hc.get("RestartPolicy") or {}
        spec.restart_policy = restart.get("Name", "") or ""
        spec.privileged = hc.get("Privileged", False)

        if hc.get("NanoCpus", 0):
            spec.cpu_limit = float(hc["NanoCpus"]) / 1e9
        if hc.get("Memory", 0):
            spec.memory_limit_mb = hc["Memory"] // 1048576
        if hc.get("MemoryReservation", 0):
            spec.memory_reservation_mb = hc["MemoryReservation"] // 1048576

        for dr in hc.get("DeviceRequests") or []:
            if (dr.get("Count", 0) or 0) < 0:
                spec.gpus = "all"
            else:
                spec.gpus = str(dr.get("Count", 0) or 0)
            break

    seen_targets = set()
    for m in insp.get("Mounts") or []:
        dest = m.get("Destination", "")
        if not dest:
            continue
        source = m.get("Source", "")
        if m.get("Name"):
            source = m["Name"]
        spec.volumes.append(
            models.VolumeMapping(
                source=source, target=dest, read_only=not m.get("RW", True)
            )
        )
        seen_targets.add(dest)
    for bind in (hc.get("Binds") or []):
        vm = _parse_bind(bind)
        if vm is None:
            continue
        if vm.target in seen_targets:
            continue
        spec.volumes.append(vm)
        seen_targets.add(vm.target)

    netset = insp.get("NetworkSettings") or {}
    for name in (netset.get("Networks") or {}).keys():
        if name in ("none", "host"):
            continue
        spec.networks.append(name)

    return spec


# ---------------------------------------------------------------------------
# Compose temp-file helper
# ---------------------------------------------------------------------------


def _write_temp_compose(compose_yaml: str):
    """Write compose YAML to a temp file; return (path, cleanup)."""
    fd, path = tempfile.mkstemp(prefix="kowan-compose-", suffix=".yml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(compose_yaml)
    except Exception:
        os.unlink(path)
        raise

    def _cleanup() -> None:
        try:
            os.unlink(path)
        except OSError:
            pass

    return path, _cleanup
