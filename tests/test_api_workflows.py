"""Workflows end to end: REST CRUD, a manual run of every step type against the
fake Engine / fake receiver, failure handling (stop vs continueOnError), the
public run webhook with its payload variables, protection and masking."""

import asyncio
import json
import os
import unittest

from tests.apicase import ServerCase
from tests.fakeservices import FakeReceiver


class WorkflowCase(ServerCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.receiver = FakeReceiver()
        self.url = await self.receiver.start()
        self.engine.add_image("alpine:3.22")
        self.engine.add_image("nginx:1.27")

    async def asyncTearDown(self):
        await self.receiver.stop()
        await super().asyncTearDown()

    async def create(self, steps, role="admin", **extra):
        body = {"name": extra.pop("name", "wf"), "steps": steps, "enabled": True, **extra}
        status, wf = await self.call("POST", "/workflows", role, json=body)
        self.assertEqual(status, 201, wf)
        return wf

    async def run_and_wait(self, wf_id):
        _, started = await self.call("POST", f"/workflows/{wf_id}/run", "admin", expect=202)
        return await self.wait_run(wf_id, started["runId"])

    async def wait_run(self, wf_id, run_id):
        for _ in range(400):
            _, run = await self.call("GET", f"/workflows/{wf_id}/runs/{run_id}", "operator", expect=200)
            if run["status"] != "running":
                return run
            await asyncio.sleep(0.025)
        self.fail("run did not finish")


class TestEveryStep(WorkflowCase):
    async def test_all_step_types(self):
        _, ch = await self.call("POST", "/credentials", "admin", json={
            "kind": "channel", "name": "ops", "type": "webhook", "config": {"url": self.url + "/notify"}}, expect=201)
        _, dep = await self.call("POST", "/deployments", "operator", json={
            "name": "wfdep", "type": "container", "strategy": "standalone", "targets": ["local"],
            "spec": {"image": "nginx:1.27"}})
        cid = self.engine.add_container("svc")
        self.engine.runners.append(("alpine", lambda c: (0, ["built ok", "KOWAN_OUTPUT=v42"])))
        steps = [
            {"name": "script", "type": "script", "config": {"script": "echo hello $GREETING\necho \"NEXT=from-script\" >> $KOWAN_OUTPUT"}},
            {"name": "http", "type": "http", "config": {"method": "POST", "url": self.url + "/hook?n=$NEXT",
                                                        "body": "{\"g\": \"$GREETING\"}",
                                                        "headers": "X-Test: yes", "expectStatus": "2xx"}},
            {"name": "restart", "type": "container", "config": {"host": "local", "container": cid, "action": "restart"}},
            {"name": "job", "type": "run-container", "config": {"host": "local", "image": "alpine:3.22",
                                                                "command": "sh -c build", "remove": "true"}},
            {"name": "notify", "type": "notify", "config": {"channel": ch["id"], "title": "done", "body": "ok",
                                                            "level": "info"}},
            {"name": "backup", "type": "backup", "config": {}},
            {"name": "deploy", "type": "deploy", "config": {"deployment": dep["id"], "action": "deploy"}},
            {"name": "prune", "type": "prune", "config": {"host": "local", "target": "images"}},
        ]
        wf = await self.create(steps, variables={"GREETING": "world"})
        run = await self.run_and_wait(wf["id"])
        results = {s["name"]: s for s in run["steps"]}
        self.assertEqual(run["status"], "success", json.dumps(run["steps"], indent=1)[:3000])
        self.assertIn("hello world", results["script"]["output"])
        hook = next(r for r in self.receiver.received if r["path"].startswith("/hook"))
        self.assertEqual(hook["body"], {"g": "world"})
        self.assertIn("built ok", results["job"]["output"])
        self.assertTrue(any(r["path"] == "/notify" for r in self.receiver.received))
        self.assertIn("wrote backup", results["backup"]["output"])
        backups = os.listdir(os.path.join(self.data_dir, "backups"))
        self.assertEqual(len(backups), 1)
        _, runs = await self.call("GET", f"/workflows/{wf['id']}/runs", "operator", expect=200)
        self.assertEqual(len(runs), 1)

    async def test_failure_stops_unless_continue_on_error(self):
        steps = [
            {"name": "bad", "type": "http", "config": {"url": self.url + "/x", "expectStatus": "418"},
             "continueOnError": True},
            {"name": "worse", "type": "container", "config": {"host": "local", "container": "missing",
                                                              "action": "restart"}},
            {"name": "never", "type": "backup", "config": {}},
        ]
        wf = await self.create(steps)
        run = await self.run_and_wait(wf["id"])
        self.assertEqual(run["status"], "failed")
        self.assertEqual([s["status"] for s in run["steps"]], ["failed", "failed", "skipped"])


class TestStepErrors(WorkflowCase):
    async def test_every_step_reports_its_failure(self):
        steps = [
            {"name": "empty-script", "type": "script", "config": {"script": ""}},
            {"name": "failing-script", "type": "script", "config": {"script": "echo oops >&2; exit 3"}},
            {"name": "no-url", "type": "http", "config": {}},
            {"name": "unreachable", "type": "http", "config": {"url": "http://127.0.0.1:9/x"}},
            {"name": "bad-conn", "type": "http", "config": {"url": self.url, "connectionRef": "missing"}},
            {"name": "no-deployment", "type": "deploy", "config": {"deployment": "ghost"}},
            {"name": "container-args", "type": "container", "config": {"host": "local"}},
            {"name": "bad-action", "type": "container", "config": {"host": "local", "container": "x",
                                                                   "action": "explode"}},
            {"name": "bad-host", "type": "prune", "config": {"host": "mars", "target": "images"}},
            {"name": "bad-cmd", "type": "run-container", "config": {"host": "local", "image": "alpine:3.22",
                                                                     "command": "echo 'unterminated"}},
            {"name": "no-channel", "type": "notify", "config": {}},
            {"name": "bad-channel", "type": "notify", "config": {"channel": "missing"}},
        ]
        wf = await self.create([{**st, "continueOnError": True} for st in steps])
        run = await self.run_and_wait(wf["id"])
        by = {s["name"]: s for s in run["steps"]}
        self.assertEqual(run["status"], "failed")
        for name in by:
            self.assertEqual(by[name]["status"], "failed", (name, by[name]))
        self.assertIn("oops", by["failing-script"]["error"])
        self.assertIn("ghost", by["no-deployment"]["error"])

    async def test_backup_step_is_admin_only_and_stays_in_backups(self):
        steps = [{"name": "bk", "type": "backup", "config": {}}]
        for path in ("/etc/cron.d", "../outside", "nightly/../../x"):
            _, body = await self.call("POST", "/workflows", json={
                "name": "bad", "steps": [{"name": "bk", "type": "backup", "config": {"path": path}}]}, expect=400)
            self.assertIn("<data>/backups", body["error"])
        # Operators can neither create, change, run nor expose one.
        _, body = await self.call("POST", "/workflows", "operator", json={"name": "op", "steps": steps}, expect=403)
        self.assertIn("backup", body["error"])
        wf = await self.create(steps)
        await self.call("POST", f"/workflows/{wf['id']}/run", "operator", expect=403)
        await self.call("POST", f"/workflows/{wf['id']}/webhook", "operator", expect=403)
        await self.call("PUT", f"/workflows/{wf['id']}", "operator", json={"name": "wf", "steps": []}, expect=403)
        # A symlink inside backups/ cannot lead the archive out of it.
        outside = os.path.join(self.data_dir, "elsewhere")
        os.makedirs(os.path.join(self.data_dir, "backups"))
        os.makedirs(outside)
        os.symlink(outside, os.path.join(self.data_dir, "backups", "link"))
        await self.call("PUT", f"/workflows/{wf['id']}", json={
            "name": "wf", "enabled": True, "steps": [{"name": "bk", "type": "backup", "config": {"path": "link"}}]},
            expect=200)
        run = await self.run_and_wait(wf["id"])
        self.assertEqual(run["status"], "failed")
        self.assertIn("inside <data>/backups", run["steps"][0]["error"])
        self.assertEqual(os.listdir(outside), [])

    async def test_container_actions_and_relative_backup_path(self):
        cid = self.engine.add_container("svc")
        wf = await self.create([
            {"name": "stop", "type": "container", "config": {"host": "local", "container": cid, "action": "stop"}},
            {"name": "start", "type": "container", "config": {"host": "local", "container": cid, "action": "start"}},
            {"name": "bk", "type": "backup", "config": {"path": "nightly"}},
            {"name": "hdr", "type": "http", "config": {"url": self.url + "/q?m={{method}}&h={{header.X-Trace}}",
                                                       "method": "put", "headers": "junk-without-colon\nX-A: 1"}},
        ])
        _, hook = await self.call("POST", f"/workflows/{wf['id']}/webhook", "admin", expect=200)
        r = await self.client.post("/hooks/workflow/" + hook["token"] + "?k=v", data=b"x",
                                   headers={"X-Trace": "abc"})
        run = await self.wait_run(wf["id"], (await r.json())["runId"])
        self.assertEqual(run["status"], "success", run["steps"])
        self.assertTrue(os.listdir(os.path.join(self.data_dir, "backups", "nightly")))
        sent = [x for x in self.receiver.received if x["path"] == "/q"]
        self.assertTrue(sent)


class TestWebhookAndCrud(WorkflowCase):
    async def test_public_webhook_passes_the_payload(self):
        wf = await self.create([{"name": "echo", "type": "script",
                                 "config": {"script": "echo \"$KOWAN_WEBHOOK_METHOD $KOWAN_WEBHOOK_BODY\""}}])
        _, hook = await self.call("POST", f"/workflows/{wf['id']}/webhook", "admin", expect=200)
        r = await self.client.post("/hooks/workflow/" + hook["token"], data=b"ping!")
        self.assertEqual(r.status, 202)
        run = await self.wait_run(wf["id"], (await r.json())["runId"])
        self.assertIn("POST ping!", run["steps"][0]["output"])
        r = await self.client.get("/hooks/workflow/" + hook["token"])
        self.assertEqual(r.status, 405)
        await self.call("DELETE", f"/workflows/{wf['id']}/webhook", "admin", expect=200)
        r = await self.client.post("/hooks/workflow/" + hook["token"])
        self.assertEqual(r.status, 404)

    async def test_crud_roles_protection_and_masking(self):
        steps = [{"name": "h", "type": "http", "config": {"url": self.url, "headers": "Authorization: Bearer s3cr3t"}}]
        await self.roles("POST", "/workflows", "operator", json={"name": "x", "steps": steps})
        wf = await self.create(steps, role="operator", variables={"TOKEN": "abc"})
        _, seen = await self.call("GET", f"/workflows/{wf['id']}", "viewer", expect=200)
        self.assertNotIn("s3cr3t", json.dumps(seen))
        self.assertNotIn("abc", json.dumps(seen["variables"]))
        _, listing = await self.call("GET", "/workflows", "viewer", expect=200)
        self.assertEqual(len(listing), 1)
        _, types = await self.call("GET", "/workflow-trigger-types", "viewer", expect=200)
        self.assertTrue(types)
        # Operators may not add script steps (they run bash on the server).
        status, _ = await self.call("PUT", f"/workflows/{wf['id']}", "operator", json={
            "name": "x", "steps": [{"name": "s", "type": "script", "config": {"script": "id"}}]})
        self.assertEqual(status, 403)
        _, upd = await self.call("PUT", f"/workflows/{wf['id']}", "operator", json={
            "name": "renamed", "steps": steps, "enabled": False}, expect=200)
        self.assertEqual(upd["name"], "renamed")
        await self.call("PUT", f"/workflows/{wf['id']}/protection", "operator", json={"protected": True}, expect=200)
        await self.call("DELETE", f"/workflows/{wf['id']}", "operator", expect=409)
        await self.call("PUT", f"/workflows/{wf['id']}/protection", "operator", json={"protected": False}, expect=200)
        await self.call("DELETE", f"/workflows/{wf['id']}", "operator", expect=200)
        await self.call("GET", f"/workflows/{wf['id']}", "viewer", expect=404)
        for bad in ({"name": "", "steps": steps}, {"name": "n", "steps": [{"name": "s", "type": "teleport"}]}):
            status, _ = await self.call("POST", "/workflows", "admin", json=bad)
            self.assertEqual(status, 400, bad)


if __name__ == "__main__":
    unittest.main()
