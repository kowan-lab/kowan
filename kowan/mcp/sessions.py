"""In-memory MCP session table for the Streamable HTTP transport.

A session is created by a successful ``initialize`` and identified by the
``Mcp-Session-Id`` header the server returns. Each session is bound to the
principal that created it (user id + username), so a leaked session id is
useless with another credential. Sessions expire after an idle TTL and the
table is capped (oldest evicted first) so a misbehaving client cannot grow it
without bound. Sessions are deliberately not persisted: a server restart
answers 404 and the client simply re-initializes, as the spec prescribes.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional, Tuple

DEFAULT_IDLE_TTL = 12 * 3600.0
DEFAULT_MAX_SESSIONS = 1024


@dataclass
class Session:
    id: str
    principal: Tuple[str, str]
    protocol_version: str
    created: float = field(default_factory=time.monotonic)
    last_seen: float = field(default_factory=time.monotonic)
    initialized: bool = False
    client_name: str = ""


class SessionStore:
    def __init__(
        self,
        idle_ttl: float = DEFAULT_IDLE_TTL,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        clock=time.monotonic,
    ) -> None:
        self._idle_ttl = idle_ttl
        self._max = max_sessions
        self._clock = clock
        self._lock = threading.Lock()
        self._sessions: "OrderedDict[str, Session]" = OrderedDict()

    def __len__(self) -> int:
        return len(self._sessions)

    def create(self, principal: Tuple[str, str], protocol_version: str, client_name: str = "") -> Session:
        # 32 random bytes, URL-safe base64: visible ASCII only (spec 0x21-0x7E).
        sid = secrets.token_urlsafe(32)
        now = self._clock()
        s = Session(
            id=sid,
            principal=principal,
            protocol_version=protocol_version,
            created=now,
            last_seen=now,
            client_name=client_name,
        )
        with self._lock:
            self._sweep(now)
            while len(self._sessions) >= self._max:
                self._sessions.popitem(last=False)
            self._sessions[sid] = s
        return s

    def get(self, sid: str, principal: Tuple[str, str]) -> Optional[Session]:
        """Return the live session for ``sid`` owned by ``principal``, else None."""
        now = self._clock()
        with self._lock:
            s = self._sessions.get(sid)
            if s is None:
                return None
            if now - s.last_seen > self._idle_ttl:
                del self._sessions[sid]
                return None
            if s.principal != principal:
                return None
            s.last_seen = now
            self._sessions.move_to_end(sid)
            return s

    def delete(self, sid: str, principal: Tuple[str, str]) -> bool:
        with self._lock:
            s = self._sessions.get(sid)
            if s is None or s.principal != principal:
                return False
            del self._sessions[sid]
            return True

    def _sweep(self, now: float) -> None:
        stale = [k for k, s in self._sessions.items() if now - s.last_seen > self._idle_ttl]
        for k in stale:
            del self._sessions[k]
