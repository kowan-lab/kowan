"""Host groups: tags on hosts, and what targets a group (grants, auto-update
policies, schedules, runbook steps)."""

import asyncio
import subprocess

from tests.apicase import ServerCase


class TestHostGroups(ServerCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.web = self.engine.add_container("web", "app:1", labels={"com.docker.compose.project": "shop",
                                                                      "com.docker.compose.service": "web"})
        self.agent = self.engine.add_container("kowan-agent", "agent:1", labels={"org.opencontainers.image.title": "kowan-agent"})
        self.quiet = self.engine.add_container("keep", "app:1", labels={"kowan.schedule.group": "off"})
        self.job = self.engine.add_container("job", "app:1", state="exited")

    async def tag(self, *tags, expect=200):
        return await self.call("PATCH", "/hosts/local", "admin", json={"tags": list(tags)}, expect=expect)

    async def test_tags(self):
        _, h = await self.tag("Production", "customer-x", " production ")
        self.assertEqual(h["tags"], ["customer-x", "production"])
        await self.tag("no spaces", expect=400)
        await self.call("PATCH", "/hosts/local", "operator", json={"tags": ["x"]}, expect=403)
        _, hosts = await self.call("GET", "/hosts", "viewer", expect=200)
        self.assertEqual(hosts[0]["tags"], ["customer-x", "production"])
        self.assertEqual(self.svc.store.hosts_with_tag("production"), ["local"])

    async def test_a_group_grant_covers_every_host_in_it(self):
        _, t = await self.call("POST", "/teams", json={"name": "ops"}, expect=201)
        _, key = await self.call("POST", "/apikeys", json={"name": "k", "role": "operator", "teamId": t["id"]}, expect=201)
        hdr = {"X-API-Key": key["secret"]}
        await self.call("POST", "/grants", json={"subjectType": "team", "subjectId": t["id"], "scope": "host_tag",
                                                 "target": "Bad Tag", "level": "operate"}, expect=400)
        await self.call("POST", "/grants", json={"subjectType": "team", "subjectId": t["id"], "scope": "host_tag",
                                                 "target": "production", "level": "operate"}, expect=201)
        r = await self.client.get("/api/v1/hosts/local/containers?all=1", headers=hdr)
        self.assertEqual(r.status, 403)  # not in the group yet
        await self.tag("production")
        r = await self.client.get("/api/v1/hosts/local/containers?all=1", headers=hdr)
        self.assertEqual(r.status, 200)
        self.assertGreater(len(await r.json()), 0)

    async def test_group_autoupdate_policy(self):
        await self.tag("production")
        _, groups = await self.call("GET", "/autoupdate/groups", "viewer", expect=200)
        self.assertEqual([(g["tag"], g["stored"]) for g in groups], [("production", None)])
        await self.call("PUT", "/autoupdate/groups/production", "operator", json={"mode": "notify"}, expect=200)
        _, groups = await self.call("GET", "/autoupdate/groups", "viewer", expect=200)
        self.assertEqual(groups[0]["effective"]["mode"], "notify")
        res = await self.svc.autoupdater.process_host(self.svc.store.get_host("local"), check_registry=False)
        targets = {r.get("target") for r in res}
        self.assertIn("web", targets)
        self.assertNotIn("kowan-agent", targets)  # Kowan's own never follow a group policy
        _, overview = await self.call("GET", "/autoupdate/overview", "viewer", expect=200)
        self.assertFalse(any(o.get("kind") == "group" for o in overview["objects"]))
        await self.call("DELETE", "/autoupdate/groups/production", "operator", expect=200)
        self.assertEqual(await self.svc.autoupdater.process_host(self.svc.store.get_host("local"), check_registry=False), [])

    async def test_group_schedule(self):
        await self.tag("staging")
        _, s = await self.call("POST", "/schedules", "operator", json={
            "name": "night", "targetType": "group", "targetId": "staging", "action": "stop", "cron": "0 20 * * *"}, expect=201)
        _, run = await self.call("POST", f"/schedules/{s['id']}/run", "operator", expect=200)
        self.assertEqual(run["status"], "success", run)
        states = {c["Name"].lstrip("/"): c["State"]["Running"] for c in self.engine.containers.values()}
        self.assertEqual(states, {"web": False, "kowan-agent": True, "keep": True, "job": False})
        _, s2 = await self.call("POST", "/schedules", "operator", json={
            "name": "morning", "targetType": "group", "targetId": "staging", "action": "start", "cron": "0 8 * * *"}, expect=201)
        await self.call("POST", f"/schedules/{s2['id']}/run", "operator", expect=200)
        states = {c["Name"].lstrip("/"): c["State"]["Running"] for c in self.engine.containers.values()}
        # Only what is meant to run comes back (the fake's default policy is
        # unless-stopped; the exited job was created with it too, so it starts).
        self.assertTrue(states["web"])

    async def test_runbook_on_a_group(self):
        def handler(cid, argv):
            if argv[:2] == ["sh", "-c"]:
                p = subprocess.run(argv, capture_output=True, timeout=20)
                return p.returncode, p.stdout
            return None

        self.engine.exec_handler = handler
        await self.tag("edge")
        await self.call("POST", "/runbooks", "operator", json={
            "name": "x", "steps": [{"workload": "group~edge", "command": "true"}]}, expect=400)  # needs a service
        _, rb = await self.call("POST", "/runbooks", "operator", json={
            "name": "Purge", "steps": [{"workload": "group~edge", "service": "web", "command": "echo purged"}]}, expect=201)
        _, listed = await self.call("GET", "/runbooks", "viewer", params={"workload": "group~edge"}, expect=200)
        self.assertEqual([r["id"] for r in listed], [rb["id"]])
        _, run = await self.call("POST", f"/runbooks/{rb['id']}/run", "operator", expect=202)
        for _ in range(100):
            _, run = await self.call("GET", f"/runbook-runs/{run['id']}", "operator", expect=200)
            if run["status"] != "running":
                break
            await asyncio.sleep(0.05)
        self.assertEqual((run["status"], run["steps"][0]["container"], run["steps"][0]["output"]), ("succeeded", "web", "purged\n"))
