"""Team/grant scoping of records that name their target (kowan.auth.authorize.
TargetAccess): schedules, volume backup jobs and runs, auto-update policies and
history, the MCP views of them, and the notification unread counter.

The operator is in a team holding ``stack`` on the local host's ``shop``
project: they see and change only what points at ``shop``, never ``other``."""

import json
import unittest
import uuid

from kowan.models import Notification
from tests.apicase import ServerCase
from tests.test_mcp_e2e import PV

SHOP = {"com.docker.compose.project": "shop"}
OTHER = {"com.docker.compose.project": "other"}


class TargetScopingCase(ServerCase):
    level = "manage"

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.shop_ctr = self.engine.add_container("shop-web", labels=SHOP)
        self.other_ctr = self.engine.add_container("other-web", labels=OTHER)
        self.engine.add_volume("shop-data", labels=SHOP)
        self.engine.add_volume("other-data", labels=OTHER)
        _, team = await self.call("POST", "/teams", json={"name": "shop"}, expect=201)
        self.team = team["id"]
        _, users = await self.call("GET", "/users", expect=200)
        uid = next(u["id"] for u in users if u["username"] == "operator")
        await self.call("POST", f"/teams/{team['id']}/members", json={"userId": uid}, expect=201)
        await self.call("POST", "/grants", json={"subjectType": "team", "subjectId": team["id"], "scope": "stack",
                                                 "hostId": "local", "target": "shop", "level": self.level},
                        expect=201)


class TestSchedules(TargetScopingCase):
    async def schedule(self, target_type, target_id, role="admin", expect=201):
        body = {"targetType": target_type, "targetId": target_id, "action": "stop", "cron": "0 3 * * *"}
        if target_type != "deployment":
            body["hostId"] = "local"
        _, s = await self.call("POST", "/schedules", role, json=body, expect=expect)
        return s

    async def test_list_get_write_and_runs(self):
        _, dep = await self.call("POST", "/deployments", json={
            "name": "other-app", "type": "container", "strategy": "standalone", "targets": ["local"],
            "spec": {"image": "nginx:1.27"}}, expect=201)
        mine = [await self.schedule("container", "shop-web"), await self.schedule("stack", "shop")]
        theirs = [await self.schedule("container", "other-web"), await self.schedule("stack", "other"),
                  await self.schedule("deployment", dep["id"])]

        _, listed = await self.call("GET", "/schedules", "operator", expect=200)
        self.assertEqual({s["id"] for s in listed}, {s["id"] for s in mine})
        _, everything = await self.call("GET", "/schedules", "viewer", expect=200)  # unscoped: role only
        self.assertEqual(len(everything), 5)
        await self.call("GET", f"/schedules/{mine[0]['id']}", "operator", expect=200)
        for s in theirs:
            await self.call("GET", f"/schedules/{s['id']}", "operator", expect=404)
            await self.call("POST", f"/schedules/{s['id']}/run", "operator", expect=404)
            await self.call("DELETE", f"/schedules/{s['id']}", "operator", expect=404)

        # No automation outside the grants: the runner would carry it out unrestricted.
        await self.schedule("container", "other-web", "operator", expect=403)
        await self.schedule("stack", "other", "operator", expect=403)
        await self.schedule("deployment", dep["id"], "operator", expect=403)
        await self.schedule("container", "gone", "operator", expect=403)  # unknown: fail closed
        await self.call("PUT", f"/schedules/{mine[0]['id']}", "operator", json={"targetId": "other-web"}, expect=403)
        await self.schedule("container", "shop-web", "operator")

        await self.call("POST", f"/schedules/{mine[0]['id']}/run", "operator", expect=200)
        await self.call("POST", f"/schedules/{theirs[0]['id']}/run", expect=200)
        _, runs = await self.call("GET", "/schedules/runs", "operator", expect=200)
        self.assertEqual([r["targetId"] for r in runs], ["shop-web"])
        _, runs = await self.call("GET", "/schedules/runs", expect=200)
        self.assertEqual(len(runs), 2)


