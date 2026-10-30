"""Users and auth REST: first-run init rules, login/logout and status, user
CRUD with validation, role changes revoking sessions, and self-protection."""

import unittest

from tests.apicase import ServerCase


class TestUsersApi(ServerCase):
    async def test_init_is_once(self):
        r = await self.client.post("/api/v1/auth/init", json={
            "username": "again", "password": "supersecret", "setupToken": self.svc.setup_token})
        self.assertEqual(r.status, 409)
        r = await self.client.get("/api/v1/auth/status")
        self.assertTrue((await r.json())["initialized"])

    async def test_user_crud_and_validation(self):
        await self.roles("GET", "/users", "admin")
        _, u = await self.call("POST", "/users", "admin", json={
            "username": "dana", "password": "dana-password", "role": "viewer"}, expect=201)
        self.assertNotIn("password", str(u).lower().replace("haspassword", ""))
        _, got = await self.call("GET", f"/users/{u['id']}", "admin", expect=200)
        self.assertEqual(got["role"], "viewer")
        for bad in ({"username": "", "password": "x" * 12}, {"username": "e", "password": "short"},
                    {"username": "e", "password": "long-enough-pw", "role": "god"}):
            await self.call("POST", "/users", "admin", json=bad, expect=400)
        await self.call("POST", "/users", "admin", json=[1], expect=400)
        await self.call("POST", "/users", "admin", json={"username": "dana", "password": "dana-password", "role": "viewer"},
                        expect=409)

        # Log in as dana, then promote her: the old token stops working (sessions revoked).
        r = await self.client.post("/api/v1/auth/login", json={"username": "dana", "password": "dana-password"})
        token = (await r.json())["token"]
        hdr = {"Authorization": "Bearer " + token}
        self.assertEqual((await self.client.get("/api/v1/me", headers=hdr)).status, 200)
        _, upd = await self.call("PUT", f"/users/{u['id']}", "admin", json={"role": "operator"}, expect=200)
        self.assertEqual(upd["role"], "operator")
        self.assertEqual((await self.client.get("/api/v1/me", headers=hdr)).status, 401)
        await self.call("PUT", f"/users/{u['id']}", "admin", json={"role": "king"}, expect=400)
        await self.call("PUT", f"/users/{u['id']}", "admin", json={"password": "x"}, expect=400)
        await self.call("PUT", f"/users/{u['id']}", "admin", json={"password": "a-new-password"}, expect=200)
        r = await self.client.post("/api/v1/auth/login", json={"username": "dana", "password": "a-new-password"})
        self.assertEqual(r.status, 200)
        await self.call("PUT", f"/users/{u['id']}", "admin", json=[1], expect=400)
        await self.call("PUT", "/users/missing", "admin", json={"role": "viewer"}, expect=404)
        await self.call("GET", "/users/missing", "admin", expect=404)

        # Logout revokes the token.
        r = await self.client.post("/api/v1/auth/login", json={"username": "dana", "password": "a-new-password"})
        hdr = {"Authorization": "Bearer " + (await r.json())["token"]}
        r = await self.client.post("/api/v1/auth/logout", headers=hdr)
        self.assertEqual(r.status, 200)
        self.assertEqual((await self.client.get("/api/v1/me", headers=hdr)).status, 401)

        await self.call("DELETE", f"/users/{u['id']}", "admin", expect=200)
        await self.call("DELETE", f"/users/{u['id']}", "admin", expect=404)
        _, me = await self.call("GET", "/me", "admin", expect=200)
        await self.call("DELETE", f"/users/{me['id']}", "admin", expect=400)

    async def test_every_user_changes_their_own_password(self):
        # A viewer cannot touch /users/{id}, but changes their own password.
        _, me = await self.call("GET", "/me", "viewer", expect=200)
        await self.call("PUT", f"/users/{me['id']}", "viewer", json={"password": "new-password-1"}, expect=403)
        for bad in ({"newPassword": "new-password-1"}, {"currentPassword": "viewer-password"},
                    {"currentPassword": "viewer-password", "newPassword": "short"},
                    {"currentPassword": "viewer-password", "newPassword": "new-password-1", "role": "admin"}):
            await self.call("PUT", "/me/password", "viewer", json=bad, expect=400)
        _, body = await self.call("PUT", "/me/password", "viewer", json={
            "currentPassword": "wrong-password", "newPassword": "new-password-1"}, expect=400)
        self.assertIn("current password", body["error"])

        old_session = self.h["viewer"]
        r = await self.client.put("/api/v1/me/password", headers=old_session, json={
            "currentPassword": "viewer-password", "newPassword": "new-password-1"})
        self.assertEqual(r.status, 200, await r.text())
        fresh = r.headers["X-Session-Token"]
        # Other sessions end; this one carries on with the fresh token.
        r = await self.client.get("/api/v1/me", headers=old_session)
        self.assertEqual(r.status, 401)
        r = await self.client.get("/api/v1/me", headers={"Authorization": "Bearer " + fresh})
        self.assertEqual(r.status, 200)
        r = await self.client.post("/api/v1/auth/login", json={"username": "viewer", "password": "viewer-password"})
        self.assertEqual(r.status, 401)
        r = await self.client.post("/api/v1/auth/login", json={"username": "viewer", "password": "new-password-1"})
        self.assertEqual(r.status, 200)

        # An API key has no password.
        _, key = await self.call("POST", "/apikeys", json={"name": "ci", "role": "viewer"}, expect=201)
        r = await self.client.put("/api/v1/me/password", headers={"X-API-Key": key["secret"]}, json={
            "currentPassword": "x", "newPassword": "new-password-1"})
        self.assertEqual(r.status, 400)

    async def test_wrong_current_password_is_rate_limited(self):
        statuses = []
        for _ in range(12):
            status, _ = await self.call("PUT", "/me/password", "operator", json={
                "currentPassword": "guess-password", "newPassword": "new-password-1"})
            statuses.append(status)
        self.assertIn(429, statuses)

    async def test_login_failures(self):
        r = await self.client.post("/api/v1/auth/login", json={"username": "viewer", "password": "nope"})
        self.assertEqual(r.status, 401)
        r = await self.client.post("/api/v1/auth/login", data=b"not json")
        self.assertEqual(r.status, 400)
        r = await self.client.post("/api/v1/auth/login", json={"username": "", "password": ""})
        self.assertIn(r.status, (400, 401))


if __name__ == "__main__":
    unittest.main()
