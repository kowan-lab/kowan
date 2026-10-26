"""Small fake HTTP services for tests (not a test module): a Docker registry
with Basic auth and a webhook receiver that records what it was sent."""

from __future__ import annotations

import base64
import json
from typing import Any, Dict, List, Optional

from aiohttp import web
from aiohttp.test_utils import TestServer


class FakeRegistry:
    """Docker Registry v2: /v2/, /v2/_catalog, /v2/{repo}/tags/list, Basic auth."""

    def __init__(self, username: str = "bob", password: str = "pw", repos: Optional[Dict[str, List[str]]] = None,
                 bearer: bool = False):
        self.username, self.password = username, password
        # bearer=True: /v2/ answers with a Bearer challenge and /token hands out
        # a token for valid Basic credentials (the Docker Hub flow).
        self.bearer = bearer
        self.token = "tok-123"
        self.repos = repos if repos is not None else {"app/web": ["1.0", "1.1", "latest"], "tools": ["v2"]}
        self.server: Optional[TestServer] = None

    def _basic_ok(self, request: web.Request) -> bool:
        want = "Basic " + base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
        return request.headers.get("Authorization", "") == want

    def _authed(self, request: web.Request) -> bool:
        if self.bearer:
            return request.headers.get("Authorization", "") == "Bearer " + self.token
        return self._basic_ok(request)

    def _deny(self) -> web.Response:
        challenge = (f'Bearer realm="http://127.0.0.1:{self.server.port}/token",service="fake"'
                     if self.bearer else 'Basic realm="fake"')
        return web.json_response({"errors": [{"code": "UNAUTHORIZED"}]}, status=401,
                                 headers={"WWW-Authenticate": challenge})

    async def _token(self, request):
        if not self._basic_ok(request):
            return web.json_response({"details": "incorrect username or password"}, status=401)
        return web.json_response({"token": self.token})

    async def _base(self, request):
        return web.json_response({}) if self._authed(request) else self._deny()

    async def _catalog(self, request):
        return web.json_response({"repositories": sorted(self.repos)}) if self._authed(request) else self._deny()

    async def _tags(self, request):
        if not self._authed(request):
            return self._deny()
        repo = request.match_info["repo"]
        if repo not in self.repos:
            return web.json_response({"errors": [{"code": "NAME_UNKNOWN"}]}, status=404)
        return web.json_response({"name": repo, "tags": self.repos[repo]})

    async def start(self) -> str:
        app = web.Application()
        app.router.add_get("/v2/", self._base)
        app.router.add_get("/v2/_catalog", self._catalog)
        app.router.add_get("/v2/{repo:.+}/tags/list", self._tags)
        app.router.add_get("/token", self._token)
        self.server = TestServer(app)
        await self.server.start_server()
        return f"127.0.0.1:{self.server.port}"

    async def stop(self) -> None:
        if self.server is not None:
            await self.server.close()


class FakeReceiver:
    """Accepts POSTs anywhere and records (path, headers, body)."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.received: List[Dict[str, Any]] = []
        self.server: Optional[TestServer] = None

    async def _any(self, request: web.Request) -> web.Response:
        raw = await request.read()
        try:
            body: Any = json.loads(raw) if raw else None
        except ValueError:
            body = raw.decode("utf-8", "replace")
        self.received.append({"path": request.path, "headers": dict(request.headers), "body": body,
                              "method": request.method})
        return web.json_response({"ok": True}, status=self.status)

    async def start(self) -> str:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._any)
        self.server = TestServer(app)
        await self.server.start_server()
        return f"http://127.0.0.1:{self.server.port}"

    async def stop(self) -> None:
        if self.server is not None:
            await self.server.close()
