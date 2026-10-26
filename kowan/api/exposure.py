"""Exposure and HTTPS routes (see ``kowan/exposure`` and docs/exposure.md).

  GET  /exposure                          every host's exposure (any role)
  GET  /hosts/{hostID}/exposure           one host (any role)
  GET  /hosts/{hostID}/exposure/certificates   cert + router status (any role)
  POST /hosts/{hostID}/exposure/expose    publish a container / deployment (operator)
  POST /hosts/{hostID}/exposure/unexpose  undo it (operator)
  POST /hosts/{hostID}/exposure/proxy     deploy Traefik with Let's Encrypt (operator)
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from aiohttp import web

from kowan import errors
from kowan.exposure import service as exposure
from kowan.store import NotFoundError
from kowan.web import api_route, bind_svc, json_response, ops_error_response, read_json, require_role

if TYPE_CHECKING:  # pragma: no cover
    from kowan.web import Services

log = logging.getLogger("kowan.api.exposure")

_HOST_TIMEOUT = 15.0


def _host(svc: "Services", request: web.Request) -> Any:
    host_id = request.match_info.get("hostID", "")
    try:
        return svc.store.get_host(host_id)
    except NotFoundError:
        raise errors.NotFound("host not found")


async def _one(svc: "Services", request: web.Request) -> web.StreamResponse:
    host = _host(svc, request)
    try:
        out = await asyncio.wait_for(exposure.host_exposure(svc, host), _HOST_TIMEOUT)
    except asyncio.TimeoutError:
        return ops_error_response(errors.BadGateway("host did not answer in time"))
    except Exception as err:  # noqa: BLE001
        return ops_error_response(err)
    return json_response(out)


async def _all(svc: "Services", request: web.Request) -> web.StreamResponse:
    hosts = svc.store.list_hosts()

    async def one(h: Any) -> Any:
        try:
            return await asyncio.wait_for(exposure.host_exposure(svc, h), _HOST_TIMEOUT)
        except Exception as err:  # noqa: BLE001
            return {"hostId": h.id, "hostName": h.name or h.id, "error": str(err) or "unreachable",
                    "ports": [], "domains": [], "proxies": [], "summary": {}}

    results = await asyncio.gather(*(one(h) for h in hosts))
    return json_response({"hosts": sorted(results, key=lambda r: (r.get("hostName") or "").lower())})


async def _certs(svc: "Services", request: web.Request) -> web.StreamResponse:
    host = _host(svc, request)
    try:
        out = await asyncio.wait_for(exposure.certificates(svc, host), 30.0)
    except asyncio.TimeoutError:
        return ops_error_response(errors.BadGateway("host did not answer in time"))
    except Exception as err:  # noqa: BLE001
        return ops_error_response(err)
    return json_response(out)


def _mutation(fn):
    async def handler(svc: "Services", request: web.Request) -> web.StreamResponse:
        host = _host(svc, request)
        body = await read_json(request)
        if not isinstance(body, dict):
            return ops_error_response(errors.Validation("invalid request body"))
        try:
            out = await fn(svc, host, body)
        except NotFoundError:
            return ops_error_response(errors.NotFound("not found"))
        except Exception as err:  # noqa: BLE001
            return ops_error_response(err)
        return json_response(out)

    return handler


def register(app: web.Application, svc: "Services") -> None:
    h = bind_svc(svc)
    op = require_role("operator")
    api_route(app, "GET", "/exposure", h(_all))
    api_route(app, "GET", "/hosts/{hostID}/exposure", h(_one))
    api_route(app, "GET", "/hosts/{hostID}/exposure/certificates", h(_certs))
    api_route(app, "POST", "/hosts/{hostID}/exposure/expose", op(h(_mutation(exposure.expose))))
    api_route(app, "POST", "/hosts/{hostID}/exposure/unexpose", op(h(_mutation(exposure.unexpose))))
    api_route(app, "POST", "/hosts/{hostID}/exposure/proxy", op(h(_mutation(exposure.deploy_proxy))))
