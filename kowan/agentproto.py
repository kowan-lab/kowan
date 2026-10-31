"""Agent protocol: a request/response framing over a single mux stream.

The wire contract between ``docker.RemoteClient`` (control plane) and the
remote/edge agent (``agent_ops_server``). Each call uses ONE multiplexed stream,
because the tunnel already gives us a symmetric stream mux (``kowan.mux``) and we
don't want to run a whole HTTP server over it.

Wire format (one call == one :class:`kowan.mux.Stream`)
------------------------------------------------------

Request::

    <header-line>\\n            JSON: {"op": str, "args": {...}, "body": bool}
    [ body frames ]            present iff header.body is true

Response::

    <header-line>\\n            JSON: {"ok": true, "meta": <any>}
                               or   {"ok": false, "error": str}
    [ body bytes ... EOF ]     present iff the op streams a body (ok only)

Body framing (request side) is length-prefixed so the server can tell a
half-close apart from "more input coming" — this is what lets ``exec_attach``
stream stdin frames while stdout streams back concurrently::

    <4-byte big-endian length><payload>   repeated
    <4-byte big-endian 0>                  terminator (end of request body)

Response bodies are NOT length-prefixed: the server simply streams raw bytes
and half-closes the stream (FIN) when done, so the client reads to EOF.

Each op maps one-to-one onto a :class:`kowan.docker.base.Ops` method;
:func:`serve_ops` dispatches each op to that method and streams the result back.
"""

from __future__ import annotations

from . import jsonx
import logging
import struct
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, Optional, Tuple

# Shares the "kowan.agent" logger namespace so op-level failures show up next to
# the agent's connect/serve lines in the agent container logs.
log = logging.getLogger("kowan.agent")

# ---------------------------------------------------------------------------
# Streaming ops: their response is a byte stream, not a JSON result. The client
# hands the returned stream back to its caller unchanged; the server pumps the
# Ops-returned AsyncByteStream into the response body. Kept as a module constant
# so both halves agree.
# ---------------------------------------------------------------------------

STREAMING_OPS = frozenset(
    {
        "container_logs",
        "container_stats_stream",
        "events",  # Docker events NDJSON (bounded with until, live without)
        "pull_image",
        "download_volume_file",
        "download_volume_archive",
        "read_volume_file",  # bounded, but streamed as raw bytes
        "exec_attach",  # bidirectional: request body frames + response body bytes
    }
)

_LEN = struct.Struct(">I")


class ProtocolError(Exception):
    """Raised when a peer sends a malformed frame or the op reports an error."""


# ---------------------------------------------------------------------------
# Low-level line / frame helpers over a mux Stream
# ---------------------------------------------------------------------------


# A header line carries an op's JSON result (a container list, an inspect):
# generous, but a peer cannot make the other side buffer without end.
_MAX_LINE = 64 << 20


async def _read_line(stream: Any) -> bytes:
    """Read a single ``\\n``-terminated line from ``stream`` (newline stripped).

    Reads one byte at a time — headers are short and this keeps us from
    over-reading into a following body (mux ``read`` has no unget).
    """
    buf = bytearray()
    while True:
        b = await stream.read(1)
        if not b:
            if not buf:
                raise ProtocolError("agentproto: eof before header line")
            break
        if b == b"\n":
            break
        buf.extend(b)
        if len(buf) > _MAX_LINE:
            raise ProtocolError("agentproto: header line too long")
    return bytes(buf)


async def _read_exact(stream: Any, n: int) -> bytes:
    """Read exactly ``n`` bytes or raise on early EOF."""
    out = bytearray()
    while len(out) < n:
        chunk = await stream.read(n - len(out))
        if not chunk:
            raise ProtocolError(
                f"agentproto: eof after {len(out)}/{n} bytes of body frame"
            )
        out.extend(chunk)
    return bytes(out)


async def _write_body_frame(stream: Any, payload: bytes) -> None:
    """Write one length-prefixed request-body frame."""
    await stream.write(_LEN.pack(len(payload)) + payload)


async def _write_body_terminator(stream: Any) -> None:
    """Write the zero-length frame that ends a request body."""
    await stream.write(_LEN.pack(0))


