"""Event loop selection: uvloop when available, stdlib asyncio otherwise.

`uvloop <https://github.com/MagicStack/uvloop>`_ is a drop-in asyncio loop built
on libuv; it makes socket I/O, the WebSocket tunnel mux and aiohttp's request
handling noticeably cheaper. It's optional: no wheel (e.g. Windows) or
``KOWAN_UVLOOP=0`` in the environment means the plain asyncio loop. Only the
process entrypoints (``kowan.server`` / ``kowan.agent``) call this; tests and
library use keep whatever loop they create.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Awaitable, Callable, Optional, TypeVar

log = logging.getLogger("kowan.eventloop")

T = TypeVar("T")


def loop_factory() -> Optional[Callable[[], asyncio.AbstractEventLoop]]:
    """Return ``uvloop.new_event_loop`` if usable and not disabled, else None."""
    if os.environ.get("KOWAN_UVLOOP", "").strip().lower() in ("0", "false", "no", "off"):
        return None
    try:
        import uvloop  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 - optional dependency
        return None
    return uvloop.new_event_loop


def new_event_loop() -> asyncio.AbstractEventLoop:
    """A new event loop (uvloop if available) — for code that manages its loop."""
    factory = loop_factory()
    loop = factory() if factory is not None else asyncio.new_event_loop()
    log.info("event loop: %s", "uvloop" if factory is not None else "asyncio")
    return loop


def run(main: Awaitable[T]) -> T:
    """``asyncio.run`` on the preferred loop."""
    factory = loop_factory()
    if factory is None:
        return asyncio.run(main)  # type: ignore[arg-type]
    log.info("event loop: uvloop")
    if sys.version_info >= (3, 12):
        return asyncio.run(main, loop_factory=factory)  # type: ignore[arg-type,call-arg]
    import uvloop  # type: ignore[import-not-found]

    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    return asyncio.run(main)  # type: ignore[arg-type]
