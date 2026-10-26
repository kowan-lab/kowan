"""Docker host system + maintenance HTTP handlers, scoped per host.

System info + metrics history, prune by kind + disk usage. Routes live under
``/hosts/{hostID}``.

- GET  /hosts/{hostID}/system  -> docker system info
- GET  /hosts/{hostID}/df      -> disk usage breakdown
- GET  /hosts/{hostID}/metrics -> recorded metric samples (read from the store,
  so an unreachable host still returns its history)
- POST /hosts/{hostID}/prune/{kind} -> prune containers|images|volumes|networks

Prune is a mutation and requires the operator role.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from aiohttp import web

from kowan import errors
from kowan.docker.base import Ops
from kowan.store import NotFoundError
from kowan.web import api_route, bind_svc, error_response, json_response, ops_error_response, require_role

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kowan.web import Services

log = logging.getLogger("kowan.api.system")


# The lookback applied when the request omits ?since=, so
# the dashboard gets a sensible chart without each client computing a timestamp.
DEFAULT_METRICS_WINDOW = timedelta(hours=1)


async def _resolve_ops(svc: "Services", request: web.Request) -> Ops:
    """Resolve the :class:`Ops` for the ``{hostID}`` URL param (see networks.py)."""
    host_id = request.match_info.get("hostID", "")
    if not host_id:
        raise errors.Validation("missing host id")
    try:
        host = svc.store.get_host(host_id)
    except NotFoundError:
        raise errors.NotFound("host not found")
    try:
        return svc.docker.ops(host)
    except Exception as err:  # noqa: BLE001
        raise errors.BadGateway("resolve host client: " + str(err))


def _is_truthy(v: str) -> bool:
    """Report whether a query value should be treated as true (1/true/yes/on)."""
    return v.strip().lower() in ("1", "true", "yes", "on")


async def _system_info(svc: "Services", request: web.Request) -> web.Response:
    host_id = request.match_info.get("hostID", "")
    ops = await _resolve_ops(svc, request)
    try:
        info = await ops.system_info()
    except Exception as err:  # noqa: BLE001
        # Surface WHY the info failed: this is what the UI shows as the generic
        # "Failed to load system info". The real cause (agent socket permission,
        # host not connected, timeout) is in `err` — log it so it's diagnosable.
        log.warning("system info failed: host=%s error=%s", host_id, err)
        return ops_error_response(err)
    return json_response(info)


async def _disk_usage(svc: "Services", request: web.Request) -> web.Response:
    ops = await _resolve_ops(svc, request)
    try:
        usage = await ops.disk_usage()
    except Exception as err:  # noqa: BLE001
        return ops_error_response(err)
    return json_response(usage)


async def _prune(svc: "Services", request: web.Request) -> web.Response:
    """Prune one Docker object kind and report the bytes reclaimed.

    Networks always report zero since the engine returns no reclaimed figure for
    them.
    """
    ops = await _resolve_ops(svc, request)
    kind = request.match_info["kind"]

    reclaimed = 0
    try:
        if kind == "containers":
            reclaimed = await ops.prune_containers()
        elif kind == "images":
            # Sole image-prune path (?all= removes unused images, not just
            # dangling ones).
            reclaimed = await ops.prune_images(_is_truthy(request.query.get("all", "")))
        elif kind == "volumes":
            reclaimed = await ops.prune_volumes()
        elif kind == "networks":
            await ops.prune_networks()
        else:
            return error_response(400, "unknown prune kind: " + kind)
    except Exception as err:  # noqa: BLE001
        return ops_error_response(err)
    return json_response({"spaceReclaimed": reclaimed})


async def _metrics(svc: "Services", request: web.Request) -> web.Response:
    """Return recorded metric samples for a host from the store.

    Unlike the docker-operation routes it does not resolve a docker client: the
    samples are read from the store (recorded by the monitor), so an unreachable
    host still returns its history. The host is validated first so an unknown id
    is a 404. ``?since=`` (RFC3339) defaults to the last hour when absent.
    """
    host_id = request.match_info.get("hostID", "")
    if not host_id:
        return error_response(400, "missing host id")
    from kowan.auth import authorize as _authz

    _authz.check_host(host_id)  # team-scoped users only see hosts they hold

    try:
        svc.store.get_host(host_id)
    except NotFoundError:
        return error_response(404, "host not found")

    since = request.query.get("since", "").strip()
    if not since:
        since = (datetime.now(timezone.utc) - DEFAULT_METRICS_WINDOW).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    else:
        parsed = _parse_rfc3339(since)
        if parsed is None:
            return error_response(400, "invalid since: expected RFC3339 timestamp")
        # Normalise to UTC so the comparison matches stored timestamps.
        since = parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        samples = svc.store.list_metrics(host_id, since)
    except Exception as err:  # noqa: BLE001
        return error_response(500, str(err))
    if samples is None:
        samples = []
    return json_response(samples)


def _parse_rfc3339(value: str):
    """Parse an RFC3339 timestamp, returning a tz-aware datetime or None.

    Accepts a trailing ``Z`` (UTC) which ``datetime.fromisoformat`` on older
    Python does not (RFC 3339 timestamps).
    """
    v = value.strip()
    if v.endswith("Z") or v.endswith("z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def register(app: web.Application, svc: "Services") -> None:
    """Mount the per-host system / maintenance routes under ``/api/v1``.

    system / df / metrics are open to any authenticated principal; prune requires
    the operator role.
    """
    h = bind_svc(svc)
    base = "/hosts/{hostID}"

    api_route(app, "GET", base + "/system", h(_system_info))
    api_route(app, "GET", base + "/df", h(_disk_usage))
    api_route(app, "GET", base + "/metrics", h(_metrics))

    api_route(
        app, "POST", base + "/prune/{kind}",
        require_role("operator")(h(_prune)),
    )
