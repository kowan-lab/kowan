"""Container log streaming over WebSocket.

The shared line pump and WebSocket upgrader live in :mod:`kowan.api._dockerws`.

Route (mounted by :func:`register`):

    GET /api/v1/hosts/{hostID}/containers/{id}/logs   (WebSocket)

The handler upgrades the connection to a WebSocket and streams a container's log
lines as text messages, following the stream until the container stops, the
client disconnects, or the connection is cancelled. It is viewer-accessible
(read-only), outside the operator-gated routes.

Lines are batched: each text message carries one or more log lines joined with
``\\n`` (flushed every ~50 ms or 64 KB, see ``_dockerws.LineBatcher``), so
clients must split each message on newlines.

Query parameters:

  - follow: defaults to true (a live tail); pass follow=false for a one-shot
    snapshot of the current backlog.
  - tail: bounds the initial backlog (default 500 lines) so a busy container
    does not flood the client with its entire history.
"""

from __future__ import annotations

import logging

from aiohttp import web

from ..docker.base import AsyncByteStream
from ._dockerws import (
    is_truthy,
    prepare_ws,
    resolve_ops,
    stream_lines,
    strip_docker_log_header,
    _safe_close,
)

log = logging.getLogger("kowan.api.logs")


async def container_logs(request: web.Request) -> web.WebSocketResponse:
    """GET /hosts/{hostID}/containers/{id}/logs — stream logs over a WebSocket."""
    # Resolve Ops BEFORE upgrading so a bad host/provider surfaces as a normal
    # HTTP error (an upgraded connection can no longer send an HTTP status).
    ops = resolve_ops(request)
    container_id = request.match_info["id"]

    # follow defaults to true; tail defaults to 500.
    follow = True
    if (v := request.query.get("follow")) is not None and v != "":
        follow = is_truthy(v)
    tail = 500
    if (v := request.query.get("tail")) is not None and v != "":
        try:
            tail = int(v)
        except ValueError:
            tail = 500

    ws = await prepare_ws(request)

    stream: AsyncByteStream
    try:
        stream = await ops.container_logs(container_id, follow, tail)
    except Exception as err:  # noqa: BLE001 - report to the socket then close
        from ._dockerws import _write_close_error

        await _write_close_error(ws, str(err))
        return ws

    try:
        await stream_lines(ws, stream, strip_docker_log_header, batch=True)
    except Exception as err:  # noqa: BLE001
        log.debug("logs stream ended: %s (container=%s)", err, container_id)
    finally:
        await _safe_close(stream)
    return ws


def register(app: web.Application, svc) -> None:
    """Mount the container-logs WebSocket route.

    Read-only, so no role gate. ``api_route`` prepends the /api/v1 prefix.
    """
    from ..web import api_route

    api_route(
        app,
        "GET",
        "/hosts/{hostID}/containers/{id}/logs",
        container_logs,
    )
