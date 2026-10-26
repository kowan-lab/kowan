"""Hosts REST: create validation, join commands (edge/agent, external URL and
X-Forwarded-Proto), renaming (the local host too), the swarm view, liveness
when the daemon fails, and the protected local host."""

import unittest
from unittest import mock

from tests.apicase import ServerCase


class TestHostsApi(ServerCase):
    async def test_create_rules_and_join_commands(self):
        for bad in ([1], {}, {"name": "x", "mode": "ssh"}, {"name": "x", "mode": "agent"}, {"name": "x", "token": "t"}):
            await self.call("POST", "/hosts", "admin", json=bad, expect=400)
        await self.roles("POST", "/hosts", "admin", json={"name": "x", "mode": "edge"})
        _, created = await self.call("POST", "/hosts", "admin", json={"name": "edge1", "mode": "edge"}, expect=201)
        hid = created["host"]["id"]
        _, cmd = await self.call("GET", f"/hosts/{hid}/edge-command", "admin", expect=200)
        self.assertIn("KOWAN_SERVER_URL=http://", cmd["dockerRun"])
        r = await self.client.get(f"/api/v1/hosts/{hid}/edge-command", headers={
            **self.h["admin"], "X-Forwarded-Proto": "https"})
        self.assertIn("KOWAN_SERVER_URL=https://", (await r.json())["dockerRun"])
        self.svc.config.external_url = "https://kowan.example.com/"
        _, cmd = await self.call("GET", f"/hosts/{hid}/edge-command", "admin", expect=200)
        self.assertIn("KOWAN_SERVER_URL=https://kowan.example.com ", cmd["dockerRun"])
        with mock.patch.dict("os.environ", {"KOWAN_AGENT_IMAGE": "registry.example.com/agent:1"}):
            _, cmd = await self.call("GET", f"/hosts/{hid}/edge-command", "admin", expect=200)
        self.assertTrue(cmd["dockerRun"].endswith("registry.example.com/agent:1"))
        await self.call("GET", "/hosts/local/edge-command", "admin", expect=400)
        await self.call("DELETE", "/hosts/local", "admin", expect=400)
        await self.call("DELETE", "/hosts/missing", "admin", expect=404)

    async def test_rename(self):
        await self.roles("PATCH", "/hosts/local", "admin", json={"name": "nas"})
        _, h = await self.call("PATCH", "/hosts/local", "admin", json={"name": "  home-server  "}, expect=200)
        self.assertEqual((h["id"], h["name"], h["type"]), ("local", "home-server", "local"))
        self.assertTrue(h["online"])
        _, hosts = await self.call("GET", "/hosts", "viewer", expect=200)
        self.assertEqual([x["name"] for x in hosts if x["id"] == "local"], ["home-server"])
        for bad in ([1], {}, {"name": ""}, {"name": "   "}, {"name": 5}, {"name": "x" * 65},
                    {"name": "a\tb"}, {"name": "x", "id": "other"}):
            await self.call("PATCH", "/hosts/local", "admin", json=bad, expect=400)
        await self.call("PATCH", "/hosts/missing", "admin", json={"name": "x"}, expect=404)

        _, created = await self.call("POST", "/hosts", "admin", json={"name": "edge1", "mode": "edge"}, expect=201)
        hid = created["host"]["id"]
        # A name may not repeat another host's name or id: GitOps files refer
        # to hosts by either.
        await self.call("PATCH", f"/hosts/{hid}", "admin", json={"name": "Home-Server"}, expect=409)
        await self.call("PATCH", f"/hosts/{hid}", "admin", json={"name": "LOCAL"}, expect=409)
        await self.call("POST", "/hosts", "admin", json={"name": "edge1", "mode": "edge"}, expect=409)
        await self.call("PATCH", "/hosts/local", "admin", json={"name": hid}, expect=409)
        # Keeping its own name (in another case) is fine.
        _, h = await self.call("PATCH", f"/hosts/{hid}", "admin", json={"name": "EDGE1"}, expect=200)
        self.assertEqual(h["name"], "EDGE1")
        _, h = await self.call("PATCH", "/hosts/local", "admin", json={"name": "local"}, expect=200)
        self.assertEqual(h["name"], "local")

    async def test_swarm_and_liveness(self):
        _, sw = await self.call("GET", "/hosts/local/swarm", "viewer", expect=200)
        self.assertEqual(sw, {"active": False, "nodes": []})
        self.engine.swarm = True
        _, sw = await self.call("GET", "/hosts/local/swarm", "viewer", expect=200)
        self.assertTrue(sw["active"])
        self.assertTrue(sw["nodes"])
        self.engine.fail[("GET", r"/_ping")] = (500, "daemon broken")
        _, h = await self.call("GET", "/hosts/local", "viewer", expect=200)
        self.assertFalse(h["online"])
        _, hosts = await self.call("GET", "/hosts", "viewer", expect=200)
        self.assertFalse(hosts[0]["online"])
        await self.call("GET", "/hosts/missing/swarm", "viewer", expect=404)


if __name__ == "__main__":
    unittest.main()