async def _iter_request_body(stream: Any) -> AsyncIterator[bytes]:
    """Yield the payloads of a length-prefixed request body until the terminator."""
    while True:
        header = await _read_exact(stream, 4)
        (length,) = _LEN.unpack(header)
        if length == 0:
            return
        yield await _read_exact(stream, length)


async def _pump_body(
    stream: Any, body: Any, chunk_size: int = 32 * 1024
) -> None:
    """Copy an ``AsyncByteStream`` / bytes / async-iterable into request-body frames."""
    if body is None:
        return
    if isinstance(body, (bytes, bytearray)):
        if body:
            await _write_body_frame(stream, bytes(body))
        return
    # AsyncByteStream: prefer explicit read(n); fall back to async iteration.
    read = getattr(body, "read", None)
    if callable(read):
        while True:
            chunk = await read(chunk_size)
            if not chunk:
                break
            await _write_body_frame(stream, chunk)
        return
    async for chunk in body:  # async iterable of bytes
        if chunk:
            await _write_body_frame(stream, chunk)


# ---------------------------------------------------------------------------
# Response-body stream wrapper handed back to the RemoteClient caller
# ---------------------------------------------------------------------------


class _ResponseBody:
    """An :class:`~kowan.docker.base.AsyncByteStream` over the mux stream's read half.

    Reads response bytes until the server half-closes (EOF), then closing this
    resets the underlying stream so a half-open stream never lingers. Satisfies
    the ``read`` / ``__aiter__`` / ``close`` duck type the rest of the code
    expects of a streaming Docker response body.
    """

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._closed = False

    async def read(self, n: int = 65536) -> bytes:
        if self._closed:
            return b""
        if n < 0:
            # Drain to EOF (io.ReadAll semantics).
            out = bytearray()
            while True:
                chunk = await self._stream.read(65536)
                if not chunk:
                    break
                out.extend(chunk)
            return bytes(out)
        return await self._stream.read(n)

    def __aiter__(self) -> "AsyncIterator[bytes]":
        return self._iter()

    async def _iter(self) -> AsyncIterator[bytes]:
        while True:
            chunk = await self._stream.read(65536)
            if not chunk:
                return
            yield chunk

    async def write(self, data: bytes) -> None:
        """Send an input frame (used only by exec_attach's bidirectional stream)."""
        await _write_body_frame(self._stream, data)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._stream.reset()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Client half: issue one op over one stream
# ---------------------------------------------------------------------------


async def call(
    stream: Any,
    op: str,
    args: Optional[Dict[str, Any]] = None,
    body: Any = None,
) -> Tuple[Any, Optional[_ResponseBody]]:
    """Issue ``op`` over ``stream`` and return ``(meta, body_stream)``.

    ``args`` is the JSON-serialisable argument map for the op. ``body`` — when
    given — is the request payload (bytes / an ``AsyncByteStream`` / an async
    iterable of bytes) streamed to the agent (used by uploads and by
    ``exec_attach`` for the initial handshake, though exec streams stdin
    interactively via the returned body's ``write``).

    For non-streaming ops the returned ``meta`` is the decoded JSON result and
    ``body_stream`` is ``None``. For streaming ops ``meta`` is whatever metadata
    the agent chose to echo (usually ``None``) and ``body_stream`` is a
    :class:`_ResponseBody` the caller reads / closes.

    Raises :class:`ProtocolError` on a framing error or when the agent reports
    ``{"ok": false, "error": ...}``.
    """
    args = args or {}
    streaming = op in STREAMING_OPS
    has_body = body is not None or op == "exec_attach"

    header = jsonx.dumps({"op": op, "args": args, "body": has_body})
    await stream.write(header + b"\n")

    if has_body:
        await _pump_body(stream, body)
        # For exec_attach we DON'T terminate yet — stdin keeps flowing through
        # the returned _ResponseBody.write(); the terminator is sent when the
        # caller closes. For plain uploads we terminate so the server proceeds.
        if op != "exec_attach":
            await _write_body_terminator(stream)

    resp_line = await _read_line(stream)
    try:
        resp = jsonx.loads(resp_line)
    except Exception as exc:  # noqa: BLE001
        raise ProtocolError(f"agentproto: bad response header: {exc}") from exc

    if not resp.get("ok", False):
        message = resp.get("error") or "agentproto: agent error"
        status = resp.get("status")
        if isinstance(status, int) and 400 <= status < 500:
            # A typed client error on the agent (not found, invalid, conflict)
            # keeps its status and its class across the tunnel, so callers'
            # ``except NotFound`` work the same for remote hosts.
            from . import errors

            cls = {400: errors.Validation, 401: errors.Unauthorized, 403: errors.Forbidden,
                   404: errors.NotFound, 409: errors.Conflict}.get(status, errors.AppError)
            raise cls(message, status=status)
        if status == 502:
            # The agent reached its Docker daemon and the daemon failed.
            from .errors import BadGateway

            raise BadGateway(message)
        raise ProtocolError(message)

    if streaming:
        return resp.get("meta"), _ResponseBody(stream)

    # Non-streaming: no body follows; half-close our write side is unnecessary
    # (server already responded) but harmless. Leave the stream to be reset by
    # the caller when it drops the reference — the mux resets on close().
    return resp.get("meta"), None


