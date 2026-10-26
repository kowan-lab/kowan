"""JWT sessions are checked against the database on every request.

A valid signature is not enough: logout revokes the token, a password change
ends every session of that user, a deleted user is locked out at once, and the
role comes from the database, so a demotion applies to tokens already issued.
"""

import datetime
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
        monitor_interval=datetime.timedelta(seconds=3600),
        metrics_retention=datetime.timedelta(hours=1),
        secret_key="",
        external_url="",
    )


class TestSessions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        app, self.svc = await build_app(_cfg())
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        r = await self.client.post("/api/v1/auth/init", json={
            "username": "root", "password": "supersecret", "setupToken": self.svc.setup_token})
        self.admin = {"Authorization": "Bearer " + (await r.json())["token"]}
        r = await self.client.post("/api/v1/users", headers=self.admin, json={
            "username": "ops", "password": "opspassword", "role": "operator"})
        self.assertEqual(r.status, 201, await r.text())
        self.ops_id = (await r.json())["id"]

    async def asyncTearDown(self):
        await self.client.close()

    async def _login(self, username, password):
        r = await self.client.post("/api/v1/auth/login", json={"username": username, "password": password})
        self.assertEqual(r.status, 200)
        return {"Authorization": "Bearer " + (await r.json())["token"]}

    async def _status(self, method, path, headers, **kw):
        r = await self.client.request(method, "/api/v1" + path, headers=headers, **kw)
        return r.status

    async def test_logout_revokes_only_that_token(self):
        first = await self._login("ops", "opspassword")
        second = await self._login("ops", "opspassword")
        self.assertEqual(await self._status("POST", "/auth/logout", first), 200)
        self.assertEqual(await self._status("GET", "/me", first), 401)
        self.assertEqual(await self._status("GET", "/me", second), 200)

    async def test_demotion_applies_to_issued_tokens(self):
        ops = await self._login("ops", "opspassword")
        # Operator: past the role gate (400 for the empty body), not 403.
        self.assertEqual(await self._status("POST", "/workflows", ops, json={}), 400)
        r = await self.client.put(f"/api/v1/users/{self.ops_id}", headers=self.admin, json={"role": "viewer"})
        self.assertEqual(r.status, 200)
        # The role change ends the session; signing in again gives the new role.
        self.assertEqual(await self._status("POST", "/workflows", ops, json={}), 401)
        viewer = await self._login("ops", "opspassword")
        self.assertEqual(await self._status("POST", "/workflows", viewer, json={}), 403)

    async def test_password_change_ends_sessions(self):
        ops = await self._login("ops", "opspassword")
        r = await self.client.put(f"/api/v1/users/{self.ops_id}", headers=self.admin, json={"password": "newpassword1"})
        self.assertEqual(r.status, 200)
        self.assertEqual(await self._status("GET", "/me", ops), 401)
        fresh = await self._login("ops", "newpassword1")
        self.assertEqual(await self._status("GET", "/me", fresh), 200)

    async def test_deleted_user_is_locked_out(self):
        ops = await self._login("ops", "opspassword")
        r = await self.client.delete(f"/api/v1/users/{self.ops_id}", headers=self.admin)
        self.assertEqual(r.status, 200)
        self.assertEqual(await self._status("GET", "/me", ops), 401)

    async def test_forged_role_claim_is_ignored(self):
        # A token signed with the right key but claiming admin for an operator.
        forged = self.svc.jwt.issue(self.ops_id, "ops", "admin")
        h = {"Authorization": "Bearer " + forged}
        self.assertEqual(await self._status("GET", "/users", h), 403)

    async def test_own_password_change_keeps_this_tab_signed_in(self):
        r = await self.client.get("/api/v1/me", headers=self.admin)
        me = (await r.json())["id"]
        r = await self.client.put(f"/api/v1/users/{me}", headers=self.admin, json={"password": "another-secret"})
        self.assertEqual(r.status, 200)
        fresh = {"Authorization": "Bearer " + r.headers["X-Session-Token"]}
        self.assertEqual(await self._status("GET", "/me", self.admin), 401)
        self.assertEqual(await self._status("GET", "/me", fresh), 200)
        # Changing someone else's password hands back no token.
        r = await self.client.put(f"/api/v1/users/{self.ops_id}", headers=fresh, json={"password": "ops-new-pass"})
        self.assertNotIn("X-Session-Token", r.headers)

    async def test_user_update_keeps_email_when_omitted(self):
        r = await self.client.put(f"/api/v1/users/{self.ops_id}", headers=self.admin, json={"email": "ops@example.com"})
        self.assertEqual(r.status, 200)
        r = await self.client.put(f"/api/v1/users/{self.ops_id}", headers=self.admin, json={"role": "viewer"})
        self.assertEqual((await r.json())["email"], "ops@example.com")


if __name__ == "__main__":
    unittest.main()
