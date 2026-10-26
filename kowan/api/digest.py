"""The periodic digest (kowan/digest.py).

Routes (under /api/v1):
  GET  /digest           settings and the last send            (operator)
  PUT  /digest           settings                               (admin)
  GET  /digest/preview   what it would say now, and the text    (operator)
  POST /digest/send      send it now to its channels            (admin)
"""

from __future__ import annotations

from aiohttp import web

from kowan import digest
from kowan.auth import require_role
from kowan.errors import Validation
from kowan.models.credential import CredentialKindChannel
from kowan.web import api_route, json_response, read_json
from kowan.web.app import SERVICES_KEY


def register(app: web.Application, svc) -> None:
    sched = digest.Scheduler(svc)
    svc.attach("digest", sched)
    op, admin = require_role("operator"), require_role("admin")
    api_route(app, "GET", "/digest", op(_get))
    api_route(app, "PUT", "/digest", admin(_put))
    api_route(app, "GET", "/digest/preview", op(_preview))
    api_route(app, "POST", "/digest/send", admin(_send))

    async def _start(_app: web.Application) -> None:
        sched.start()

    async def _stop(_app: web.Application) -> None:
        await sched.stop()

    app.on_startup.append(_start)
    app.on_cleanup.append(_stop)


def _svc(request: web.Request):
    return request.app[SERVICES_KEY]


async def _get(request: web.Request) -> web.Response:
    svc = _svc(request)
    return json_response({"settings": digest.get_settings(svc.store), "last": digest.last_sent(svc.store)})


async def _put(request: web.Request) -> web.Response:
    svc = _svc(request)
    body = await read_json(request)
    if not isinstance(body, dict):
        raise Validation("invalid request body")
    channels = [c.id for c in svc.store.list_credentials(CredentialKindChannel)]
    try:
        settings = digest.validate_settings(body, channels)
    except ValueError as err:
        raise Validation(str(err)) from None
    digest.save_settings(svc.store, settings)
    return json_response(settings)


async def _preview(request: web.Request) -> web.Response:
    svc = _svc(request)
    d = await digest.build(svc, digest.get_settings(svc.store))
    msg = digest.render(d)
    return json_response({**d, "title": msg.title, "text": msg.body, "level": msg.level})


async def _send(request: web.Request) -> web.Response:
    return json_response(await digest.run(_svc(request), force=True))