# ---------------------------------------------------------------------------
# Server half: read one op, dispatch to Ops, stream the result back
# ---------------------------------------------------------------------------


async def _write_ok(stream: Any, meta: Any = None) -> None:
    await stream.write(jsonx.dumps({"ok": True, "meta": meta}) + b"\n")


async def _write_error(stream: Any, message: str, status: Optional[int] = None) -> None:
    # `status` (optional, additive) carries the HTTP status of a typed error
    # (404 not found, 400 invalid…) so the control plane can surface it instead
    # of a generic 502.
    frame: Dict[str, Any] = {"ok": False, "error": message}
    if isinstance(status, int) and 400 <= status < 600:
        frame["status"] = status
    await stream.write(jsonx.dumps(frame) + b"\n")


async def serve_ops(stream: Any, ops: Any) -> None:
    """Serve exactly one op call arriving on ``stream``, dispatching to ``ops``.

    Reads the request header, invokes the matching :class:`Ops` method, and
    streams the JSON result or byte body back per the wire format. Errors from
    the op are reported as ``{"ok": false, "error": ...}`` when they occur
    before the response header is sent; once a streaming body has started there
    is no way to signal failure except closing the stream (best effort).

    The caller (``agent_ops_server.serve_agent_ops``) is responsible for
    closing / resetting the stream once this returns.
    """
    try:
        header_line = await _read_line(stream)
    except ProtocolError:
        return  # peer opened and dropped the stream; nothing to do

    try:
        req = jsonx.loads(header_line)
        op = req["op"]
        args = req.get("args") or {}
        has_body = bool(req.get("body"))
    except Exception as exc:  # noqa: BLE001
        log.warning("agent op: bad request header: %s", exc)
        await _write_error(stream, f"agentproto: bad request header: {exc}")
        return

    log.debug("agent op received: %s", op)

    handler = _DISPATCH.get(op)
    if handler is None:
        # Drain any request body so the peer isn't left blocked, then error.
        if has_body:
            try:
                async for _ in _iter_request_body(stream):
                    pass
            except ProtocolError:
                pass
        log.warning("agent op: unknown op %r", op)
        await _write_error(stream, f"agentproto: unknown op: {op}")
        return

    args = _coerce_arg_models(op, args)

    try:
        await handler(stream, ops, args, has_body)
    except ProtocolError:
        # Framing error mid-call: the stream is unusable, just bail.
        log.debug("agent op %s: framing error mid-call", op)
        return
    except Exception as exc:  # noqa: BLE001
        # This is where a failed Docker op surfaces (e.g. a socket permission
        # error on GET /info). Log it locally with the op name — otherwise the
        # only trace is the error sent back over the wire, and the agent side
        # looks silent. Report it iff we haven't committed a response yet: the
        # per-op handlers send the header only after the op succeeds for
        # streaming ops, and carry a flag for unary ops, so a raised exception
        # here means "before/instead of the header" — safe to report.
        log.warning("agent op %s failed: %s", op, exc)
        try:
            await _write_error(stream, str(exc), getattr(exc, "status", None))
        except Exception:  # noqa: BLE001
            pass


