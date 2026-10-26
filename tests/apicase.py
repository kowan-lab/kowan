"""A full Kowan server wired to the fake Docker Engine, for API tests
(not a test module itself).

``ServerCase`` builds the real app (``build_app``) with its local Docker socket
pointed at ``FakeEngine`` and signs in one user per role, so a test can call any
route the way the browser does and check both the result and who may call it.
"""

from __future__ import annotations

import datetime
import tempfile
import unittest
from typing import Any, Dict, Optional, Tuple

from aiohttp.test_utils import TestClient, TestServer

from kowan.config import ServerConfig
from kowan.server import build_app
from tests.fakeengine import FakeEngine


def config(data_dir: str, socket: str, **kw: Any) -> ServerConfig:
    return ServerConfig(
        addr="127.0.0.1:0", data_dir=data_dir, jwt_secret="x" * 48,
        monitor_interval=datetime.timedelta(seconds=3600),
        metrics_retention=datetime.timedelta(hours=1), secret_key="", external_url="",
        docker_socket=socket, release_repository="", **kw,
    )


class ServerCase(unittest.IsolatedAsyncioTestCase):
    """Server + fake engine + one signed-in user per role (admin, operator, viewer)."""

    config_overrides: Dict[str, Any] = {}

    async def asyncSetUp(self) -> None:
        self.engine = FakeEngine()
        socket = await self.engine.start()
        self.data_dir = tempfile.mkdtemp()
        app, self.svc = await build_app(config(self.data_dir, socket, **self.config_overrides))
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        r = await self.client.post("/api/v1/auth/init", json={
            "username": "root", "password": "supersecret", "setupToken": self.svc.setup_token})
        self.h: Dict[str, Dict[str, str]] = {"admin": {"Authorization": "Bearer " + (await r.json())["token"]}}
        for role in ("operator", "viewer"):
            await self.client.post("/api/v1/users", headers=self.h["admin"], json={
                "username": role, "password": role + "-password", "role": role})
            r = await self.client.post("/api/v1/auth/login", json={"username": role, "password": role + "-password"})
            self.h[role] = {"Authorization": "Bearer " + (await r.json())["token"]}

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.engine.stop()
        self.svc.store.close()

    async def call(self, method: str, path: str, role: str = "admin", *, json: Any = None,
                   params: Optional[Dict[str, str]] = None, data: Any = None,
                   expect: Optional[int] = None) -> Tuple[int, Any]:
        r = await self.client.request(method, "/api/v1" + path, headers=self.h[role], json=json,
                                      params=params, data=data)
        ctype = r.headers.get("Content-Type", "")
        body = await r.json() if ctype.startswith("application/json") else await r.read()
        if expect is not None:
            self.assertEqual(r.status, expect, f"{method} {path} as {role}: {body!r:.300}")
        return r.status, body

    async def roles(self, method: str, path: str, allowed: str, **kw: Any) -> None:
        """Assert which roles pass the gate: below ``allowed`` gets 403."""
        order = ["viewer", "operator", "admin"]
        for role in order[: order.index(allowed)]:
            status, _ = await self.call(method, path, role, **kw)
            self.assertEqual(status, 403, f"{method} {path} must be refused to {role}")
