"""In-memory transport pair — for tests and single-process demos.

``memory_pair()`` returns two connected transports; a message sent on one
is received on the other, in order. ``None`` is the end-of-stream sentinel.
"""

from __future__ import annotations

import asyncio

from .base import MessageTransport


class MemoryTransport(MessageTransport):
    def __init__(self, inbox: asyncio.Queue, outbox: asyncio.Queue):
        self._in = inbox
        self._out = outbox
        self._closed = False

    async def send(self, data: bytes) -> None:
        if self._closed:
            raise ConnectionError("transport closed")
        await self._out.put(bytes(data))

    async def recv(self) -> bytes | None:
        if self._closed:
            return None
        return await self._in.get()  # a None item means the peer closed

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Signal EOF to the peer's recv().
        await self._out.put(None)


def memory_pair():
    """Return two connected ``MemoryTransport`` objects ``(a, b)``."""
    q1: asyncio.Queue = asyncio.Queue()
    q2: asyncio.Queue = asyncio.Queue()
    a = MemoryTransport(inbox=q1, outbox=q2)
    b = MemoryTransport(inbox=q2, outbox=q1)
    return a, b
