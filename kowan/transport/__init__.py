"""Message transports the mux runs over.

A transport is a bidirectional, reliable, ordered pipe that delivers whole
*messages* (one mux frame per message). A WebSocket binary message maps
onto this one-to-one; the in-memory pair is used for tests and demos.
"""

from .base import MessageTransport
from .memory import MemoryTransport, memory_pair

__all__ = ["MessageTransport", "MemoryTransport", "memory_pair"]
