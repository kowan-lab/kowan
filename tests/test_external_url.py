"""Settings → Server address: the external URL is a setting, applied live;
KOWAN_EXTERNAL_URL only seeds it once."""

import datetime
import tempfile
import unittest

from aiohttp.test_utils import TestClient, TestServer

from kowan import externalurl
from kowan.config import ServerConfig
from kowan.server import build_app


def _cfg(data_dir, external_url=""):
    return ServerConfig(addr="127.0.0.1:0", data_dir=data_dir, jwt_secret="x" * 48,
                        monitor_interval=datetime.timedelta(seconds=3600),
                        metrics_retention=datetime.timedelta(hours=1), secret_key="", external_url=external_url)


class TestNormalize(unittest.TestCase):
    def test_values(self):
        self.assertEqual(externalurl.normalize(" https://k.example.com/ "), ("https://k.example.com", None))
        self.assertEqual(externalurl.normalize("https://k.example.com/kowan/"), ("https://k.example.com/kowan", None))
        self.assertEqual(externalurl.normalize(""), ("", None))
        for bad in ("k.example.com", "ftp://k.example.com", "https://", "https://k.example.com/?a=1",
                    "https://u:p@k.example.com", "https://k.example.com:99999"):
            self.assertIsNotNone(externalurl.normalize(bad)[1], bad)


class ExternalURLCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.data_dir = tempfile.mkdtemp()
        self.clients = []

    async def asyncTearDown(self):
        for c in self.clients:
            await c.close()

    async def start(self, env_value=""):
        app, svc = await build_app(_cfg(self.data_dir, env_value))
        client = TestClient(TestServer(app))
        await client.start_server()
        self.clients.append(client)
        return client, svc

    async def admin(self, client, svc):
        r = await client.post("/api/v1/auth/init", json={
            "username": "root", "password": "supersecret", "setupToken": svc.setup_token})
        return {"Authorization": "Bearer " + (await r.json())["token"]}

    async def test_env_seeds_once_then_the_setting_wins(self):
        client, svc = await self.start("https://first.example.com/")
        self.assertEqual(svc.config.external_url, "https://first.example.com")
        self.assertEqual(svc.store.get_setting(externalurl.SETTING), "https://first.example.com")
        await client.close()
        _, svc2 = await self.start("https://other.example.com")
        self.assertEqual(svc2.config.external_url, "https://first.example.com")

    async def test_save_applies_live(self):
        client, svc = await self.start()
        h = await self.admin(client, svc)
        r = await client.get("/api/v1/settings/general", headers=h)
        body = await r.json()
        self.assertEqual(body["externalUrl"], "")
        self.assertTrue(body["currentOrigin"].startswith("http://127.0.0.1:"))
        r = await client.get("/api/v1/auth/status")
        self.assertNotIn("Strict-Transport-Security", r.headers)

        r = await client.put("/api/v1/settings/general", headers=h, json={"externalUrl": "https://kowan.invalid/"})
        body = await r.json()
        self.assertEqual(r.status, 200, body)
        self.assertEqual(body["externalUrl"], "https://kowan.invalid")
        self.assertTrue(any("does not answer" in w for w in body["warnings"]))
        r = await client.get("/api/v1/sso/providers", headers=h)
        self.assertEqual((await r.json())["externalUrl"], "https://kowan.invalid")
        r = await client.get("/api/v1/auth/status")
        self.assertIn("Strict-Transport-Security", r.headers)

        # Its own address answers: no reachability warning.
        own = body["currentOrigin"]
        r = await client.put("/api/v1/settings/general", headers=h, json={"externalUrl": own})
        body = await r.json()
        self.assertFalse(any("does not answer" in w for w in body["warnings"]), body)

    async def test_invalid_and_cleared(self):
        client, svc = await self.start("https://seed.example.com")
        h = await self.admin(client, svc)
        r = await client.put("/api/v1/settings/general", headers=h, json={"externalUrl": "kowan.example.com"})
        self.assertEqual(r.status, 400)
        r = await client.put("/api/v1/settings/general", headers=h, json={"externalUrl": "x", "other": 1})
        self.assertEqual(r.status, 400)
        r = await client.put("/api/v1/settings/general", headers=h, json={"externalUrl": ""})
        self.assertEqual(r.status, 200)
        self.assertEqual(svc.config.external_url, "")
        await client.close()
        # Cleared on purpose: the environment does not bring it back.
        _, svc2 = await self.start("https://seed.example.com")
        self.assertEqual(svc2.config.external_url, "")

    async def test_admins_only(self):
        client, svc = await self.start()
        h = await self.admin(client, svc)
        r = await client.post("/api/v1/users", headers=h, json={"username": "eve", "password": "supersecret1", "role": "operator"})
        self.assertIn(r.status, (200, 201), await r.text())
        r = await client.post("/api/v1/auth/login", json={"username": "eve", "password": "supersecret1"})
        eve = {"Authorization": "Bearer " + (await r.json())["token"]}
        r = await client.get("/api/v1/settings/general", headers=eve)
        self.assertEqual(r.status, 403)
        r = await client.put("/api/v1/settings/general", headers=eve, json={"externalUrl": "https://evil.example.com"})
        self.assertEqual(r.status, 403)


if __name__ == "__main__":
    unittest.main()
