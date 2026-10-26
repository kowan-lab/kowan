"""The message-transport abstraction the mux is built on."""

from __future__ import annotations

from abc import ABC, abstractmethod


class MessageTransport(ABC):
    """A bidirectional, reliable, ordered *message* pipe.

    ``send`` transmits one whole message; ``recv`` returns one whole message
    (or ``None`` at end-of-stream); ``close`` tears the pipe down and causes
    the peer's ``recv`` to return ``None``.
    """

    @abstractmethod
    async def send(self, data: bytes) -> None: ...

    @abstractmethod
    async def recv(self) -> bytes | None: ...

    @abstractmethod
    async def close(self) -> None: ...
