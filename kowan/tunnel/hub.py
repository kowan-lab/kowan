"""Server-side registry of live tunnel sessions, keyed by host id.

Holds BOTH edge (agent-dialed) and
direct (server-dialed) sessions — because once a session exists, the
control-plane drives every host the same way: ``hub.dial(host_id)`` opens a
mux stream and speaks the agent protocol over it.

The mere *presence* of a session is also the host's liveness signal, so no
persisted online flag is needed.
"""

from __future__ import annotations

import asyncio

from ..mux.session import Session
from ..mux.stream import Stream


class Hub:
    def __init__(self):
        self._sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()

    def connected(self, host_id: str) -> bool:
        return host_id in self._sessions

    def hosts(self) -> list[str]:
        return list(self._sessions.keys())

    async def register(self, host_id: str, session: Session) -> None:
        async with self._lock:
            prev = self._sessions.get(host_id)
            self._sessions[host_id] = session
        if prev is not None and prev is not session:
            await prev.close()

    async def unregister(self, host_id: str, session: Session) -> None:
        async with self._lock:
            if self._sessions.get(host_id) is session:
                del self._sessions[host_id]

    async def dial(self, host_id: str) -> Stream:
        """Open a new mux stream to the connected host (control-plane -> agent)."""
        session = self._sessions.get(host_id)
        if session is None:
            raise KeyError(f"host {host_id!r} not connected")
        return await session.open_stream()
