"""Teams and grants REST validation: CRUD with name conflicts, members, every
grant scope's rules, deletion and the effective-permission views."""

import unittest

from tests.apicase import ServerCase


class TestTeamsApi(ServerCase):
    async def user_id(self, name):
        _, users = await self.call("GET", "/users", "admin", expect=200)
        return next(u["id"] for u in users if u["username"] == name)

    async def test_team_crud_and_members(self):
        await self.roles("GET", "/teams", "admin")
        _, t = await self.call("POST", "/teams", json={"name": "web", "description": "front"}, expect=201)
        await self.call("POST", "/teams", json={"name": "WEB"}, expect=409)
        await self.call("POST", "/teams", json={"name": " "}, expect=400)
        await self.call("POST", "/teams", json={"name": "x", "extra": 1}, expect=400)
        _, other = await self.call("POST", "/teams", json={"name": "ops"}, expect=201)

        await self.call("PUT", f"/teams/{t['id']}", json={"name": "ops"}, expect=409)
        await self.call("PUT", f"/teams/{t['id']}", json={"name": ""}, expect=400)
        await self.call("PUT", f"/teams/{t['id']}", json={"bogus": 1}, expect=400)
        _, upd = await self.call("PUT", f"/teams/{t['id']}", json={"name": "Web", "description": "d2"}, expect=200)
        self.assertEqual((upd["name"], upd["description"]), ("Web", "d2"))
        await self.call("PUT", "/teams/missing", json={"name": "z"}, expect=404)

        uid = await self.user_id("operator")
        await self.call("POST", f"/teams/{t['id']}/members", json={"userId": uid}, expect=201)
        await self.call("POST", f"/teams/{t['id']}/members", json={"userId": uid}, expect=200)
        await self.call("POST", f"/teams/{t['id']}/members", json={"userId": "nobody"}, expect=400)
        await self.call("POST", f"/teams/{t['id']}/members", json={"who": uid}, expect=400)
        await self.call("POST", "/teams/missing/members", json={"userId": uid}, expect=404)
        _, got = await self.call("GET", f"/teams/{t['id']}", expect=200)
        self.assertEqual(got["grants"], [])
        await self.call("GET", "/teams/missing", expect=404)

        _, perms = await self.call("GET", f"/users/{uid}/permissions", expect=200)
        self.assertEqual(perms["mode"], "scoped")
        self.assertEqual(perms["identities"], [])
        await self.call("GET", "/users/missing/permissions", expect=404)

        await self.call("DELETE", f"/teams/{t['id']}/members/{uid}", expect=200)
        await self.call("DELETE", f"/teams/{t['id']}", expect=200)
        await self.call("DELETE", f"/teams/{t['id']}", expect=404)
        _, listing = await self.call("GET", "/teams", expect=200)
        self.assertEqual([x["id"] for x in listing], [other["id"]])

    async def test_team_with_bound_keys_cannot_be_deleted(self):
        # Unbinding would leave the key acting with its full role: fail closed.
        _, t = await self.call("POST", "/teams", json={"name": "ci"}, expect=201)
        _, key = await self.call("POST", "/apikeys", json={"name": "deployer", "role": "operator",
                                                           "teamId": t["id"]}, expect=201)
        _, body = await self.call("DELETE", f"/teams/{t['id']}", expect=409)
        self.assertIn("deployer", body["error"])
        r = await self.client.get("/api/v1/me/permissions", headers={"X-API-Key": key["secret"]})
        self.assertEqual((await r.json())["mode"], "scoped")

        await self.call("DELETE", f"/apikeys/{key['apiKey']['id']}", expect=200)
        await self.call("DELETE", f"/teams/{t['id']}", expect=200)

    async def test_store_team_delete_keeps_keys_bound(self):
        # Even below the API, a key bound to a deleted team stays scoped (no grants).
        _, t = await self.call("POST", "/teams", json={"name": "gone"}, expect=201)
        _, key = await self.call("POST", "/apikeys", json={"name": "k", "role": "operator",
                                                           "teamId": t["id"]}, expect=201)
        self.svc.store.delete_team(t["id"])
        r = await self.client.get("/api/v1/me/permissions", headers={"X-API-Key": key["secret"]})
        me = await r.json()
        self.assertEqual((me["mode"], me["grants"]), ("scoped", []))

    async def test_grant_validation_per_scope(self):
        _, t = await self.call("POST", "/teams", json={"name": "g"}, expect=201)
        base = {"subjectType": "team", "subjectId": t["id"], "level": "read"}
        cases = [
            ({"scope": "all_hosts"}, 201),
            ({"scope": "host", "hostId": "local"}, 201),
            ({"scope": "host", "hostId": "nope"}, 400),
            ({"scope": "deployment", "target": "missing"}, 400),
            ({"scope": "stack", "hostId": "local", "target": "shop"}, 201),
            ({"scope": "stack", "hostId": "local", "target": ""}, 400),
            ({"scope": "stack", "hostId": "nope", "target": "x"}, 400),
            ({"scope": "labels", "target": "team=web"}, 201),
            ({"scope": "labels", "target": "no-equals"}, 400),
            ({"scope": "labels", "target": "a=b", "hostId": "nope"}, 400),
            ({"scope": "planet"}, 400),
            ({"scope": "all_hosts", "level": "god"}, 400),
            ({"scope": "all_hosts", "subjectType": "robot"}, 400),
            ({"scope": "all_hosts", "subjectType": "user", "subjectId": "missing"}, 400),
            ({"scope": "all_hosts", "subjectType": "apikey", "subjectId": "missing"}, 400),
            ({"scope": "all_hosts", "colour": "red"}, 400),
        ]
        ids = []
        for extra, want in cases:
            status, body = await self.call("POST", "/grants", json={**base, **extra})
            self.assertEqual(status, want, (extra, body))
            if status == 201:
                ids.append(body["id"])
        await self.call("POST", "/grants", json=[1], expect=400)
        _, listing = await self.call("GET", "/grants", params={"subjectType": "team", "subjectId": t["id"]}, expect=200)
        self.assertEqual(len(listing), 4)
        _, team = await self.call("GET", "/teams", expect=200)
        self.assertEqual(len(team[0]["grants"]), 4)
        for gid in ids:
            await self.call("DELETE", f"/grants/{gid}", expect=200)
        await self.call("DELETE", f"/grants/{ids[0]}", expect=404)

    async def test_my_permissions(self):
        _, me = await self.call("GET", "/me/permissions", "viewer", expect=200)
        self.assertEqual((me["role"], me["mode"]), ("viewer", "global"))
        _, me = await self.call("GET", "/me/permissions", "admin", expect=200)
        self.assertEqual(me["mode"], "admin")
        # Through an API key.
        _, key = await self.call("POST", "/apikeys", "admin", json={"name": "ci", "role": "operator"}, expect=201)
        token = key["secret"]
        r = await self.client.get("/api/v1/me/permissions", headers={"X-API-Key": token})
        self.assertEqual(r.status, 200, await r.text())
        self.assertEqual((await r.json())["mode"], "global")


if __name__ == "__main__":
    unittest.main()
