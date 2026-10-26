"""End-to-end integration test: boot the whole control plane in-process
(aiohttp TestClient) and exercise auth + the REST surface against a real
SQLite store. No Docker daemon is required (only non-docker endpoints are
hit); docker-backed routes are covered elsewhere.
"""

import datetime
import json
import tempfile
import unittest

from aiohttp.test_utils import TestClient, TestServer

from kowan.config import ServerConfig
from kowan.server import build_app


def _cfg():
    return ServerConfig(
        addr="127.0.0.1:0",
        data_dir=tempfile.mkdtemp(),
        jwt_secret="x" * 48,
        admin_user="admin",
        admin_password="admin",
        seed_admin=True,  # seed admin/admin for the test (env-bootstrap path)
        # Long intervals so background workers don't actually loop during the test.
        monitor_interval=datetime.timedelta(seconds=3600),
        metrics_retention=datetime.timedelta(hours=1),
        secret_key="",
        external_url="",
    )


class TestControlPlane(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        app, self.svc = await build_app(_cfg())
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        r = await self.client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "admin"}
        )
        self.assertEqual(r.status, 200, await r.text())
        self.token = (await r.json())["token"]
        self.H = {"Authorization": f"Bearer {self.token}"}

    async def asyncTearDown(self):
        await self.client.close()

    async def get(self, p):
        r = await self.client.get(p, headers=self.H)
        return r.status, await r.text()

    async def post(self, p, body):
        r = await self.client.post(p, headers=self.H, json=body)
        return r.status, await r.text()

    async def put(self, p, body):
        r = await self.client.put(p, headers=self.H, json=body)
        return r.status, await r.text()

    # ---------------------------------------------------------------- #
    async def test_health_public(self):
        r = await self.client.get("/health")
        self.assertEqual(r.status, 200)
        self.assertEqual((await r.text()).strip(), "ok")

    async def test_bad_login_401(self):
        r = await self.client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "nope"}
        )
        self.assertEqual(r.status, 401)

    async def test_unauthenticated_is_401(self):
        r = await self.client.get("/api/v1/hosts")
        self.assertEqual(r.status, 401)

    async def test_me_and_reads(self):
        for p in [
            "/api/v1/me", "/api/v1/hosts",
            "/api/v1/credentials", "/api/v1/credentials?kind=secret",
            "/api/v1/credentials?kind=registry", "/api/v1/credentials?kind=channel",
            "/api/v1/credentials/types", "/api/v1/credentials/types?kind=registry",
            "/api/v1/workflows",
            "/api/v1/audit", "/api/v1/apikeys",
            "/api/v1/deployments", "/api/v1/notifications",
            "/api/v1/notifications/unread-count", "/api/v1/alert-rules", "/api/v1/alerts",
        ]:
            s, b = await self.get(p)
            self.assertIn(s, (200, 204), f"{p} -> {s} {b[:120]}")

    async def test_local_host_present(self):
        s, b = await self.get("/api/v1/hosts")
        self.assertEqual(s, 200)
        self.assertIn("local", b)

    async def test_secret_masked(self):
        s, _ = await self.post(
            "/api/v1/credentials",
            {"kind": "secret", "name": "db", "type": "generic", "fields": {"password": "hunter2"}},
        )
        self.assertIn(s, (200, 201))
        s, b = await self.get("/api/v1/credentials?kind=secret")
        self.assertNotIn("hunter2", b)
        self.assertIn("password", b)  # field NAME is exposed, value is not

    async def test_credentials_channel_and_reveal(self):
        # Create a channel credential; its webhook URL (sensitive) must be masked.
        s, b = await self.post(
            "/api/v1/credentials",
            {"kind": "channel", "name": "chat", "type": "slack",
             "enabled": True, "minLevel": "warning",
             "config": {"url": "https://hooks.example/xyz"}},
        )
        self.assertIn(s, (200, 201), b[:150])
        cid = json.loads(b)["id"]
        s, b = await self.get("/api/v1/credentials?kind=channel")
        self.assertNotIn("hooks.example", b)  # sensitive URL masked
        self.assertIn("********", b)
        # Reveal is secrets-only: a channel must be rejected.
        s, _ = await self.post(f"/api/v1/credentials/{cid}/reveal", {})
        self.assertEqual(s, 400)

    async def test_connection_and_reference(self):
        # A reusable connection (a Docker login) — its secret must be masked on read.
        s, b = await self.post(
            "/api/v1/credentials",
            {"kind": "connection", "name": "hub-login", "type": "generic",
             "meta": {"username": "me"}, "config": {"password": "s3cretpw"}},
        )
        self.assertIn(s, (200, 201), b[:150])
        cid = json.loads(b)["id"]
        s, b = await self.get("/api/v1/credentials?kind=connection")
        self.assertNotIn("s3cretpw", b)
        self.assertIn("********", b)
        # A registry that DELEGATES its auth to the shared connection.
        s, b = await self.post(
            "/api/v1/credentials",
            {"kind": "registry", "name": "hub", "url": "docker.io", "type": "generic",
             "connectionRef": cid},
        )
        self.assertIn(s, (200, 201), b[:150])
        s, b = await self.get("/api/v1/credentials?kind=registry")
        self.assertIn("connectionRef", b)
        # The connection-types catalog advertises capabilities.
        s, b = await self.get("/api/v1/credentials/types?kind=connection")
        self.assertEqual(s, 200)
        self.assertIn("dockerRegistryAuth", b)

    async def test_secret_delegates_to_vault_connection(self):
        # A shared Vault connection (backend address + token).
        s, b = await self.post(
            "/api/v1/credentials",
            {"kind": "connection", "name": "vault-prod", "type": "vault",
             "config": {"address": "https://vault:8200", "token": "s.roottoken"}},
        )
        self.assertIn(s, (200, 201), b[:150])
        # Its capability catalog advertises secretFetch.
        self.assertIn("secretFetch", b)
        cid = json.loads(b)["id"]
        # A secret that DELEGATES to it needs only its own selectors (path/key);
        # the sensitive backend (address/token) comes from the connection, so the
        # normally-required external fields must NOT be demanded here.
        s, b = await self.post(
            "/api/v1/credentials",
            {"kind": "secret", "name": "dbpass", "type": "vault",
             "connectionRef": cid, "fields": {"path": "app/db", "key": "password"}},
        )
        self.assertIn(s, (200, 201), b[:150])
        # A standalone vault secret (no connectionRef) still must carry the backend.
        s, b = await self.post(
            "/api/v1/credentials",
            {"kind": "secret", "name": "bad", "type": "vault",
             "fields": {"path": "app/db", "key": "password"}},
        )
        self.assertEqual(s, 400, b[:150])

    async def test_native_git_connection(self):
        # A github connection (native type): username -> meta (shown), token -> data
        # (masked). The connection catalog advertises it with gitFetch.
        s, b = await self.get("/api/v1/credentials/types?kind=connection")
        self.assertEqual(s, 200)
        self.assertIn("github", b)
        self.assertIn("gitFetch", b)
        s, b = await self.post(
            "/api/v1/credentials",
            {"kind": "connection", "name": "gh", "type": "github",
             "meta": {"username": "me"}, "config": {"token": "ghp_secretvalue"}},
        )
        self.assertIn(s, (200, 201), b[:150])
        self.assertIn("gitFetch", b)  # capabilities advertised on the record
        s, b = await self.get("/api/v1/credentials?kind=connection")
        self.assertNotIn("ghp_secretvalue", b)  # token masked
        self.assertIn("********", b)

    async def test_cloud_connection_secret_masked(self):
        # An aws connection's secretAccessKey must be masked on read (regression:
        # the sensitivity resolver must recognize cloud types via their facet schema).
        s, b = await self.post(
            "/api/v1/credentials",
            {"kind": "connection", "name": "aws1", "type": "aws",
             "config": {"region": "eu", "accessKeyId": "AKIAX", "secretAccessKey": "topsecretkey"}},
        )
        self.assertIn(s, (200, 201), b[:150])
        s, b = await self.get("/api/v1/credentials?kind=connection")
        self.assertNotIn("topsecretkey", b)
        self.assertIn("********", b)

    async def test_apperror_maps_to_status(self):
        # An invalid alert-rule type raises Validation -> must be 400, not 500.
        s, b = await self.post("/api/v1/alert-rules", {"name": "x", "type": "BOGUS"})
        self.assertEqual(s, 400, f"{s} {b[:120]}")

    async def test_write_roundtrips(self):
        # host (edge mode -> response wraps the host + join command)
        s, b = await self.post(
            "/api/v1/hosts",
            {"name": "edge1", "mode": "edge", "address": ""},
        )
        self.assertIn(s, (200, 201), b[:150])
        payload = json.loads(b)
        self.assertIn("id", payload.get("host", {}), f"host not serialised: {b[:200]}")
        # persisted -> shows up in the list
        s, b = await self.get("/api/v1/hosts")
        self.assertEqual(s, 200)
        self.assertIn("edge1", b)
        # workflow
        s, b = await self.post(
            "/api/v1/workflows", {"name": "wf1", "enabled": False, "steps": [], "triggers": []}
        )
        self.assertIn(s, (200, 201), b[:150])
        # registry (password masked on read)
        s, b = await self.post(
            "/api/v1/credentials",
            {"kind": "registry", "name": "dh", "url": "docker.io", "type": "generic",
             "username": "u", "password": "secretpw"},
        )
        self.assertIn(s, (200, 201), b[:150])
        s, b = await self.get("/api/v1/credentials?kind=registry")
        self.assertNotIn("secretpw", b)
        # alert rule (valid)
        s, b = await self.post(
            "/api/v1/alert-rules",
            {"name": "cpu", "type": "cpu_high", "operator": ">", "threshold": 90, "severity": "warning", "enabled": True},
        )
        self.assertIn(s, (200, 201), b[:150])
        # api key
        s, b = await self.post("/api/v1/apikeys", {"name": "ci", "role": "operator"})
        self.assertIn(s, (200, 201), b[:150])
        # deployment
        s, b = await self.post(
            "/api/v1/deployments",
            {"name": "web", "type": "compose", "strategy": "standalone", "targets": ["local"],
             "compose": "services:\n  web:\n    image: nginx\n"},
        )
        self.assertIn(s, (200, 201), b[:150])


if __name__ == "__main__":
    unittest.main()
