"""Settings > Network access: admins narrow who may connect, with their
password, without being able to lock themselves out or widen the
environment's lists."""

import datetime
import tempfile
import unittest

from aiohttp.test_utils import TestClient, TestServer

from kowan.config import ServerConfig
from kowan.server import build_app


def _cfg(data_dir, **kw):
    return ServerConfig(addr="127.0.0.1:0", data_dir=data_dir, jwt_secret="x" * 48,
                        monitor_interval=datetime.timedelta(seconds=3600),
                        metrics_retention=datetime.timedelta(hours=1), secret_key="", external_url="", **kw)


class NetAccessCase(unittest.IsolatedAsyncioTestCase):
    env = {}

    async def asyncSetUp(self):
        self.data_dir = tempfile.mkdtemp()
        await self._start()
        r = await self.client.post("/api/v1/auth/init", json={
            "username": "root", "password": "supersecret", "setupToken": self.svc.setup_token})
        self.admin = {"Authorization": "Bearer " + (await r.json())["token"]}

    async def _start(self):
        app, self.svc = await build_app(_cfg(self.data_dir, **self.env))
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def put(self, body, headers=None):
        r = await self.client.put("/api/v1/settings/network", headers=headers or self.admin, json=body)
        return r.status, await r.json()


class TestNetworkAccess(NetAccessCase):
    async def test_password_is_required(self):
        st, _ = await self.put({"uiAllow": ["127.0.0.0/8"]})
        self.assertEqual(st, 403)
        st, _ = await self.put({"uiAllow": ["127.0.0.0/8"], "password": "wrong-one"})
        self.assertEqual(st, 403)
        st, body = await self.put({"uiAllow": ["127.0.0.0/8"], "edgeAllow": ["198.51.100.0/24"], "password": "supersecret"})
        self.assertEqual(st, 200, body)
        self.assertEqual((body["uiAllow"], body["edgeAllow"]), (["127.0.0.0/8"], ["198.51.100.0/24"]))

    async def test_cannot_lock_yourself_out(self):
        st, body = await self.put({"uiAllow": ["192.0.2.0/24"], "password": "supersecret"})
        self.assertEqual(st, 409)
        self.assertIn("lock you out", body["error"])
        # Nothing was saved: the UI still answers.
        r = await self.client.get("/api/v1/settings/network", headers=self.admin)
        self.assertEqual(r.status, 200)
        self.assertEqual((await r.json())["uiAllow"], [])

    async def test_bad_entries_are_rejected(self):
        st, _ = await self.put({"uiAllow": ["10.0.0.0/33"], "password": "supersecret"})
        self.assertEqual(st, 400)
        st, _ = await self.put({"uiAllow": "10.0.0.0/8", "password": "supersecret"})
        self.assertEqual(st, 400)
        st, _ = await self.put({"uiAllow": [], "trustedProxies": ["0.0.0.0/0"], "password": "supersecret"})
        self.assertEqual(st, 400)  # proxies are environment-only

    async def test_lists_survive_a_restart_and_apply(self):
        st, _ = await self.put({"uiAllow": ["127.0.0.1"], "password": "supersecret"})
        self.assertEqual(st, 200)
        await self.client.close()
        await self._start()
        self.assertEqual([str(n) for n in self.svc.netpolicy.ui_allow_stored], ["127.0.0.1/32"])
        r = await self.client.get("/api/v1/settings/network", headers=self.admin)
        self.assertEqual((await r.json())["uiAllow"], ["127.0.0.1"])
        r = await self.client.get("/api/v1/auth/status")
        self.assertEqual(r.status, 200)

    async def test_non_admins_cannot_see_or_change(self):
        await self.client.post("/api/v1/users", headers=self.admin, json={
            "username": "ops", "password": "opspassword", "role": "operator"})
        r = await self.client.post("/api/v1/auth/login", json={"username": "ops", "password": "opspassword"})
        ops = {"Authorization": "Bearer " + (await r.json())["token"]}
        self.assertEqual((await self.client.get("/api/v1/settings/network", headers=ops)).status, 403)
        st, _ = await self.put({"uiAllow": [], "password": "opspassword"}, headers=ops)
        self.assertEqual(st, 403)


class TestEnvironmentIsTheOuterLimit(NetAccessCase):
    env = {"ui_allow_cidrs": "127.0.0.0/8"}

    async def test_ui_list_can_only_narrow(self):
        # 127.0.0.1 is in the environment list but not in this one: refused,
        # because together they would leave this connection out.
        st, _ = await self.put({"uiAllow": ["10.0.0.0/8"], "password": "supersecret"})
        self.assertEqual(st, 409)
        st, body = await self.put({"uiAllow": ["127.0.0.1", "10.0.0.0/8"], "password": "supersecret"})
        self.assertEqual(st, 200)
        self.assertTrue(any("10.0.0.0/8" in w for w in body["warnings"]))
        self.assertEqual(body["env"]["uiAllow"], ["127.0.0.0/8"])


if __name__ == "__main__":
    unittest.main()