# --- per-op handlers -------------------------------------------------------
#
# Each handler receives (stream, ops, args, has_body). Unary handlers compute a
# JSON-serialisable meta and send it via _write_ok. Streaming handlers open the
# underlying stream, send _write_ok, then pump bytes and FIN.


def _dump(model: Any) -> Any:
    """JSON-normalise an Ops return value (pydantic model / list / scalar)."""
    if model is None:
        return None
    if isinstance(model, list):
        return [_dump(m) for m in model]
    md = getattr(model, "model_dump", None)
    if callable(md):
        return md(by_alias=True)
    return model


async def _drain_body(stream: Any, has_body: bool) -> None:
    """Consume and discard a request body (if any)."""
    if not has_body:
        return
    async for _ in _iter_request_body(stream):
        pass


async def _stream_response(stream: Any, src: Any) -> None:
    """Send an OK header then pump ``src`` (AsyncByteStream) into the body + FIN."""
    await _write_ok(stream, None)
    try:
        read = getattr(src, "read", None)
        if isinstance(src, (bytes, bytearray)):
            # Bounded ops (read_volume_file) return the bytes themselves.
            for i in range(0, len(src), 32 * 1024):
                await stream.write(bytes(src[i:i + 32 * 1024]))
        elif callable(read):
            while True:
                chunk = await read(32 * 1024)
                if not chunk:
                    break
                await stream.write(chunk)
        else:
            async for chunk in src:
                if chunk:
                    await stream.write(chunk)
    finally:
        close = getattr(src, "close", None)
        if callable(close):
            res = close()
            if hasattr(res, "__await__"):
                await res
        await stream.close()  # FIN: signal end of response body


# Unary handler factory: bind an Ops coroutine + an arg-extraction lambda.
def _unary(method_name: str, arg_fn: Callable[[Dict[str, Any]], tuple]):
    async def handler(stream, ops, args, has_body):
        await _drain_body(stream, has_body)
        method = getattr(ops, method_name)
        call_args = arg_fn(args)
        result = await method(*call_args)
        await _write_ok(stream, _dump(result))

    return handler


# Streaming handler factory for response-only streams (logs/stats/pull/download).
# ``watch=True`` is for long-lived streams that may sit idle (a log follow on a
# quiet container): see _pump_watched.
def _stream(method_name: str, arg_fn: Callable[[Dict[str, Any]], tuple], *, watch: bool = False):
    async def handler(stream, ops, args, has_body):
        await _drain_body(stream, has_body)
        method = getattr(ops, method_name)
        src = await method(*arg_fn(args))
        if watch:
            await _pump_watched(stream, src)
        else:
            await _stream_response(stream, src)

    return handler


