"""A team-scoped user against the real API + fake Engine: a label grant lets
them see and act on matching containers, volumes and networks only; host-wide
operations are refused; a stack grant covers its compose project."""

import unittest

from tests.apicase import ServerCase

H = "/hosts/local"


class TestScopedUser(ServerCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        _, users = await self.call("GET", "/users", "admin")
        uid = next(u["id"] for u in users if u["username"] == "operator")
        _, team = await self.call("POST", "/teams", "admin", json={"name": "web"}, expect=201)
        await self.call("POST", f"/teams/{team['id']}/members", "admin", json={"userId": uid}, expect=201)
        self.team = team
        await self.call("POST", "/grants", "admin", json={
            "subjectType": "team", "subjectId": team["id"], "scope": "labels", "target": "team=web",
            "level": "operate"}, expect=201)
        # Re-login: the principal is loaded per request, the token stays valid.
        self.engine.add_image("alpine:3.22")
        self.mine = self.engine.add_container("mine", labels={"team": "web"})
        self.theirs = self.engine.add_container("theirs", labels={"team": "db"})
        self.engine.add_volume("web-data", labels={"team": "web"})
        self.engine.add_volume("db-data", labels={"team": "db"})
        self.engine.add_network("web-net", labels={"team": "web"})
        self.engine.add_network("db-net", labels={"team": "db"})

    async def test_label_scope(self):
        _, items = await self.call("GET", H + "/containers", "operator", params={"all": "1"}, expect=200)
        self.assertEqual([c["names"] for c in items], [["/mine"]])
        await self.call("POST", f"{H}/containers/{self.mine}/restart", "operator", expect=200)
        await self.call("POST", f"{H}/containers/{self.theirs}/restart", "operator", expect=403)
        _, vols = await self.call("GET", H + "/volumes", "operator", expect=200)
        self.assertEqual([v["name"] for v in vols], ["web-data"])
        await self.call("DELETE", H + "/volumes/db-data", "operator", expect=403)
        # Removing needs "manage": operate is not enough, even in scope.
        await self.call("DELETE", H + "/volumes/web-data", "operator", expect=403)
        await self.call("POST", "/grants", "admin", json={
            "subjectType": "team", "subjectId": self.team["id"], "scope": "labels", "target": "team=web",
            "level": "manage"}, expect=201)
        await self.call("DELETE", H + "/volumes/web-data", "operator", expect=200)
        await self.call("DELETE", H + "/volumes/db-data", "operator", expect=403)
        _, nets = await self.call("GET", H + "/networks", "operator", expect=200)
        self.assertEqual([n["name"] for n in nets], ["web-net"])
        db_net = self.engine.network("db-net")["Id"]
        await self.call("DELETE", f"{H}/networks/{db_net}", "operator", expect=403)
        # Creating outside the scope is refused, inside is fine.
        await self.call("POST", H + "/volumes", "operator", json={"name": "x", "labels": {"team": "db"}}, expect=403)
        await self.call("POST", H + "/volumes", "operator", json={"name": "y", "labels": {"team": "web"}},
                        expect=201)
        # Host-wide operations need a host grant.
        await self.call("POST", H + "/prune/images", "operator", expect=403)
        _, me = await self.call("GET", "/me/permissions", "operator", expect=200)
        self.assertEqual(me["mode"], "scoped")

    async def test_stack_scope(self):
        await self.call("POST", "/grants", "admin", json={
            "subjectType": "team", "subjectId": self.team["id"], "scope": "stack", "hostId": "local",
            "target": "shop", "level": "operate"}, expect=201)
        shop = self.engine.add_container("shop-web-1", labels={"com.docker.compose.project": "shop"})
        await self.call("POST", f"{H}/containers/{shop}/restart", "operator", expect=200)


if __name__ == "__main__":
    unittest.main()
