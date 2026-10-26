"""Deployment log streaming over a WebSocket.

It reuses the shared log pump from
:mod:`kowan.api._dockerws` (``stream_lines`` + ``strip_docker_log_header``) — the
same one behind the container-logs endpoint — so both stream identically.

``handle_logs`` upgrades ``GET /deployments/{id}/logs?host={hostID}`` to a
WebSocket and streams the managed container's log lines as **text** messages,
following the stream until the container stops, the client disconnects, or the
request context is cancelled. The ``host`` / ``container`` / ``follow`` / ``tail``
query parameters match the container-logs endpoint so the shared log viewer
drives both the same way.

Note: lines are sent as text frames (``ws.send_str``), not binary — the browser
log viewer only parses string messages, so a binary frame would render nothing.
Each frame is a newline-joined batch of one or more lines (see
``kowan.api._dockerws.LineBatcher``).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiohttp import web

from kowan.api._dockerws import stream_lines, strip_docker_log_header, _safe_close
from kowan.web import SERVICES_KEY, error_response

if TYPE_CHECKING:  # pragma: no cover
    from .service import DeploymentService

log = logging.getLogger("kowan.deployment")

# Default backlog bound (tail=500).
_DEFAULT_TAIL = 500


def _parse_bool(v: str, default: bool) -> bool:
    """Parse a query-string bool ("1"/"t"/"true", "0"/"f"/"false", any case).

    Unparseable values fall back to ``default``.
    """
    s = v.strip().lower()
    if s in ("1", "t", "true"):
        return True
    if s in ("0", "f", "false"):
        return False
    return default


async def handle_logs(request: web.Request) -> web.StreamResponse:
    """GET /deployments/{id}/logs?host={hostID} — WebSocket log stream."""
    svc: "DeploymentService" = request.app[SERVICES_KEY].deployments
    dep_id = request.match_info["id"]
    host_id = request.query.get("host", "")
    # Optional: stream a specific compose service's container. Empty -> first.
    container = request.query.get("container", "")

    follow = True
    if request.query.get("follow", "") != "":
        follow = _parse_bool(request.query["follow"], True)
    tail = _DEFAULT_TAIL
    if request.query.get("tail", "") != "":
        try:
            tail = int(request.query["tail"])
        except ValueError:
            pass

    # Resolve everything that can fail with a normal HTTP error BEFORE upgrading:
    # once upgraded, the connection can no longer send an HTTP status.
    store = request.app[SERVICES_KEY].store
    try:
        dep = store.get_deployment(dep_id)
    except Exception:
        return error_response(404, "deployment not found")
    from kowan.auth import authorize as _authz  # team/grant scoping

    if not _authz.deployment_allowed(dep, _authz.LEVEL_READ):
        return error_response(403, "insufficient permissions for this deployment")
    _authz.check_deployment(dep, _authz.LEVEL_READ)
    if host_id == "":
        if not dep.targets:
            return error_response(400, "deployment has no target hosts")
        host_id = dep.targets[0]

    ws = web.WebSocketResponse(heartbeat=30.0)  # detect half-open clients
    await ws.prepare(request)
    from ..web.wsregistry import track as _track_ws

    _track_ws(request, ws)

    try:
        rc = await svc.logs(dep, host_id, container, follow, tail)
    except Exception as err:  # noqa: BLE001
        await ws.send_str("error: " + str(err))
        await ws.close(code=1011, message=str(err).encode("utf-8")[:120])
        return ws

    # Reuse the shared pump: it scans newline-delimited frames, strips Docker's
    # 8-byte multiplex header, and sends each line as a TEXT message (the browser
    # viewer only reads string frames), closing the socket on a normal end.
    try:
        await stream_lines(ws, rc, strip_docker_log_header, batch=True)
    except Exception as err:  # noqa: BLE001
        log.debug("deployment logs stream ended error=%s deployment=%s", err, dep_id)
    finally:
        await _safe_close(rc)
    return ws
