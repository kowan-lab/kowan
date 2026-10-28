"""Deploy an image registry from Kowan (see ``kowan/hostedregistry`` and
docs/hosted-registry.md).

  POST /hosts/{hostID}/registry   deploy one on the host and add it to the registry list (admin)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiohttp import web

from kowan import errors
from kowan.auth.middleware import user_from
from kowan.hostedregistry import service as hosted
from kowan.store import NotFoundError
from kowan.web import api_route, bind_svc, json_response, ops_error_response, read_json, require_role

if TYPE_CHECKING:  # pragma: no cover
    from kowan.web import Services


async def _deploy(svc: "Services", request: web.Request) -> web.StreamResponse:
    try:
        host = svc.store.get_host(request.match_info["hostID"])
    except NotFoundError:
        raise errors.NotFound("host not found")
    body = await read_json(request)
    u = user_from(request)
    try:
        out = await hosted.deploy(svc, host, body, getattr(u, "username", "") if u else "")
    except Exception as err:  # noqa: BLE001 - validation, conflicts, no proxy, offline host
        return ops_error_response(err)
    return json_response(out, status=201)


def register(app: web.Application, svc: "Services") -> None:
    h = bind_svc(svc)
    api_route(app, "POST", "/hosts/{hostID}/registry", require_role("admin")(h(_deploy)))
