"""Docker network HTTP handlers, scoped per host.

Every route lives under
``/hosts/{hostID}/networks``. The handler resolves the concrete
:class:`kowan.docker.base.Ops` for the addressed host through the docker
Provider (``svc.docker.ops(host)``), looking the host record up in the store.

Mutating operations (create / connect / disconnect / remove) require at least
the operator role.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiohttp import web

from kowan import errors
from kowan.docker.base import Ops
from kowan.models import NetworkCreateOptions
from kowan.store import NotFoundError
from kowan.web import (
    ops_error_response,
    api_route,
    bind_svc,
    error_response,
    json_response,
    read_json,
    require_role,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kowan.web import Services


async def _resolve_ops(svc: "Services", request: web.Request) -> Ops:
    """Resolve the :class:`Ops` for the ``{hostID}`` URL param.

    A missing/unknown host is a 404, a docker
    provider failure a 502. The resolved host record drives which concrete Ops
    (local / remote agent / edge tunnel) is returned.
    """
    host_id = request.match_info.get("hostID", "")
    if not host_id:
        raise errors.Validation("missing host id")
    try:
        host = svc.store.get_host(host_id)
    except NotFoundError:
        raise errors.NotFound("host not found")
    try:
        return svc.docker.ops(host)
    except Exception as err:  # noqa: BLE001 - provider build failure -> 502
        raise errors.BadGateway("resolve host client: " + str(err))


async def _list_networks(svc: "Services", request: web.Request) -> web.Response:
    ops = await _resolve_ops(svc, request)
    try:
        networks = await ops.list_networks()
    except Exception as err:  # noqa: BLE001
        return ops_error_response(err)
    return json_response(networks)


async def _create_network(svc: "Services", request: web.Request) -> web.Response:
    ops = await _resolve_ops(svc, request)
    body = await read_json(request)
    try:
        req = NetworkCreateOptions.model_validate(body)  # pydantic errors are ValueErrors
    except ValueError:
        return error_response(400, "invalid request body")
    if not req.name:
        return error_response(400, "name is required")
    try:
        net = await ops.create_network(req)
    except Exception as err:  # noqa: BLE001
        return ops_error_response(err)
    return json_response(net, status=201)


async def _inspect_network(svc: "Services", request: web.Request) -> web.Response:
    ops = await _resolve_ops(svc, request)
    net_id = request.match_info["id"]
    try:
        net = await ops.inspect_network(net_id)
    except Exception as err:  # noqa: BLE001
        return ops_error_response(err)
    return json_response(net)


async def _connect_container(svc: "Services", request: web.Request) -> web.Response:
    ops = await _resolve_ops(svc, request)
    net_id = request.match_info["id"]
    body = await read_json(request)
    container_id = body.get("containerId", "") if isinstance(body, dict) else ""
    if not container_id:
        return error_response(400, "containerId is required")
    try:
        await ops.connect_container(net_id, container_id)
    except Exception as err:  # noqa: BLE001
        return ops_error_response(err)
    return json_response({"status": "connected"})


async def _disconnect_container(svc: "Services", request: web.Request) -> web.Response:
    ops = await _resolve_ops(svc, request)
    net_id = request.match_info["id"]
    body = await read_json(request)
    if not isinstance(body, dict):
        body = {}
    container_id = body.get("containerId", "")
    force = bool(body.get("force", False))
    if not container_id:
        return error_response(400, "containerId is required")
    try:
        await ops.disconnect_container(net_id, container_id, force)
    except Exception as err:  # noqa: BLE001
        return ops_error_response(err)
    return json_response({"status": "disconnected"})


async def _remove_network(svc: "Services", request: web.Request) -> web.Response:
    ops = await _resolve_ops(svc, request)
    net_id = request.match_info["id"]
    try:
        await ops.remove_network(net_id)
    except Exception as err:  # noqa: BLE001
        return ops_error_response(err)
    return json_response({"status": "removed"})


def register(app: web.Application, svc: "Services") -> None:
    """Mount the per-host network routes under ``/api/v1``.

    Read routes (list / inspect) are open to any authenticated principal; the
    mutating routes are wrapped with ``require_role("operator")``.
    """
    h = bind_svc(svc)
    base = "/hosts/{hostID}/networks"

    api_route(app, "GET", base, h(_list_networks))
    api_route(app, "GET", base + "/{id}", h(_inspect_network))

    api_route(app, "POST", base, require_role("operator")(h(_create_network)))
    api_route(
        app, "POST", base + "/{id}/connect",
        require_role("operator")(h(_connect_container)),
    )
    api_route(
        app, "POST", base + "/{id}/disconnect",
        require_role("operator")(h(_disconnect_container)),
    )
    api_route(
        app, "DELETE", base + "/{id}",
        require_role("operator")(h(_remove_network)),
    )
