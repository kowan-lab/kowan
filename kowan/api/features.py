"""Platform features on/off (kowan/features.py).

  GET /features  {templates, networkMap, gitops: bool}   (any signed-in user:
                                                          the UI hides what is off)
  PUT /features  {<feature>: bool, ...} -> the new state (admin)
"""

from __future__ import annotations

from aiohttp import web

from kowan import features
from kowan.auth import require_role
from kowan.errors import Validation
from kowan.web import SERVICES_KEY, api_route, json_response, read_json


def register(app: web.Application, svc) -> None:
    app.middlewares.append(features.middleware(svc.store))
    api_route(app, "GET", "/features", _get)
    api_route(app, "PUT", "/features", require_role("admin")(_put))


async def _get(request: web.Request) -> web.Response:
    return json_response(features.get(request.app[SERVICES_KEY].store))


async def _put(request: web.Request) -> web.Response:
    body = await read_json(request)
    if not isinstance(body, dict) or not body:
        raise Validation("send {feature: true|false, ...}")
    try:
        return json_response(features.update(request.app[SERVICES_KEY].store, body))
    except ValueError as err:
        raise Validation(str(err)) from None
