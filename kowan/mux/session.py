"""Session — a symmetric stream multiplexer over one message transport.

Both peers can open streams. To keep stream ids collision-free the two
ends split the id space:

  * the "client" side (our control-plane, by convention) uses ODD ids;
  * the "server" side (the agent) uses EVEN ids.

Which side is the client is decided by our *tunnel role*, NOT by who dialed
the underlying socket (in edge mode the agent dials out, yet the
control-plane is still the client).
"""

from __future__ import annotations

import asyncio
import logging

from . import frame
from .stream import Stream, DEFAULT_WINDOW

log = logging.getLogger("kowan.mux")


class Session:
    def __init__(self, transport, *, is_client: bool,
                 window: int = DEFAULT_WINDOW):
        self._t = transport
        self._is_client = is_client
        self._window = window
        self._streams: dict[int, Stream] = {}
        self._accept_q: asyncio.Queue = asyncio.Queue()
        self._next_id = 1 if is_client else 2  # odd for client, even for server
        self._write_lock = asyncio.Lock()      # serialize transport writes
        self._closed = False
        self._reader_task: asyncio.Task | None = None

    def start(self) -> "Session":
        """Launch the background reader loop. Returns self for chaining."""
        self._reader_task = asyncio.ensure_future(self._read_loop())
        return self

    # ------------------------------- API ------------------------------- #
    async def open_stream(self) -> Stream:
        if self._closed:
            raise ConnectionError("session closed")
        sid = self._next_id
        self._next_id += 2
        s = Stream(self, sid, self._window)
        self._streams[sid] = s
        await self._send_syn(sid)
        return s

    async def accept_stream(self):
        """Return the next peer-opened stream, or None when the session ends."""
        return await self._accept_q.get()

    async def wait_closed(self) -> None:
        """Block until the session ends (transport EOF/close or GO_AWAY).

        Callers that own the underlying socket (e.g. an aiohttp /edge/connect
        handler) use this to keep the connection alive WITHOUT reading the
        transport themselves — the reader loop is the single consumer, so a
        second concurrent read would race it.
        """
        t = self._reader_task
        if t is None:
            return
        # asyncio.wait does not consume the task's exception and does not cancel
        # it if this waiter is cancelled.
        await asyncio.wait({t})

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._send_frame(frame.TYPE_GO_AWAY, 0, 0, 0)
        except Exception:
            pass
        await self._t.close()
        await self._fail_all()
        await self._accept_q.put(None)
        if self._reader_task is not None:
            self._reader_task.cancel()

    # ---------------- frame senders (used by Stream) ------------------- #
    async def _send_frame(self, typ, flags, sid, length, payload=b"") -> None:
        data = frame.encode(typ, flags, sid, length, payload)
        async with self._write_lock:
            await self._t.send(data)

    async def _send_syn(self, sid):
        await self._send_frame(frame.TYPE_WINDOW_UPDATE, frame.FLAG_SYN, sid, 0)

    async def _send_data(self, sid, payload):
        await self._send_frame(frame.TYPE_DATA, 0, sid, len(payload), payload)

    async def _send_window_update(self, sid, delta):
        await self._send_frame(frame.TYPE_WINDOW_UPDATE, 0, sid, delta)

    async def _send_fin(self, sid):
        await self._send_frame(frame.TYPE_DATA, frame.FLAG_FIN, sid, 0)

    async def _send_rst(self, sid):
        await self._send_frame(frame.TYPE_WINDOW_UPDATE, frame.FLAG_RST, sid, 0)

    # ---------------------------- reader ------------------------------- #
    async def _read_loop(self):
        try:
            while True:
                msg = await self._t.recv()
                if msg is None:
                    break  # transport EOF
                typ, flags, sid, length, payload = frame.decode(msg)
                await self._dispatch(typ, flags, sid, length, payload)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # pragma: no cover - defensive
            log.warning("mux reader error: %r", e)
        finally:
            await self._shutdown()

    async def _dispatch(self, typ, flags, sid, length, payload):
        if typ == frame.TYPE_GO_AWAY:
            await self._shutdown()
            return
        if flags & frame.FLAG_SYN:
            # Peer opened a new stream.
            if sid not in self._streams:
                s = Stream(self, sid, self._window)
                self._streams[sid] = s
                await self._accept_q.put(s)
            return  # SYN carries no payload

        s = self._streams.get(sid)
        if s is None:
            # Unknown stream — tell the peer to give up on it.
            if not (flags & frame.FLAG_RST):
                await self._send_rst(sid)
            return

        if flags & frame.FLAG_RST:
            await s._do_reset()
            self._streams.pop(sid, None)
            return

        if typ == frame.TYPE_DATA:
            if payload:
                s._feed(payload)
            if flags & frame.FLAG_FIN:
                s._feed_eof()
        elif typ == frame.TYPE_WINDOW_UPDATE:
            if flags & frame.FLAG_FIN:
                s._feed_eof()
            elif length:
                await s._add_send_credit(length)

    async def _fail_all(self):
        for s in list(self._streams.values()):
            await s._do_reset()
        self._streams.clear()

    async def _shutdown(self):
        if self._closed:
            return
        self._closed = True
        # Close our end of the transport too. On the reader-EOF path only
        # close() used to do this, and close() early-returns once _closed is set,
        # so a session torn down via transport EOF/GO_AWAY never closed its own
        # transport. Idempotent for WS transports.
        try:
            await self._t.close()
        except Exception:
            pass
        await self._fail_all()
        await self._accept_q.put(None)
