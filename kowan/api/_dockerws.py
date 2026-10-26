"""Shared WebSocket + Docker-stream plumbing for the docker api modules.

The three docker-streaming api modules — ``exec``, ``logs`` and ``stats`` —
build on these helpers so they all share one framing, one client-disconnect
policy, and one close-handshake.

Design notes:

- :func:`prepare_ws` prepares an ``aiohttp.web.WebSocketResponse``. aiohttp's
  ``WebSocketResponse`` allows any origin (there is no built-in Origin check to
  disable); auth is done by the JWT middleware upstream.

- :func:`stream_lines` is an async coroutine that scans an
  :class:`~kowan.docker.base.AsyncByteStream` line-by-line and writes each frame
  as a text message. A ``transform`` callback rewrites each frame (used for
  Docker log-header stripping); ``None`` sends the raw line.

- A background ``asyncio.Task`` reads the socket only to notice a disconnect;
  when it sees a close/error it closes the underlying stream, which unblocks
  the read loop.

- :func:`strip_docker_log_header` removes the 8-byte multiplex header Docker
  prepends to log frames.

- :func:`resolve_ops` does the up-front ``Ops`` resolution every docker handler
  needs: read ``{hostID}`` from the path, ``store.get_host`` (404 on miss),
  ``provider.ops(host)`` (502 on failure). It raises the typed
  ``kowan.errors.*`` exceptions so the shared error middleware maps them — but for
  the WS handlers we call it BEFORE preparing the socket so those errors surface
  as ordinary HTTP responses (an upgraded connection can no longer send a status
  line).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, List, Optional

from aiohttp import WSMsgType, web

from ..docker.base import AsyncByteStream, Ops
from ..errors import BadGateway, NotFound, Validation
from ..web import SERVICES_KEY

log = logging.getLogger("kowan.api.dockerws")

# Maximum single line we buffer from a Docker stream before treating the line as
# too long (1 MiB).
_MAX_LINE = 1024 * 1024

# Seconds between WebSocket pings on browser-facing streams (see prepare_ws).
WS_HEARTBEAT = 30.0

# Chunk size for the exec byte pump (32 KiB).
_EXEC_CHUNK = 32 * 1024


def resolve_ops(request: web.Request) -> Ops:
    """Resolve the :class:`Ops` for the host addressed by ``{hostID}``.

    A missing host id is a 400, an
    unknown host is a 404, and a provider failure is a 502. On success returns
    the concrete ``Ops``. Raises the matching ``kowan.errors`` exception on
    failure; call this BEFORE preparing a WebSocket so the error can still be
    delivered as a normal HTTP response.
    """
    host_id = request.match_info.get("hostID", "")
    if not host_id:
        raise Validation("missing host id")

    svc = request.app[SERVICES_KEY]
    try:
        host = svc.store.get_host(host_id)
    except Exception as err:  # NotFoundError (or any store failure) -> 404
        raise NotFound("host not found") from err

    try:
        return svc.docker.ops(host)
    except Exception as err:
        raise BadGateway("resolve host client: " + str(err)) from err


async def prepare_ws(request: web.Request) -> web.WebSocketResponse:
    """Upgrade ``request`` to a WebSocket and return the prepared response.

    Any origin is accepted (auth is handled by the JWT
    middleware; the frontend is same-origin in production).
    """
    # heartbeat: ping every WS_HEARTBEAT s and drop the socket if no pong comes
    # back. Without it a half-open connection (laptop sleep, NAT/proxy drop)
    # kept a log-follow / stats stream — and its Docker request — open forever.
    ws = web.WebSocketResponse(heartbeat=WS_HEARTBEAT)
    await ws.prepare(request)
    from ..web.wsregistry import track

    return track(request, ws)


async def _write_close_error(ws: web.WebSocketResponse, message: str) -> None:
    """Send the ``error: <msg>`` text frame + internal-error close.

    On a failed stream start the
    client gets a human-readable text frame followed by a close with the
    internal-server-error code (1011).
    """
    try:
        await ws.send_str("error: " + message)
    finally:
        # 1011 == websocket.CloseInternalServerErr in gorilla/websocket.
        await ws.close(code=1011, message=message.encode("utf-8", "replace"))


def _spawn_reader(
    ws: web.WebSocketResponse, on_close: Callable[[], object]
) -> "asyncio.Task[None]":
    """Drain inbound frames; call ``on_close`` when the client goes away.

    The browser-side read pump only exists to notice a disconnect. When any
    close/error frame arrives (or reading raises) we invoke ``on_close`` — typically closing the
    Docker stream so the outbound scanner unblocks and the handler returns.
    """

    async def _reader() -> None:
        try:
            async for msg in ws:
                if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.ERROR):
                    break
        except Exception:  # noqa: BLE001 - disconnect is not worth surfacing
            pass
        finally:
            res = on_close()
            if asyncio.iscoroutine(res):
                try:
                    await res
                except Exception:  # noqa: BLE001
                    pass

    return asyncio.ensure_future(_reader())


class _LineSplitter:
    """Incremental ``bufio.Scanner``: feed byte chunks, get complete lines back.

    Splits on ``\\n``, stripping a trailing ``\\r`` so CRLF and LF streams frame
    identically. A line longer than :data:`_MAX_LINE` is flushed as-is (rather
    than erroring) so a pathological stream cannot wedge the pump; any trailing
    bytes without a newline come out of :meth:`finish`.
    """

    __slots__ = ("_buf",)

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> List[bytes]:
        out: List[bytes] = []
        buf = self._buf
        buf.extend(chunk)
        start = 0
        while True:
            nl = buf.find(b"\n", start)
            if nl < 0:
                break
            end = nl - 1 if nl > start and buf[nl - 1] == 0x0D else nl
            out.append(bytes(buf[start:end]))
            start = nl + 1
        if start:
            del buf[:start]
        if len(buf) > _MAX_LINE:
            out.append(bytes(buf))
            buf.clear()
        return out

    def finish(self) -> List[bytes]:
        if not self._buf:
            return []
        rest = bytes(self._buf)
        self._buf.clear()
        return [rest]


async def _lines(stream: AsyncByteStream):
    """Yield newline-delimited frames from an async byte stream.

    The Python analogue of ``bufio.Scanner`` over the Docker reader (see
    :class:`_LineSplitter` for the framing rules).
    """
    splitter = _LineSplitter()
    async for chunk in stream:
        if not chunk:
            continue
        for line in splitter.feed(chunk):
            yield line
    for line in splitter.finish():
        yield line


# Log batching (see LineBatcher): flush a batch every LOG_BATCH_DELAY seconds or
# once it reaches LOG_BATCH_BYTES, whichever comes first.
LOG_BATCH_DELAY = 0.05
LOG_BATCH_BYTES = 64 * 1024


class LineBatcher:
    """Groups log lines into newline-joined batches (one WS message each).

    Sending one WebSocket message per log line costs a frame, a syscall-ish
    write and a browser ``onmessage`` dispatch per line; a container dumping its
    500-line backlog, or a chatty one under load, turned that into thousands of
    tiny messages. The batcher accumulates *bytes* lines and hands back a batch
    when:

    * adding a line would push the batch past ``max_bytes`` (the pending batch is
      emitted first, so batches stay under the cap unless one line alone exceeds
      it — that line is then emitted on its own), or
    * ``max_delay`` seconds have passed since the first pending line
      (:meth:`due` / :meth:`time_left`; the pump polls these).

    Lines never contain ``\\n`` (the splitter cut on it), so joining with
    ``\\n`` keeps line boundaries unambiguous; the client splits on newlines and
    a batch of one is byte-identical to the old single-line message.
    """

    def __init__(
        self,
        max_bytes: int = LOG_BATCH_BYTES,
        max_delay: float = LOG_BATCH_DELAY,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_bytes = max(1, int(max_bytes))
        self.max_delay = float(max_delay)
        self._clock = clock
        self._lines: List[bytes] = []
        self._size = 0  # bytes incl. the joining newlines
        self._first_at: Optional[float] = None

    def __len__(self) -> int:
        return len(self._lines)

    def add(self, line: bytes) -> List[bytes]:
        """Queue ``line``; return the batches (0, 1 or 2) that became ready."""
        out: List[bytes] = []
        extra = len(line) + (1 if self._lines else 0)
        if self._lines and self._size + extra > self.max_bytes:
            out.append(self._take())
            extra = len(line)
        if not self._lines:
            self._first_at = self._clock()
        self._lines.append(line)
        self._size += extra
        if self._size >= self.max_bytes:
            out.append(self._take())
        return out

    def time_left(self) -> Optional[float]:
        """Seconds until the pending batch is due, or ``None`` when empty."""
        if self._first_at is None:
            return None
        return max(0.0, self.max_delay - (self._clock() - self._first_at))

    def due(self) -> bool:
        left = self.time_left()
        return left is not None and left <= 0.0

    def flush(self) -> Optional[bytes]:
        """Return the pending batch (if any) regardless of timing."""
        return self._take() if self._lines else None

    def _take(self) -> bytes:
        data = self._lines[0] if len(self._lines) == 1 else b"\n".join(self._lines)
        self._lines = []
        self._size = 0
        self._first_at = None
        return data


async def _next_chunk(it) -> bytes:
    return await it.__anext__()


async def stream_lines(
    ws: web.WebSocketResponse,
    stream: AsyncByteStream,
    transform: Optional[Callable[[bytes], bytes]] = None,
    *,
    batch: bool = False,
    max_bytes: int = LOG_BATCH_BYTES,
    max_delay: float = LOG_BATCH_DELAY,
) -> None:
    """Pump ``stream`` to ``ws`` as text messages until it ends.

    The single shared pump behind the container-logs, deployment-logs and
    stats-stream endpoints. ``transform`` rewrites each
    frame before it is sent (used to strip Docker's log-multiplex header);
    ``None`` sends the raw line.

    With ``batch=False`` every line is its own message (stats: one JSON sample
    per message). With ``batch=True`` lines are grouped by :class:`LineBatcher`
    into newline-joined messages (logs).

    A background reader closes ``stream`` when the client disconnects so this
    loop unblocks. On a normal end it sends a normal-closure frame. This owns
    neither ``ws`` nor ``stream`` — the caller closes both.
    """
    reader = _spawn_reader(ws, lambda: _safe_close(stream))
    try:
        if batch:
            ok = await _pump_batched(ws, stream, transform, max_bytes, max_delay)
        else:
            ok = await _pump_lines(ws, stream, transform)
        if ok and not ws.closed:
            await ws.close(code=1000)
    finally:
        reader.cancel()


async def _pump_lines(ws, stream, transform) -> bool:
    async for line in _lines(stream):
        frame = transform(line) if transform is not None else line
        try:
            await ws.send_str(frame.decode("utf-8", "replace"))
        except (ConnectionResetError, RuntimeError):
            return False  # client gone; not an error worth surfacing
    return True


async def _pump_batched(ws, stream, transform, max_bytes: int, max_delay: float) -> bool:
    """Batched pump: reads whole chunks and flushes on size or age.

    A read is kept in flight as a task so a pending batch can be flushed when
    ``max_delay`` elapses even if the container goes quiet (we can't cancel an
    ``__anext__`` without killing the stream, so we wait on the task instead).
    Returns False if the client went away mid-send.
    """
    batcher = LineBatcher(max_bytes, max_delay)
    splitter = _LineSplitter()

    async def send(data: Optional[bytes]) -> bool:
        if not data:
            return True
        try:
            await ws.send_str(data.decode("utf-8", "replace"))
            return True
        except (ConnectionResetError, RuntimeError):
            return False

    def queue(lines: List[bytes]) -> List[bytes]:
        ready: List[bytes] = []
        for line in lines:
            frame = transform(line) if transform is not None else line
            ready.extend(batcher.add(frame))
        return ready

    it = stream.__aiter__()
    pending: Optional[asyncio.Task] = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(_next_chunk(it))
            wait = batcher.time_left()
            if wait is not None and not pending.done():
                done, _ = await asyncio.wait({pending}, timeout=wait)
                if not done:
                    if not await send(batcher.flush()):
                        return False
                    continue
            task, pending = pending, None
            try:
                chunk = await task
            except StopAsyncIteration:
                break
            if not chunk:
                continue
            for data in queue(splitter.feed(chunk)):
                if not await send(data):
                    return False
            if batcher.due() and not await send(batcher.flush()):
                return False
        for data in queue(splitter.finish()):
            if not await send(data):
                return False
        return await send(batcher.flush())
    finally:
        if pending is not None and not pending.done():
            pending.cancel()


async def pump_exec(ws: web.WebSocketResponse, stream) -> None:
    """Bidirectionally copy bytes between ``ws`` and an interactive exec stream.

    Inbound WS messages are written verbatim
    to the exec stdin, and exec stdout/stderr is read in chunks and sent to the
    browser as binary messages. When either side closes, both stop and the socket
    is closed with a normal-closure frame. This owns neither ``ws`` nor
    ``stream`` — the caller closes both.
    """

    async def _stdin() -> None:
        # browser WS -> exec stdin. Each inbound frame is written verbatim.
        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    data = msg.data
                elif msg.type == WSMsgType.TEXT:
                    data = msg.data.encode("utf-8")
                else:
                    break  # CLOSE/CLOSING/ERROR
                if not data:
                    continue
                await stream.write(data)
        except Exception:  # noqa: BLE001 - client gone / stream closed
            pass
        finally:
            await _safe_close(stream)  # unblock the stdout read below

    stdin_task = asyncio.ensure_future(_stdin())
    try:
        # exec stdout/stderr -> browser WS as binary messages.
        while True:
            try:
                data = await stream.read(_EXEC_CHUNK)
            except Exception as err:  # noqa: BLE001
                log.debug("exec stream ended: %s", err)
                break
            if not data:
                break  # EOF: process exited or stdin closed
            try:
                await ws.send_bytes(data)
            except (ConnectionResetError, RuntimeError):
                break
        if not ws.closed:
            await ws.close(code=1000)
    finally:
        stdin_task.cancel()


async def _safe_close(stream) -> None:
    """Close an AsyncByteStream / ExecStream, swallowing errors and awaiting a
    coroutine ``close`` if the object exposes one (ExecStream.close is async)."""
    close = getattr(stream, "close", None)
    if close is None:
        return
    try:
        res = close()
        if asyncio.iscoroutine(res):
            await res
    except Exception:  # noqa: BLE001
        pass


def strip_docker_log_header(b: bytes) -> bytes:
    """Strip the 8-byte stream-multiplexing header Docker prepends to log frames.

    On TTY-less containers the
    engine prefixes each frame with ``[stream, 0, 0, 0, size(4 BE)]``. If the
    line does not look like a multiplexed frame it is returned unchanged.
    """
    if (
        len(b) >= 8
        and b[0] in (0, 1, 2)
        and b[1] == 0
        and b[2] == 0
        and b[3] == 0
    ):
        return b[8:]
    return b


def is_truthy(v: str) -> bool:
    """Report whether a query-string value should be treated as true.

    Accepts "1"/"true"/"yes"/"on"
    (case-insensitive); empty or anything else is false.
    """
    return v.strip().lower() in ("1", "true", "yes", "on")
