"""Close every open WebSocket when the server shuts down.

aiohttp waits for running handlers on shutdown, and a WebSocket handler (an
edge agent's tunnel, a browser's log/stats/events stream) never returns on its
own. Without this, SIGTERM hangs until Docker's stop timeout kills the process,
which slows every self-update. Handlers register their socket with
:func:`track`; ``on_shutdown`` closes them with 1001 (going away) so peers
reconnect as soon as the server is back.
"""

from __future__ import annotations

import logging
import weakref

from aiohttp import WSCloseCode, web

log = logging.getLogger("kowan.web.ws")

WS_KEY: web.AppKey = web.AppKey("kowan_websockets", weakref.WeakSet)


def install(app: web.Application) -> None:
    if WS_KEY not in app:
        app[WS_KEY] = weakref.WeakSet()
        app.on_shutdown.append(_close_all)


def track(request: web.Request, ws: web.WebSocketResponse) -> web.WebSocketResponse:
    """Register ``ws`` for closing on shutdown; returns it for chaining."""
    sockets = request.app.get(WS_KEY)
    if sockets is not None:
        sockets.add(ws)
    return ws


async def _close_all(app: web.Application) -> None:
    sockets = list(app.get(WS_KEY, ()))
    if sockets:
        log.info("shutdown: closing %d websocket(s)", len(sockets))
    for ws in sockets:
        try:
            await ws.close(code=WSCloseCode.GOING_AWAY, message=b"server shutting down")
        except Exception:  # noqa: BLE001 - best effort
            pass
