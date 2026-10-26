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

    async def test_login_failures(self):
        r = await self.client.post("/api/v1/auth/login", json={"username": "viewer", "password": "nope"})
        self.assertEqual(r.status, 401)
        r = await self.client.post("/api/v1/auth/login", data=b"not json")
        self.assertEqual(r.status, 400)
        r = await self.client.post("/api/v1/auth/login", json={"username": "", "password": ""})
        self.assertIn(r.status, (400, 401))


if __name__ == "__main__":
    unittest.main()
