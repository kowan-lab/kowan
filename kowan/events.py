"""In-process publish/subscribe event bus.

It fans out domain events (alerts, notices, ...) to interested in-process
consumers.

Design of :class:`MemoryBus`:

  * ``publish(event)`` never blocks. Each subscriber has a bounded buffer
    (``SUBSCRIBER_BUFFER`` events); if a subscriber's buffer is full the event
    is dropped *for that subscriber only* — publishers are never held up by a
    slow consumer (a non-blocking send).
  * ``subscribe(*types)`` returns a ``Subscription`` — an async iterator that
    yields matching events. Passing no type filters subscribes to *all*
    events.
  * A subscription is cancelled with ``subscription.cancel()`` (or by using it
    as an ``async with`` context manager), which removes it from the bus and
    unblocks any iterator waiting on it.

The store layer is synchronous, but the event bus lives in the async
web/monitor/notify layers, so this is built on :mod:`asyncio` primitives.

Concurrency: the bus is safe for concurrent use from multiple asyncio tasks
running on the *same* event loop. Subscribers should be created and
consumed from tasks; publishers may be any task.

Typical use (monitors publish "alert", notifiers consume)::

    bus = MemoryBus()
    sub = bus.subscribe("alert")            # or subscribe() for all events
    bus.publish(Event(type="alert", source="monitor", payload={...}))
    async for event in sub:
        handle(event)
    sub.cancel()
"""

from __future__ import annotations

import asyncio
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional

__all__ = ["Event", "Bus", "MemoryBus", "Subscription", "SUBSCRIBER_BUFFER"]

# Bounded per-subscriber buffer (events).
SUBSCRIBER_BUFFER = 64


@dataclass
class Event:
    """A single message published on the bus.

    JSON field names: id/type/source/payload/time. ``id`` and ``time`` are
    auto-populated on publish when unset.
    """

    type: str = ""
    source: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    id: str = ""
    time: Optional[datetime] = None


class Subscription:
    """A live subscription to the bus.

    Async-iterable: ``async for event in sub``. Cancel via :meth:`cancel` or by
    using the subscription as an ``async with`` block. Cancelling is idempotent
    and closes the stream so
    any pending iteration ends cleanly.
    """

    def __init__(self, bus: "MemoryBus", key: int, types: Optional[set[str]]):
        self._bus = bus
        self._key = key
        # None / empty set means "all events".
        self._types = types
        self._queue: deque[Event] = deque()
        # Signalled whenever an event is enqueued or the subscription closes,
        # so the async iterator can wake up without busy-waiting.
        self._event = asyncio.Event()
        self._closed = False

    # -- internal (called by the bus, holds no external contract) ----------

    def _matches(self, typ: str) -> bool:
        if not self._types:
            return True
        return typ in self._types

    def _deliver(self, ev: Event) -> None:
        """Non-blocking enqueue. Drops the event if the buffer is full."""
        if self._closed:
            return
        if len(self._queue) >= SUBSCRIBER_BUFFER:
            # Buffer full: drop for this subscriber only.
            return
        self._queue.append(ev)
        self._event.set()

    # -- public API --------------------------------------------------------

    def cancel(self) -> None:
        """Remove this subscription from the bus and end iteration.

        Idempotent. Any task currently iterating will receive ``StopAsyncIteration``
        once the already-buffered events are drained.
        """
        if self._closed:
            return
        self._closed = True
        self._bus._remove(self._key)
        # Wake any waiting iterator so it can observe the closed state.
        self._event.set()

    def __aiter__(self) -> AsyncIterator[Event]:
        return self

    async def __anext__(self) -> Event:
        while True:
            if self._queue:
                return self._queue.popleft()
            if self._closed:
                raise StopAsyncIteration
            # Nothing buffered and still open: wait for a signal.
            self._event.clear()
            # Re-check after clear() to avoid a lost-wakeup race.
            if self._queue or self._closed:
                continue
            await self._event.wait()

    async def __aenter__(self) -> "Subscription":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.cancel()


class Bus:
    """Publish/subscribe interface."""

    def publish(self, event: Event) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def subscribe(self, *types: str) -> Subscription:  # pragma: no cover
        raise NotImplementedError


class MemoryBus(Bus):
    """In-memory implementation of :class:`Bus`.

    A map of subscribers keyed by a monotonically
    increasing integer, with non-blocking fan-out on publish.
    """

    def __init__(self) -> None:
        self._subs: dict[int, Subscription] = {}
        self._next = 0

    def publish(self, event: Event) -> None:
        """Deliver ``event`` to all matching subscribers.

        Auto-populates ``id`` and ``time`` if unset, then performs a
        non-blocking send to every matching subscriber. Never blocks.
        """
        if not event.id:
            event.id = str(uuid.uuid4())
        if event.time is None:
            event.time = datetime.now(timezone.utc)

        # Iterate over a snapshot: a subscriber's cancel() could mutate the dict.
        for sub in list(self._subs.values()):
            if sub._matches(event.type):
                sub._deliver(event)

    def subscribe(self, *types: str) -> Subscription:
        """Register a subscriber for the given event types.

        With no ``types`` the subscription receives every event. Returns a
        :class:`Subscription`; call ``.cancel()`` (or use ``async with``) to
        unsubscribe.
        """
        type_set: Optional[set[str]] = set(types) if types else None
        key = self._next
        self._next += 1
        sub = Subscription(self, key, type_set)
        self._subs[key] = sub
        return sub

    # -- internal ----------------------------------------------------------

    def _remove(self, key: int) -> None:
        self._subs.pop(key, None)
