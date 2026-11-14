"""Settings that used to be variables: seeded once, then owned by the UI and
applied live."""

import datetime
import os
import tempfile
import unittest
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

from kowan import tunables
from kowan.config import ServerConfig
from kowan.server import build_app

T = tunables.BY_KEY


def _cfg(data_dir, **kw):
    base = dict(addr="127.0.0.1:0", data_dir=data_dir, jwt_secret="x" * 48,
                monitor_interval=datetime.timedelta(seconds=3600),
                metrics_retention=datetime.timedelta(hours=1), secret_key="", external_url="")
    base.update(kw)
    return ServerConfig(**base)


class TestParse(unittest.TestCase):
    def test_durations(self):
        self.assertEqual(tunables.parse(T["audit_retention"], "90d"), (90 * 86400, None))
        self.assertEqual(tunables.parse(T["audit_retention"], "0"), (0.0, None))
        self.assertIn("at least 1d", tunables.parse(T["audit_retention"], "2h")[1])
        self.assertEqual(tunables.parse(T["monitor_interval"], "30"), (30.0, None))
        self.assertIsNotNone(tunables.parse(T["monitor_interval"], "0")[1])
        self.assertIsNotNone(tunables.parse(T["monitor_interval"], "soon")[1])
        self.assertEqual(tunables.canonical(T["reconcile_cooldown"], 5400), "1h30m")

    def test_other_kinds(self):
        self.assertEqual(tunables.parse(T["workflow_runs_keep"], "0"), (0, None))
        self.assertIsNotNone(tunables.parse(T["workflow_runs_keep"], "-3")[1])
        self.assertEqual(tunables.parse(T["compose_validate"], "off"), (False, None))
        self.assertEqual(tunables.parse(T["mcp_allowed_origins"], "https://A.example.com/, http://b:8080"),
                         (["https://a.example.com", "http://b:8080"], None))
        self.assertIsNotNone(tunables.parse(T["mcp_allowed_origins"], "https://a.example.com/path")[1])
        self.assertIsNotNone(tunables.parse(T["agent_image"], "a b")[1])


class TunablesCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.data_dir = tempfile.mkdtemp()
        self.clients = []

    async def asyncTearDown(self):
        for c in self.clients:
            await c.close()

    async def start(self, **cfg):
        app, svc = await build_app(_cfg(self.data_dir, **cfg))
        client = TestClient(TestServer(app))
        await client.start_server()
        self.clients.append(client)
        return client, svc

    async def admin(self, client, svc):
        r = await client.post("/api/v1/auth/init", json={
            "username": "root", "password": "supersecret", "setupToken": svc.setup_token})
        return {"Authorization": "Bearer " + (await r.json())["token"]}

    async def test_variables_seed_once(self):
        env = {"KOWAN_GIT_TIMEOUT": "300", "KOWAN_MCP_ALLOWED_ORIGINS": "https://app.example.com",
               "KOWAN_SSO_ONLY": "", "KOWAN_RECONCILE_INTERVAL": "nonsense"}
        with mock.patch.dict(os.environ, env):
            client, svc = await self.start(audit_retention=datetime.timedelta(days=7))
        self.assertEqual(tunables.text("git_timeout"), "5m")
        self.assertEqual(tunables.value("mcp_allowed_origins"), ["https://app.example.com"])
        self.assertEqual(tunables.text("audit_retention"), "7d")
        self.assertEqual(tunables.text("monitor_interval"), "1h")  # from the config
        self.assertEqual(tunables.value("reconcile_interval"), 5.0)  # invalid: default, not saved
        self.assertFalse(tunables.is_saved("reconcile_interval"))
        self.assertFalse(tunables.is_saved("release_repository"))  # the default is not copied
        await client.close()
        # Next start: the saved values win over a changed variable.
        with mock.patch.dict(os.environ, {"KOWAN_GIT_TIMEOUT": "60"}):
            await self.start()
        self.assertEqual(tunables.text("git_timeout"), "5m")

    async def test_api_saves_all_or_nothing_and_applies_live(self):
        client, svc = await self.start()
        h = await self.admin(client, svc)
        r = await client.get("/api/v1/settings/tunables?group=retention", headers=h)
        rows = (await r.json())["settings"]
        self.assertEqual({x["key"] for x in rows},
                         {"metrics_retention", "audit_retention", "alert_retention", "workflow_runs_keep"})

        r = await client.put("/api/v1/settings/tunables", headers=h, json={
            "values": {"audit_retention": "30d", "workflow_runs_keep": "many"}})
        body = await r.json()
        self.assertEqual(r.status, 400)
        self.assertIn("workflow_runs_keep", body["fields"])
        self.assertEqual(tunables.text("audit_retention"), "90d")  # nothing saved

        r = await client.put("/api/v1/settings/tunables", headers=h, json={
            "values": {"audit_retention": "720h", "mcp_allowed_origins": "https://app.example.com"},
            "group": "retention"})
        self.assertEqual(r.status, 200, await r.text())
        self.assertEqual(tunables.value("audit_retention"), 30 * 86400)
        self.assertEqual(svc.monitor._audit_retention, 30 * 86400)

        # The MCP origin check reads it at once.
        r = await client.post("/mcp", headers={**h, "Origin": "https://app.example.com"},
                              json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        self.assertNotEqual(r.status, 403)
        r = await client.post("/mcp", headers={**h, "Origin": "https://evil.example.com"},
                              json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        self.assertEqual(r.status, 403)

        # null: back to the default.
        r = await client.put("/api/v1/settings/tunables", headers=h, json={"values": {"audit_retention": None}})
        self.assertEqual(r.status, 200)
        self.assertEqual(tunables.text("audit_retention"), "90d")
        self.assertFalse(svc.store.setting_exists("tunable.audit_retention"))

    async def test_release_repository_can_be_turned_off(self):
        client, svc = await self.start()
        h = await self.admin(client, svc)
        self.assertTrue(svc.releases.enabled)
        await client.put("/api/v1/settings/tunables", headers=h, json={"values": {"release_repository": ""}})
        self.assertFalse(svc.releases.enabled)

    async def test_admins_only(self):
        client, svc = await self.start()
        h = await self.admin(client, svc)
        await client.post("/api/v1/users", headers=h, json={"username": "op", "password": "supersecret1", "role": "operator"})
        r = await client.post("/api/v1/auth/login", json={"username": "op", "password": "supersecret1"})
        op = {"Authorization": "Bearer " + (await r.json())["token"]}
        r = await client.get("/api/v1/settings/tunables", headers=op)
        self.assertEqual(r.status, 403)
        r = await client.put("/api/v1/settings/tunables", headers=op, json={"values": {"agent_image": "evil/agent"}})
        self.assertEqual(r.status, 403)

    async def test_sso_variables_seed_only(self):
        from kowan.api import sso as sso_api

        client, svc = await self.start()
        svc.store.set_setting(sso_api.SETTING_BREAK_GLASS, "chosen-in-ui")
        sso_api.bootstrap_from_env(svc, {"KOWAN_SSO_BREAK_GLASS_USER": "from-env"})
        self.assertEqual(svc.store.get_setting(sso_api.SETTING_BREAK_GLASS), "chosen-in-ui")


if __name__ == "__main__":
    unittest.main()