async def _pump_watched(stream: Any, src: Any) -> None:
    """Pump ``src`` like :func:`_stream_response`, but stop as soon as the
    caller goes away.

    The pump alone only notices a vanished caller when its next write fails, so
    a follow on a quiet container kept the Engine request (and this task) open
    indefinitely after the viewer closed. The client never sends anything after
    the header, so any read completing (EOF, or StreamReset when the caller
    closes its body) means "stop": cancel the pump, which closes ``src``.
    """
    import asyncio

    async def watch() -> None:
        try:
            while await stream.read(1):
                pass
        except Exception:  # noqa: BLE001 - reset by peer == client gone
            pass

    pump = asyncio.ensure_future(_stream_response(stream, src))
    watcher = asyncio.ensure_future(watch())
    try:
        await asyncio.wait({pump, watcher}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        pump.cancel()
        watcher.cancel()
        await asyncio.gather(pump, watcher, return_exceptions=True)
        close = getattr(src, "close", None)
        if callable(close):
            try:
                res = close()
                if hasattr(res, "__await__"):
                    await res
            except Exception:  # noqa: BLE001
                pass


async def _handle_upload(stream, ops, args, has_body):
    """upload_volume_file: request body is the file content (length-prefixed)."""

    class _BodyReader:
        def __init__(self, it: AsyncIterator[bytes]) -> None:
            self._it = it
            self._buf = bytearray()
            self._eof = False

        async def read(self, n: int = -1) -> bytes:
            if n is None or n < 0:
                out = bytearray(self._buf)
                self._buf.clear()
                async for chunk in self._it:
                    out.extend(chunk)
                return bytes(out)
            while len(self._buf) < n and not self._eof:
                try:
                    chunk = await self._it.__anext__()
                except StopAsyncIteration:
                    self._eof = True
                    break
                self._buf.extend(chunk)
            take = min(n, len(self._buf))
            data = bytes(self._buf[:take])
            del self._buf[:take]
            return data

    reader = _BodyReader(_iter_request_body(stream))
    await ops.upload_volume_file(
        args["name"],
        args.get("destDir", ""),
        args.get("filename", ""),
        int(args.get("size", 0)),
        reader,
    )
    await _write_ok(stream, None)


async def _handle_exec_attach(stream, ops, args, has_body):
    """exec_attach: bidirectional. Pump request-body frames -> exec stdin and
    exec stdout -> response body concurrently until either side ends."""
    import asyncio

    exec_stream = await ops.exec_attach(
        args["id"], args.get("cmd") or [], bool(args.get("tty", True))
    )
    await _write_ok(stream, None)

    async def pump_in() -> None:
        try:
            async for chunk in _iter_request_body(stream):
                if chunk:
                    await exec_stream.write(chunk)
        except Exception:  # noqa: BLE001
            pass

    async def pump_out() -> None:
        try:
            while True:
                chunk = await exec_stream.read(32 * 1024)
                if not chunk:
                    break
                await stream.write(chunk)
        except Exception:  # noqa: BLE001
            pass

    task_in = asyncio.ensure_future(pump_in())
    task_out = asyncio.ensure_future(pump_out())
    try:
        await asyncio.wait(
            {task_in, task_out}, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        # Cancel whichever pump is still running (usually pump_in, blocked in
        # stream.read waiting for more stdin) so it doesn't linger holding the
        # mux stream after the exec side ends. Awaiting both (return_exceptions)
        # avoids a "Task was destroyed but it is pending" warning.
        task_in.cancel()
        task_out.cancel()
        await asyncio.gather(task_in, task_out, return_exceptions=True)
        close = getattr(exec_stream, "close", None)
        if callable(close):
            res = close()
            if hasattr(res, "__await__"):
                await res
        await stream.close()


async def _handle_events(stream, ops, args, has_body):
    """events: like a plain response stream, but also watch the read half.

    A live events stream can sit idle for minutes, so the pump alone would
    never notice the control plane going away (it only fails on write). The
    client never sends anything after the header, so any read completing
    (EOF, or StreamReset when the caller closes its body) means "stop": we
    cancel the pump, which closes the Engine request.
    """
    await _drain_body(stream, has_body)
    filters = args.get("filters") or None
    src = await ops.events(
        args.get("since") or None, args.get("until") or None, filters
    )

    await _pump_watched(stream, src)


# Argument extractors -------------------------------------------------------
def _a(*keys, defaults=None):
    """Build an arg_fn pulling ``keys`` (with optional defaults) positionally."""
    defaults = defaults or {}

    def fn(args: Dict[str, Any]) -> tuple:
        return tuple(args.get(k, defaults.get(k)) for k in keys)

    return fn


# The op table: one entry per Ops method exposed to the control plane.
_DISPATCH: Dict[str, Callable[..., Awaitable[None]]] = {
    "ping": _unary("ping", _a()),
    # containers
    "list_containers": _unary("list_containers", _a("all", defaults={"all": False})),
    "inspect_container": _unary("inspect_container", _a("id")),
    "container_spec": _unary("container_spec", _a("id")),
    "start_container": _unary("start_container", _a("id")),
    "stop_container": _unary("stop_container", _a("id")),
    "restart_container": _unary("restart_container", _a("id")),
    "remove_container": _unary(
        "remove_container", _a("id", "force", defaults={"force": False})
    ),
    "container_stats": _unary("container_stats", _a("id")),
    "create_container": _unary(
        "create_container", _a("name", "spec", "start", defaults={"start": False})
    ),
    "pause_container": _unary("pause_container", _a("id")),
    "unpause_container": _unary("unpause_container", _a("id")),
    "kill_container": _unary("kill_container", _a("id", "signal", defaults={"signal": "SIGKILL"})),
    "exec": _unary("exec", _a("id", "cmd", defaults={"cmd": []})),
    "container_logs": _stream(
        "container_logs",
        _a("id", "follow", "tail", defaults={"follow": False, "tail": 0}),
        watch=True,
    ),
    "container_stats_stream": _stream("container_stats_stream", _a("id"), watch=True),
    "exec_attach": _handle_exec_attach,
    "events": _handle_events,
    # images
    "list_images": _unary("list_images", _a()),
    "build_image": _unary("build_image", _a("opts")),
    "pull_image": _stream(
        "pull_image", _a("ref", "auth", defaults={"ref": "", "auth": ""})
    ),
    "remove_image": _unary(
        "remove_image", _a("id", "force", defaults={"force": False})
    ),
    "inspect_image": _unary("inspect_image", _a("id")),
    "image_history": _unary("image_history", _a("id")),
    # volumes
    "list_volumes": _unary("list_volumes", _a()),
    "create_volume": _unary("create_volume", _a("opts")),
    "remove_volume": _unary(
        "remove_volume", _a("name", "force", defaults={"force": False})
    ),
    "inspect_volume": _unary("inspect_volume", _a("name")),
    "browse_volume": _unary(
        "browse_volume", _a("name", "subPath", defaults={"subPath": ""})
    ),
    "read_volume_file": _stream(
        "read_volume_file", _a("name", "subPath", defaults={"subPath": ""})
    ),
    "download_volume_file": _stream(
        "download_volume_file", _a("name", "subPath", defaults={"subPath": ""})
    ),
    "download_volume_archive": _stream(
        "download_volume_archive", _a("name", "subPath", defaults={"subPath": ""})
    ),
    "upload_volume_file": _handle_upload,
    # plugins
    "list_plugins": _unary("list_plugins", _a()),
    "install_plugin": _unary("install_plugin", _a("opts")),
    "remove_plugin": _unary(
        "remove_plugin", _a("name", "force", defaults={"force": False})
    ),
    # networks
    "list_networks": _unary("list_networks", _a()),
    "create_network": _unary("create_network", _a("opts")),
    "inspect_network": _unary("inspect_network", _a("id")),
    "connect_container": _unary("connect_container", _a("networkId", "containerId")),
    "disconnect_container": _unary(
        "disconnect_container",
        _a("networkId", "containerId", "force", defaults={"force": False}),
    ),
    "remove_network": _unary("remove_network", _a("id")),
    # system / prune
    "system_info": _unary("system_info", _a()),
    "disk_usage": _unary("disk_usage", _a()),
    "prune_containers": _unary("prune_containers", _a()),
    "prune_images": _unary("prune_images", _a("all", defaults={"all": False})),
    "prune_volumes": _unary("prune_volumes", _a()),
    "prune_networks": _unary("prune_networks", _a()),
    "prune_build_cache": _unary("prune_build_cache", _a()),
    # compose / swarm
    "compose_up": _unary("compose_up", _a("project", "compose", defaults={"compose": ""})),
    "compose_down": _unary("compose_down", _a("project")),
    "swarm_active": _unary("swarm_active", _a()),
    "list_swarm_nodes": _unary("list_swarm_nodes", _a()),
    "swarm_deploy": _unary("swarm_deploy", _a("stack", "compose", defaults={"compose": ""})),
    "swarm_remove": _unary("swarm_remove", _a("stack")),
    "update_service_image": _unary("update_service_image", _a("stack", "id", "image", "auth", defaults={"stack": "", "auth": ""})),
}


# ---------------------------------------------------------------------------
# The build_image / create_container / *_opts ops carry pydantic models as args.
# The dispatch above passes the raw decoded dict straight through to the Ops
# method; LocalClient is responsible for validating (it does model_validate on
# the spec/opts). To keep the agent side honest we coerce the well-known ones
# here so LocalClient always sees a proper typed model.
# ---------------------------------------------------------------------------


def _coerce_arg_models(op: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Best-effort coercion of dict args into their pydantic models (in place)."""
    try:
        from .models import (  # local import: models is a heavy package
            BuildOptions,
            ContainerSpec,
            NetworkCreateOptions,
            VolumeCreateOptions,
        )
        from .docker.base import PluginInstallOptions
    except Exception:  # noqa: BLE001
        return args

    coercions = {
        "build_image": ("opts", BuildOptions),
        "create_volume": ("opts", VolumeCreateOptions),
        "create_network": ("opts", NetworkCreateOptions),
        "install_plugin": ("opts", PluginInstallOptions),
        "create_container": ("spec", ContainerSpec),
    }
    if op in coercions:
        key, model = coercions[op]
        val = args.get(key)
        if isinstance(val, dict):
            args[key] = model.model_validate(val)
    return args


# ---------------------------------------------------------------------------
# Container filesystem ops (archive API) — container file browser + exposure.
# Appended as a block (not merged into the tables above) so the op set stays
# easy to diff. ``container_get_archive`` / ``container_read_file`` stream raw
# bytes back; ``container_put_archive`` takes a length-framed request body (the
# tar), like ``upload_volume_file``.
# ---------------------------------------------------------------------------

STREAMING_OPS = STREAMING_OPS | frozenset({"container_get_archive", "container_read_file"})


class _FramedBodyReader:
    """``read(n)`` over the length-prefixed request body frames."""

    def __init__(self, stream: Any) -> None:
        self._it = _iter_request_body(stream)
        self._buf = bytearray()
        self._eof = False

    async def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            out = bytearray(self._buf)
            self._buf.clear()
            async for chunk in self._it:
                out.extend(chunk)
            self._eof = True
            return bytes(out)
        while not self._buf and not self._eof:
            try:
                self._buf.extend(await self._it.__anext__())
            except StopAsyncIteration:
                self._eof = True
        take = min(n, len(self._buf))
        data = bytes(self._buf[:take])
        del self._buf[:take]
        return data

    async def drain(self) -> None:
        if not self._eof:
            async for _ in self._it:
                pass
            self._eof = True


class _OneShot:
    """A tiny AsyncByteStream over an in-memory payload."""

    def __init__(self, data: bytes) -> None:
        self._data = memoryview(data)
        self._pos = 0

    async def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = len(self._data)
        out = bytes(self._data[self._pos:self._pos + n])
        self._pos += len(out)
        return out


async def _handle_container_put_archive(stream, ops, args, has_body):
    reader = _FramedBodyReader(stream)
    try:
        await ops.container_put_archive(args["id"], args.get("path", "/"), reader)
    finally:
        # Never leave unread frames behind (the op may fail early).
        try:
            await reader.drain()
        except ProtocolError:
            pass
    await _write_ok(stream, None)


async def _handle_container_read_file(stream, ops, args, has_body):
    await _drain_body(stream, has_body)
    data = await ops.container_read_file(
        args["id"], args.get("path", "/"), int(args.get("maxBytes") or (1 << 20))
    )
    await _stream_response(stream, _OneShot(data or b""))


_DISPATCH.update(
    {
        "container_path_stat": _unary("container_path_stat", _a("id", "path")),
        "container_list_dir": _unary(
            "container_list_dir", _a("id", "path", "limit", defaults={"path": "/", "limit": 1000})
        ),
        "container_read_file": _handle_container_read_file,
        "container_get_archive": _stream("container_get_archive", _a("id", "path")),
        "container_put_archive": _handle_container_put_archive,
        "container_exec_status": _unary(
            "container_exec_status", _a("id", "cmd", "user", defaults={"cmd": [], "user": ""})
        ),
        "container_relabel": _unary(
            "container_relabel",
            _a(
                "id", "setLabels", "removeLabels", "connectNetworks", "disconnectNetworks",
                defaults={"setLabels": {}, "removeLabels": [], "connectNetworks": [],
                          "disconnectNetworks": []},
            ),
        ),
    }
)
