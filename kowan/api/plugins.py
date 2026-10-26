"""Docker managed-plugin (volume driver) HTTP handlers, scoped per host.

These are the Docker *managed* plugins installed on a host (volume drivers such
as s3fs/sshfs). Per-host list/install/remove routes live under
``/hosts/{hostID}/plugins``; the host-independent catalog of supported driver
types (from which the volume UI lets you pick one) is served by
``GET /volume-plugins``. Install / remove require the operator role.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiohttp import web

from kowan import errors
from kowan.docker.base import Ops
from kowan.models import PluginInstallOptions, VolumePlugins
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


async def _list_plugins(svc: "Services", request: web.Request) -> web.Response:
    ops = await _resolve_ops(svc, request)
    try:
        lst = await ops.list_plugins()
    except Exception as err:  # noqa: BLE001
        return ops_error_response(err)
    return json_response(lst)


async def _install_plugin(svc: "Services", request: web.Request) -> web.Response:
    ops = await _resolve_ops(svc, request)
    body = await read_json(request)
    try:
        req = PluginInstallOptions.model_validate(body)  # pydantic errors are ValueErrors
    except ValueError:
        return error_response(400, "invalid request body")
    if not req.remote:
        return error_response(400, "remote plugin reference is required")

    try:
        await ops.install_plugin(req)
    except Exception as err:  # noqa: BLE001
        return ops_error_response(err)
    return json_response({"status": "installed"})


async def _volume_plugin_catalog(svc: "Services", request: web.Request) -> web.Response:
    """Return the built-in catalog of supported volume-driver types.

    A plain static list (s3fs, sshfs, …) the volume UI renders so a user can pick
    a driver type and its option fields. Host-independent; any authenticated user.
    """
    return json_response(VolumePlugins)


async def _remove_plugin(svc: "Services", request: web.Request) -> web.Response:
    ops = await _resolve_ops(svc, request)
    # The plugin name is a query param because managed-plugin names contain a
    # slash (which would break a path segment).
    name = request.query.get("name", "")
    if not name:
        return error_response(400, "name is required")
    force = _is_truthy(request.query.get("force", ""))
    try:
        await ops.remove_plugin(name, force)
    except Exception as err:  # noqa: BLE001
        return ops_error_response(err)
    return json_response({"status": "removed"})


def register(app: web.Application, svc: "Services") -> None:
    """Mount the per-host managed-plugin routes under ``/api/v1``.

    List is open to any authenticated principal; install / remove require the
    operator role.
    """
    h = bind_svc(svc)
    base = "/hosts/{hostID}/plugins"

    api_route(app, "GET", "/volume-plugins", h(_volume_plugin_catalog))
    api_route(app, "GET", base, h(_list_plugins))
    api_route(
        app, "POST", base + "/install",
        require_role("operator")(h(_install_plugin)),
    )
    api_route(
        app, "DELETE", base,
        require_role("operator")(h(_remove_plugin)),
    )