class TestVolumeBackups(TargetScopingCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        _, repo = await self.call("POST", "/volbackups/repos", json={
            "name": "minio", "type": "s3", "config": {"endpoint": "http://m:9000", "bucket": "b"},
            "secrets": {"password": "pw", "accessKeyId": "ak", "secretAccessKey": "sk"}}, expect=201)
        self.repo = repo["id"]

    async def job(self, name, selector, role="admin", expect=201):
        _, j = await self.call("POST", "/volbackups/jobs", role, json={
            "name": name, "hostId": "local", "repoId": self.repo, "selector": selector}, expect=expect)
        return j

    async def test_jobs_and_runs(self):
        mine = [await self.job("a", {"mode": "names", "volumes": ["shop-data"]}),
                await self.job("b", {"mode": "stack", "stack": "shop"})]
        theirs = [await self.job("c", {"mode": "names", "volumes": ["shop-data", "other-data"]}),
                  await self.job("d", {"mode": "stack", "stack": "other"}),
                  await self.job("e", {"mode": "label"})]

        _, listed = await self.call("GET", "/volbackups/jobs", "operator", expect=200)
        self.assertEqual({j["id"] for j in listed}, {j["id"] for j in mine})
        for j in theirs:
            await self.call("GET", f"/volbackups/jobs/{j['id']}", "operator", expect=404)
            await self.call("POST", f"/volbackups/jobs/{j['id']}/run", "operator", expect=404)
            await self.call("DELETE", f"/volbackups/jobs/{j['id']}", "operator", expect=404)
        await self.job("f", {"mode": "names", "volumes": ["other-data"]}, "operator", expect=403)
        await self.job("g", {"mode": "label"}, "operator", expect=403)
        await self.call("PUT", f"/volbackups/jobs/{mine[1]['id']}", "operator",
                        json={"selector": {"mode": "stack", "stack": "other"}}, expect=403)
        await self.job("h", {"mode": "names", "volumes": ["shop-data"]}, "operator")
        await self.call("POST", "/volbackups/volumes/local/other-data/backup", "operator",
                        json={"repoId": self.repo}, expect=403)
        await self.call("POST", "/volbackups/restore", "operator", json={
            "hostId": "local", "repoId": self.repo, "snapshotId": "abcdef12", "target": "other-data"}, expect=403)

        store = self.svc.store
        shop_run = store.save_backup_run({"jobId": mine[0]["id"], "hostId": "local", "status": "success",
                                          "volumes": ["shop-data"]})
        other_run = store.save_backup_run({"jobId": theirs[0]["id"], "hostId": "local", "status": "success",
                                           "volumes": ["shop-data", "other-data"]})
        _, runs = await self.call("GET", "/volbackups/runs", "operator", expect=200)
        self.assertEqual([r["id"] for r in runs], [shop_run["id"]])
        await self.call("GET", f"/volbackups/runs/{other_run['id']}", "operator", expect=404)
        await self.call("GET", f"/volbackups/runs/{shop_run['id']}", "operator", expect=200)


class TestAutoUpdate(TargetScopingCase):
    level = "operate"

    async def test_overview_history_and_writes(self):
        _, dep = await self.call("POST", "/deployments", json={
            "name": "other-app", "type": "container", "strategy": "standalone", "targets": ["local"],
            "spec": {"image": "nginx:1.27"}}, expect=201)
        for cid in (self.shop_ctr, self.other_ctr):
            await self.call("PUT", f"/hosts/local/containers/{cid}/autoupdate", json={"mode": "notify"}, expect=200)
        await self.call("PUT", f"/deployments/{dep['id']}/autoupdate", json={"mode": "notify"}, expect=200)
        _, all_objs = await self.call("GET", "/autoupdate/overview", expect=200)
        self.assertEqual(len(all_objs["objects"]), 3)
        _, ov = await self.call("GET", "/autoupdate/overview", "operator", expect=200)
        self.assertEqual([o["target"] for o in ov["objects"]], ["shop-web"])

        for target in ("shop-web", "other-web", "shop-web"):
            self.svc.store.add_autoupdate_history({
                "id": str(uuid.uuid4()), "kind": "container", "hostId": "local", "target": target,
                "outcome": "updated", "startedAt": "2026-10-30T00:00:00Z"})
        _, hist = await self.call("GET", "/autoupdate/history", "operator", params={"limit": "1"}, expect=200)
        self.assertEqual((hist["total"], [h["target"] for h in hist["items"]]), (2, ["shop-web"]))
        _, hist = await self.call("GET", "/autoupdate/history", expect=200)
        self.assertEqual(hist["total"], 3)

        # Outside the grants: no read, no write. Inside, operate is not enough to let
        # the updater recreate a lone container (manage), but it is for a redeploy.
        await self.call("GET", f"/deployments/{dep['id']}/autoupdate", "operator", expect=403)
        await self.call("PUT", f"/deployments/{dep['id']}/autoupdate", "operator", json={"mode": "off"}, expect=403)
        await self.call("DELETE", f"/deployments/{dep['id']}/autoupdate", "operator", expect=403)
        await self.call("PUT", f"/hosts/local/containers/{self.other_ctr}/autoupdate", "operator",
                        json={"mode": "off"}, expect=403)
        await self.call("PUT", f"/hosts/local/containers/{self.shop_ctr}/autoupdate", "operator",
                        json={"mode": "off"}, expect=403)
        await self.call("POST", "/autoupdate/check", "operator",
                        json={"kind": "deployment", "target": dep["id"]}, expect=403)


class TestMCPAndNotifications(TargetScopingCase):
    async def mcp(self, key, tool, **args):
        h = {"Authorization": "Bearer " + key, "Content-Type": "application/json",
             "Accept": "application/json, text/event-stream"}
        r = await self.client.post("/mcp", headers=h, data=json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": PV, "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}))
        h.update({"Mcp-Session-Id": r.headers["Mcp-Session-Id"], "MCP-Protocol-Version": PV})
        await self.client.post("/mcp", headers=h, data=json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        r = await self.client.post("/mcp", headers=h, data=json.dumps({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool, "arguments": args}}))
        return (await r.json())["result"]

    async def test_mcp_lists_and_unread_counter(self):
        ids = {}
        for target in ("shop-web", "other-web"):
            _, s = await self.call("POST", "/schedules", json={
                "targetType": "container", "targetId": target, "hostId": "local", "action": "stop",
                "cron": "0 3 * * *"}, expect=201)
            ids[target] = s["id"]
        # A key bound to the team sees what the team's grants cover, through MCP too.
        _, key = await self.call("POST", "/apikeys", json={"name": "bot", "role": "operator", "teamId": self.team},
                                 expect=201)
        out = await self.mcp(key["secret"], "list_schedules")
        self.assertEqual([s["targetId"] for s in out["structuredContent"]["items"]], ["shop-web"])
        out = await self.mcp(key["secret"], "run_schedule", schedule=ids["other-web"])
        self.assertTrue(out.get("isError"))
        out = await self.mcp(key["secret"], "list_backups")
        self.assertEqual(out["structuredContent"]["jobs"], [])

        # Unread counter: only what the (filtered) list shows.
        for i, host in enumerate(("local", "elsewhere", "")):
            self.svc.store.create_notification(Notification(
                id=f"n{i}", kind="notice", level="info", title=f"t{i}", hostId=host,
                createdAt=f"2026-10-30T00:00:0{i}Z"))
        _, listed = await self.call("GET", "/notifications", "operator", expect=200)
        _, count = await self.call("GET", "/notifications/unread-count", "operator", expect=200)
        self.assertEqual(([n["hostId"] for n in listed], count["count"]), (["local"], 1))
        _, count = await self.call("GET", "/notifications/unread-count", "viewer", expect=200)
        self.assertEqual(count["count"], 3)


if __name__ == "__main__":
    unittest.main()
