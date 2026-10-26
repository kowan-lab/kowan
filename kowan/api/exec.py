"""Container exec: a one-shot combined-output run and an interactive WebSocket.

The WebSocket upgrader and byte pump live in :mod:`kowan.api._dockerws`.

Routes (mounted by :func:`register`), both operator-gated (exec is a write
operation on the container):

    POST /api/v1/hosts/{hostID}/containers/{id}/exec   body {cmd:[...]} -> {output}
    GET  /api/v1/hosts/{hostID}/containers/{id}/exec   (WebSocket, interactive)

The one-shot handler runs ``cmd`` inside the container and returns the combined
output. The WebSocket handler upgrades the connection, starts an interactive
exec session (``ops.exec_attach``), and copies bytes bidirectionally between the
browser and the exec stream until either side closes.

WebSocket query parameters:

  - cmd: repeatable; the command to run (default ["/bin/sh"]).
  - tty: whether to allocate a TTY (default true).
"""

from __future__ import annotations

import logging

from aiohttp import web

from ..errors import AppError, BadGateway, Validation
from ..web import json_response, read_json, require_role
from ._dockerws import (
    is_truthy,
    prepare_ws,
    pump_exec,
    resolve_ops,
    _safe_close,
    _write_close_error,
)

log = logging.getLogger("kowan.api.exec")


async def exec_container(request: web.Request) -> web.Response:
    """POST /hosts/{hostID}/containers/{id}/exec — run cmd, return combined output.

    Body ``{"cmd": [...]}``; ``cmd`` is required.
    """
    ops = resolve_ops(request)

    body = await read_json(request)
    if not isinstance(body, dict):
        raise Validation("invalid request body")
    cmd = body.get("cmd")
    if not isinstance(cmd, list) or len(cmd) == 0:
        raise Validation("cmd is required")

    container_id = request.match_info["id"]
    try:
        out = await ops.exec(container_id, [str(c) for c in cmd])
    except AppError:
        raise  # typed (404 no such container, ...) keeps its status
    except Exception as err:  # noqa: BLE001
        raise BadGateway(str(err)) from err
    return json_response({"output": out})


async def exec_stream(request: web.Request) -> web.WebSocketResponse:
    """GET /hosts/{hostID}/containers/{id}/exec — interactive exec over a WebSocket."""
    # Resolve Ops BEFORE upgrading so failures surface as normal HTTP responses.
    ops = resolve_ops(request)
    container_id = request.match_info["id"]

    # cmd is repeatable; default to /bin/sh. tty defaults to true.
    cmd = request.query.getall("cmd", [])
    if not cmd:
        cmd = ["/bin/sh"]
    tty = True
    if (v := request.query.get("tty")) is not None and v != "":
        tty = is_truthy(v)

    ws = await prepare_ws(request)

    try:
        stream = await ops.exec_attach(container_id, cmd, tty)
    except Exception as err:  # noqa: BLE001 - report to the socket then close
        await _write_close_error(ws, str(err))
        return ws

    try:
        await pump_exec(ws, stream)
    except Exception as err:  # noqa: BLE001
        log.debug("exec stream ended: %s (container=%s)", err, container_id)
    finally:
        await _safe_close(stream)
    return ws


def register(app: web.Application, svc) -> None:
    """Mount the one-shot and interactive exec routes.

    Both require the operator role. ``api_route`` prepends /api/v1.
    For the WebSocket route the credential may be supplied via ``?token=`` (the
    auth middleware handles that for upgrade requests) before ``require_role``
    runs.
    """
    from ..web import api_route

    api_route(
        app,
        "POST",
        "/hosts/{hostID}/containers/{id}/exec",
        require_role("operator")(exec_container),
    )
    api_route(
        app,
        "GET",
        "/hosts/{hostID}/containers/{id}/exec",
        require_role("operator")(exec_stream),
    )
