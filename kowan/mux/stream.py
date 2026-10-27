"""A single multiplexed stream: an ordered, full-duplex, flow-controlled
byte channel.

Flow control is credit-based (like yamux). Each direction starts with a
fixed receive window; the sender may only send up to its remaining
``send_window`` and then blocks (backpressure) until the receiver, as its
application *consumes* bytes, replenishes the credit with a WINDOW_UPDATE.
This is what stops one busy stream (say a log follow) from starving the
others while still bounding memory.
"""

from __future__ import annotations

import asyncio

DEFAULT_WINDOW = 256 * 1024  # per-stream receive window (yamux default)
MAX_FRAME = 16 * 1024        # cap a single DATA frame's payload


class StreamReset(Exception):
    """Raised when the peer aborts the stream (RST)."""


class StreamClosed(Exception):
    """Raised on read-after-EOF-needed / write-after-close."""


class Stream:
    def __init__(self, session, stream_id: int, window: int = DEFAULT_WINDOW):
        self._session = session
        self.id = stream_id
        self._window = window
        # receive side
        self._recv_buf = bytearray()
        self._recv_eof = False           # peer sent FIN
        self._pending_consumed = 0       # bytes consumed but not yet credited back
        self._data_avail = asyncio.Event()
        # send side
        self._send_window = window       # bytes we may send before blocking
        self._send_cond = asyncio.Condition()
        self._send_closed = False        # we sent FIN
        # lifecycle
        self._reset = False

    # ------------------------------------------------------------------ #
    # Called by the session reader task (single task, so no locking here) #
    # ------------------------------------------------------------------ #
    def _feed(self, data: bytes) -> bool:
        """Buffer peer data. Returns False, buffering nothing, when the peer
        sent more than the credit we granted: bytes it has sent and we have not
        credited back yet are ``_recv_buf`` + ``_pending_consumed``, and that
        may never exceed the window. A peer that ignores flow control would
        otherwise grow ``_recv_buf`` without bound."""
        if len(self._recv_buf) + self._pending_consumed + len(data) > self._window:
            return False
        self._recv_buf.extend(data)
        self._data_avail.set()
        return True

    def _feed_eof(self) -> None:
        self._recv_eof = True
        self._data_avail.set()
        self._detach()

    async def _add_send_credit(self, delta: int) -> None:
        async with self._send_cond:
            self._send_window += delta
            self._send_cond.notify_all()

    async def _do_reset(self) -> None:
        self._reset = True
        self._recv_eof = True
        self._recv_buf.clear()  # reads raise from now on; free the bytes
        self._data_avail.set()
        async with self._send_cond:
            self._send_cond.notify_all()
        self._detach()

    def _detach(self) -> None:
        """Drop this stream from the session's table once it can carry no more
        frames — after a reset, or once BOTH directions have closed (we sent FIN
        and the peer sent FIN). Without this the session's ``_streams`` dict grows
        without bound: ``RemoteClient`` opens a stream per Docker op and resets it
        (docker/remote.py), but only a *received* RST used to prune the table, so
        the initiating (reset-sending) side leaked an entry per op on the
        long-lived Hub session. Idempotent; a late frame for a detached id simply
        draws a RST from the peer (see Session._dispatch)."""
        if self._reset or (self._send_closed and self._recv_eof):
            self._session._streams.pop(self.id, None)

    # ---------------------------- public API --------------------------- #
    async def read(self, n: int = 65536) -> bytes:
        """Read up to ``n`` bytes. Blocks for data; returns ``b""`` at EOF."""
        if n <= 0:
            return b""
        while not self._recv_buf and not self._recv_eof and not self._reset:
            self._data_avail.clear()
            await self._data_avail.wait()
        if self._reset:
            raise StreamReset(f"stream {self.id} reset by peer")
        if not self._recv_buf:
            return b""  # clean EOF
        take = min(n, len(self._recv_buf))
        data = bytes(self._recv_buf[:take])
        del self._recv_buf[:take]
        await self._consume(len(data))
        return data

    async def readall(self) -> bytes:
        """Read until the peer half-closes (FIN)."""
        out = bytearray()
        while True:
            chunk = await self.read()
            if not chunk:
                return bytes(out)
            out.extend(chunk)

    async def _consume(self, n: int) -> None:
        """Account for delivered bytes; replenish the peer's send window."""
        self._pending_consumed += n
        # Batch credits to avoid a WINDOW_UPDATE storm: send once we've
        # freed at least half the window.
        if self._pending_consumed >= self._window // 2:
            delta = self._pending_consumed
            self._pending_consumed = 0
            await self._session._send_window_update(self.id, delta)

    async def write(self, data: bytes) -> None:
        if self._send_closed:
            raise StreamClosed(f"stream {self.id} write after close")
        mv = memoryview(data)
        off, n = 0, len(mv)
        while off < n:
            async with self._send_cond:
                while self._send_window <= 0 and not self._reset:
                    await self._send_cond.wait()
                if self._reset:
                    raise StreamReset(f"stream {self.id} reset by peer")
                budget = min(self._send_window, MAX_FRAME, n - off)
                self._send_window -= budget
                chunk = bytes(mv[off:off + budget])
                off += budget
            # Send the DATA frame OUTSIDE the condition lock so the reader
            # can keep replenishing credit concurrently.
            await self._session._send_data(self.id, chunk)

    async def close(self) -> None:
        """Half-close the write direction (send FIN). Reads still work."""
        if self._send_closed or self._reset:
            self._send_closed = True
            self._detach()
            return
        self._send_closed = True
        await self._session._send_fin(self.id)
        self._detach()

    async def reset(self) -> None:
        """Abort the stream in both directions (send RST)."""
        if self._reset:
            return
        self._reset = True
        await self._session._send_rst(self.id)
        self._detach()
