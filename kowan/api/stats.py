"""Container resource metrics: a one-shot JSON sample and a live WebSocket stream.

The shared line pump and WebSocket upgrader live in :mod:`kowan.api._dockerws`.

Routes (mounted by :func:`register`):

    GET /api/v1/hosts/{hostID}/containers/{id}/stats          -> one JSON sample
    GET /api/v1/hosts/{hostID}/containers/{id}/stats/stream   -> WebSocket, NDJSON

Both are viewer-accessible (read-only), outside the operator-gated routes. The
streaming endpoint emits one
normalized ``models.ContainerStats`` per sample (~1/s) as a text message until
the container stops, the client disconnects, or the connection is cancelled.
"""

from __future__ import annotations

import logging

from aiohttp import web

from ..docker.base import AsyncByteStream
from ..web import json_response
from ._dockerws import (
    prepare_ws,
    resolve_ops,
    stream_lines,
    _safe_close,
)

log = logging.getLogger("kowan.api.stats")


async def container_stats(request: web.Request) -> web.Response:
    """GET /hosts/{hostID}/containers/{id}/stats — one sampled set of metrics."""
    ops = resolve_ops(request)
    container_id = request.match_info["id"]
    from ..errors import AppError, BadGateway

    try:
        stats = await ops.container_stats(container_id)
    except AppError:
        raise  # typed (404 no such container, ...) keeps its status
    except Exception as err:  # noqa: BLE001
        raise BadGateway(str(err)) from err
    return json_response(stats)


async def container_stats_stream(request: web.Request) -> web.WebSocketResponse:
    """GET /hosts/{hostID}/containers/{id}/stats/stream — live stats over a WS."""
    # Resolve Ops BEFORE upgrading so failures surface as HTTP errors.
    ops = resolve_ops(request)
    container_id = request.match_info["id"]

    ws = await prepare_ws(request)

    stream: AsyncByteStream
    try:
        stream = await ops.container_stats_stream(container_id)
    except Exception as err:  # noqa: BLE001 - report to the socket then close
        from ._dockerws import _write_close_error

        await _write_close_error(ws, str(err))
        return ws

    try:
        # NDJSON of ContainerStats; each line is already valid JSON, so no
        # per-frame transform.
        await stream_lines(ws, stream, None)
    except Exception as err:  # noqa: BLE001
        log.debug("stats stream ended: %s (container=%s)", err, container_id)
    finally:
        await _safe_close(stream)
    return ws


def register(app: web.Application, svc) -> None:
    """Mount the one-shot stats and live stats-stream routes.

    Both are read-only, so no role gate. ``api_route`` prepends /api/v1.
    """
    from ..web import api_route

    api_route(
        app,
        "GET",
        "/hosts/{hostID}/containers/{id}/stats",
        container_stats,
    )
    api_route(
        app,
        "GET",
        "/hosts/{hostID}/containers/{id}/stats/stream",
        container_stats_stream,
    )
