"""First-run setup: no admin is seeded without env, and /auth/init bootstraps one.

With KOWAN_ADMIN_PASSWORD unset (seed_admin=False) a fresh server has zero users,
so login fails until the operator creates the first admin through the public
``/auth/init`` endpoint — the browser setup flow, à la Portainer. Once any user
exists the endpoint is a one-shot: it 409s so it can't add accounts later.
"""

import datetime
import os
import tempfile
import unittest

from aiohttp.test_utils import TestClient, TestServer

from kowan.config import ServerConfig
from kowan.server import build_app


def _cfg(data_dir):
    return ServerConfig(
        addr="127.0.0.1:0",
        data_dir=data_dir,
        jwt_secret="x" * 48,
        # seed_admin defaults False: no admin is created from env.
        monitor_interval=datetime.timedelta(seconds=3600),
        metrics_retention=datetime.timedelta(hours=1),
        secret_key="",
        external_url="",
    )


class TestSetupFlow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.data_dir = tempfile.mkdtemp()
        app, self.svc = await build_app(_cfg(self.data_dir))
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_uninitialized_then_init_then_locked(self):
        # Fresh server: not initialized, and login is impossible.
        r = await self.client.get("/api/v1/auth/status")
        self.assertEqual(r.status, 200)
        self.assertEqual((await r.json())["initialized"], False)

        r = await self.client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "admin"}
        )
        self.assertEqual(r.status, 401)

        # Create the first admin via the public setup endpoint.
        # Without the setup token (or with a wrong one) nobody can claim it.
        for bad in ({}, {"setupToken": ""}, {"setupToken": "guess"}):
            r = await self.client.post(
                "/api/v1/auth/init",
                json={"username": "evil", "password": "supersecret", **bad},
            )
            self.assertEqual(r.status, 403, bad)

        token_file = os.path.join(self.data_dir, "setup-token")
        token = open(token_file).read().strip()
        self.assertEqual(os.stat(token_file).st_mode & 0o777, 0o600)
        r = await self.client.post(
            "/api/v1/auth/init",
            json={"username": "root", "password": "supersecret", "setupToken": token},
        )
        self.assertEqual(r.status, 200, await r.text())
        # Used once: the file is gone and the token no longer unlocks anything.
        self.assertFalse(os.path.exists(token_file))
        body = await r.json()
        self.assertIn("token", body)
        self.assertEqual(body["user"]["username"], "root")
        self.assertEqual(body["user"]["role"], "admin")

        # Now initialized, and the created admin can log in.
        r = await self.client.get("/api/v1/auth/status")
        self.assertEqual((await r.json())["initialized"], True)

        r = await self.client.post(
            "/api/v1/auth/login",
            json={"username": "root", "password": "supersecret"},
        )
        self.assertEqual(r.status, 200)

        # The endpoint is a one-shot: it cannot add users once initialized.
        r = await self.client.post(
            "/api/v1/auth/init",
            json={"username": "evil", "password": "anotherpass", "setupToken": token},
        )
        self.assertEqual(r.status, 409)

    async def test_init_rejects_weak_password(self):
        r = await self.client.post(
            "/api/v1/auth/init",
            json={"username": "root", "password": "short", "setupToken": self.svc.setup_token},
        )
        self.assertEqual(r.status, 400)
        # Still uninitialized after a rejected attempt.
        r = await self.client.get("/api/v1/auth/status")
        self.assertEqual((await r.json())["initialized"], False)


    async def test_env_token_is_used_and_no_file_written(self):
        data_dir = tempfile.mkdtemp()
        cfg = _cfg(data_dir)
        cfg.setup_token = "from-provisioning-0123456789"
        _, svc = await build_app(cfg)
        self.assertEqual(svc.setup_token, "from-provisioning-0123456789")
        self.assertFalse(os.path.exists(os.path.join(data_dir, "setup-token")))


if __name__ == "__main__":
    unittest.main()
