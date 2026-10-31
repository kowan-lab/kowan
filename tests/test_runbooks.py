"""Runbooks end to end: the API against a server and a fake Docker engine
whose exec and throwaway containers run the step scripts with a real ``sh``.

Covers defining (UI and labels), parameters as data, the exit code, steps in
order with stop/continue, ``exec`` on one or all replicas, ``run`` in a
throwaway container, following a run live, history, confirmation, roles and
suggestions; plus the output parser on its own.
"""

import asyncio
import subprocess
import unittest

from kowan.runbooks.service import _Output, build_script
from tests.apicase import ServerCase


def _sh(script: str):
    p = subprocess.run(["sh", "-c", script], capture_output=True, timeout=20)
    return p.returncode, p.stdout + p.stderr


class TestOutput(unittest.TestCase):
    def test_marker_cut_and_exit_code_even_split_across_chunks(self):
        said = []
        script = build_script("echo hello; exit 4", {}, "__M_")
        code, raw = _sh(script)
        self.assertEqual(code, 0)  # the shell itself ends fine
        out = _Output("__M_", said.append)
        for i in range(0, len(raw), 3):  # tiny chunks: the marker is split
            out.feed(raw[i:i + 3])
        out.feed(b"", final=True)
        self.assertEqual(out.exit_code, 4)
        self.assertEqual("".join(said), "hello\n")
        self.assertNotIn("__M_", out.kept)

    def test_params_are_data(self):
        code, raw = _sh(build_script('echo "[$NAME]"', {"NAME": "x'; echo pwned; '$(id)"}, "__M_"))
        self.assertIn(b"[x'; echo pwned; '$(id)]", raw)
        self.assertNotIn(b"\npwned", raw)


class RunbookCase(ServerCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()

        def handler(cid, argv):
            if argv[:2] == ["sh", "-c"]:
                self.execs.append(self.engine.containers[cid]["Name"].lstrip("/"))
                return _sh(argv[2])
            return None

        self.execs = []
        self.engine.exec_handler = handler

        def runner(c):
            code, out = _sh(c["Config"]["Cmd"][0])
            return code, out.decode().splitlines()

        self.engine.runners.append(("app:", runner))
        labels = {"com.docker.compose.project": "shop", "com.docker.compose.service": "web"}
        self.web1 = self.engine.add_container("shop-web-1", "app:1", labels=labels)
        self.web2 = self.engine.add_container("shop-web-2", "app:1", labels=labels)
        self.engine.add_container("shop-db-1", "redis:7", labels={**labels, "com.docker.compose.service": "db"})

    async def wait_run(self, run_id, role="operator"):
        for _ in range(200):
            _, run = await self.call("GET", f"/runbook-runs/{run_id}", role, expect=200)
            if run["status"] != "running":
                return run
            await asyncio.sleep(0.05)
        self.fail("the run did not end")

    def step(self, command, **kw):
        return {"workload": "stack~local~shop", "service": "web", "command": command, **kw}


class TestRunbooks(RunbookCase):
    async def test_define_run_and_follow(self):
        _, rb = await self.call("POST", "/runbooks", "operator", json={
            "name": "Greet", "params": [{"name": "WHO", "required": True}],
            "steps": [self.step('echo "hello $WHO"; exit 3')]}, expect=201)
        self.assertEqual(rb["workloads"], ["stack~local~shop"])
        await self.call("POST", f"/runbooks/{rb['id']}/run", "operator", json={}, expect=400)  # WHO is required
        await self.call("POST", f"/runbooks/{rb['id']}/run", "viewer", json={"params": {"WHO": "x"}}, expect=403)
        _, run = await self.call("POST", f"/runbooks/{rb['id']}/run", "operator",
                                 json={"params": {"WHO": "world; rm -rf /"}}, expect=202)
        ws = await self.client.ws_connect(f"/api/v1/runbook-runs/{run['id']}/ws", headers=self.h["viewer"])
        text, done = "", None
        while done is None:
            ev = await ws.receive_json(timeout=10)
            if ev["type"] == "output":
                text += ev["text"]
            elif ev["type"] == "done":
                done = ev["run"]
        await ws.close()
        self.assertEqual(text, "hello world; rm -rf /\n")
        self.assertEqual(done["status"], "failed")
        final = await self.wait_run(run["id"])
        [rec] = final["steps"]
        self.assertEqual((rec["exitCode"], rec["container"], rec["output"]), (3, "shop-web-1", "hello world; rm -rf /\n"))
        _, runs = await self.call("GET", f"/runbooks/{rb['id']}/runs", "viewer", expect=200)
        self.assertEqual([r["id"] for r in runs], [run["id"]])
        _, listed = await self.call("GET", "/runbooks", "viewer", params={"workload": "stack~local~shop"}, expect=200)
        self.assertEqual(listed[0]["lastRun"]["status"], "failed")
        # A finished run replays from the store over the socket too.
        await asyncio.sleep(0)
        ws = await self.client.ws_connect(f"/api/v1/runbook-runs/{run['id']}/ws", headers=self.h["viewer"])
        kinds = []
        while True:
            ev = await ws.receive_json(timeout=10)
            kinds.append(ev["type"])
            if ev["type"] == "done":
                break
        self.assertIn("output", kinds)

    async def test_steps_in_order_stop_and_continue(self):
        _, rb = await self.call("POST", "/runbooks", "operator", json={"name": "Multi", "steps": [
            self.step("echo one", replicas="all"),
            self.step("exit 1", continueOnError=True, title="may fail"),
            self.step("echo three", workload="stack~local~shop", service="db"),
            self.step("exit 2"),
            self.step("echo never"),
        ]}, expect=201)
        _, run = await self.call("POST", f"/runbooks/{rb['id']}/run", "operator", expect=202)
        final = await self.wait_run(run["id"])
        self.assertEqual(final["status"], "failed")
        got = [(r["step"], r["container"], r["status"]) for r in final["steps"]]
        self.assertEqual(got, [(0, "shop-web-1", "succeeded"), (0, "shop-web-2", "succeeded"), (1, "shop-web-1", "failed"),
                               (2, "shop-db-1", "succeeded"), (3, "shop-web-1", "failed")])

    async def test_run_mode_uses_a_throwaway_container(self):
        _, rb = await self.call("POST", "/runbooks", "operator", json={
            "name": "Migrate", "confirm": True,
            "params": [{"name": "TARGET", "type": "choice", "choices": ["up", "down"], "default": "up"}],
            "steps": [self.step('echo "migrate $TARGET"', mode="run")]}, expect=201)
        await self.call("POST", f"/runbooks/{rb['id']}/run", "operator", json={}, expect=400)  # needs confirm
        await self.call("POST", f"/runbooks/{rb['id']}/run", "operator",
                        json={"confirm": True, "params": {"TARGET": "sideways"}}, expect=400)
        _, run = await self.call("POST", f"/runbooks/{rb['id']}/run", "operator", json={"confirm": True}, expect=202)
        final = await self.wait_run(run["id"])
        [rec] = final["steps"]
        self.assertEqual((final["status"], rec["exitCode"], rec["output"]), ("succeeded", 0, "migrate up\n"), rec)
        # Created with the service's setup, a run label, no restart; then removed.
        created = [c for c in self.engine.calls if c[0] == "POST" and c[1] == "/containers/create"]
        body = created[-1][3]
        self.assertEqual(body["Labels"]["kowan.runbook-run"], run["id"])
        self.assertEqual(body["Labels"]["com.docker.compose.project"], "shop")
        self.assertFalse(any("runbook" in c["Name"] for c in self.engine.containers.values()))
        self.assertEqual(self.execs, [])

    async def test_label_runbooks(self):
        labels = {"com.docker.compose.project": "blog", "com.docker.compose.service": "api",
                  "kowan.runbook.hello.command": "echo from-labels",
                  "kowan.runbook.hello.title": "Say hello",
                  "kowan.runbook.bad.mode": "nope"}
        self.engine.add_container("blog-api-1", "app:1", labels=labels)
        self.engine.add_container("blog-api-2", "app:1", labels=labels)
        _, listed = await self.call("GET", "/runbooks", "viewer", params={"workload": "stack~local~blog"}, expect=200)
        [rb] = listed
        self.assertEqual((rb["name"], rb["source"], rb["id"]), ("Say hello", "labels", "label:stack~local~blog:api:hello"))
        await self.call("PUT", f"/runbooks/{rb['id']}", "admin", json={"name": "x", "steps": [self.step("x")]}, expect=400)
        await self.call("DELETE", f"/runbooks/{rb['id']}", "admin", expect=400)
        _, run = await self.call("POST", f"/runbooks/{rb['id']}/run", "operator", expect=202)
        final = await self.wait_run(run["id"])
        self.assertEqual(final["steps"][0]["output"], "from-labels\n")

    async def test_edit_delete_validation_and_suggestions(self):
        await self.call("POST", "/runbooks", "operator", json={"name": "x", "steps": []}, expect=400)
        await self.call("POST", "/runbooks", "operator", json={"name": "x", "steps": [self.step("x", mode="nope")]}, expect=400)
        await self.call("POST", "/runbooks", "operator", json={"name": "x", "bogus": 1, "steps": [self.step("x")]}, expect=400)
        await self.call("POST", "/runbooks", "operator", json={
            "name": "x", "params": [{"name": "lower"}], "steps": [self.step("x")]}, expect=400)
        await self.call("POST", "/runbooks", "viewer", json={"name": "x", "steps": [self.step("x")]}, expect=403)
        _, rb = await self.call("POST", "/runbooks", "operator", json={"name": "x", "steps": [self.step("true")]}, expect=201)
        _, rb = await self.call("PUT", f"/runbooks/{rb['id']}", "operator",
                                json={"name": "renamed", "steps": [self.step("true")]}, expect=200)
        self.assertEqual(rb["name"], "renamed")
        await self.call("DELETE", f"/runbooks/{rb['id']}", "operator", expect=200)
        await self.call("GET", f"/runbooks/{rb['id']}", "operator", expect=404)
        _, sugg = await self.call("GET", "/runbooks/suggestions", "viewer", params={"workload": "stack~local~shop"}, expect=200)
        names = [s["name"] for s in sugg]
        self.assertIn("Redis memory (db)", names)
        self.assertIn("Disk usage", names)
        redis = next(s for s in sugg if s["name"] == "Redis memory (db)")
        self.assertEqual(redis["steps"][0]["service"], "db")

    async def test_no_container_and_already_running(self):
        _, rb = await self.call("POST", "/runbooks", "operator", json={
            "name": "Nobody", "steps": [self.step("true", service="worker")]}, expect=201)
        _, run = await self.call("POST", f"/runbooks/{rb['id']}/run", "operator", expect=202)
        final = await self.wait_run(run["id"])
        self.assertIn("no running container of service worker", final["steps"][0]["error"])
        _, slow = await self.call("POST", "/runbooks", "operator", json={"name": "Slow", "steps": [self.step("sleep 1")]},
                                  expect=201)
        _, run = await self.call("POST", f"/runbooks/{slow['id']}/run", "operator", expect=202)
        await self.call("POST", f"/runbooks/{slow['id']}/run", "operator", expect=409)
        await self.wait_run(run["id"])


    async def test_a_schedule_runs_a_runbook(self):
        _, rb = await self.call("POST", "/runbooks", "operator", json={
            "name": "Nightly", "params": [{"name": "DAYS", "type": "number", "default": "7"}],
            "steps": [self.step('echo "keep $DAYS days"')]}, expect=201)
        _, sched = await self.call("POST", "/schedules", "operator", json={
            "name": "nightly", "targetType": "runbook", "targetId": rb["id"], "action": "run",
            "cron": "0 3 * * *"}, expect=201)
        _, res = await self.call("POST", f"/schedules/{sched['id']}/run", "operator", expect=200)
        self.assertEqual(res["status"], "success", res)
        _, runs = await self.call("GET", f"/runbooks/{rb['id']}/runs", "operator", expect=200)
        self.assertEqual((runs[0]["trigger"], runs[0]["params"]), ("schedule", {"DAYS": "7"}))
        await self.call("POST", "/schedules", "operator", json={
            "name": "bad", "targetType": "runbook", "targetId": rb["id"], "action": "stop", "cron": "0 3 * * *"}, expect=400)


if __name__ == "__main__":
    unittest.main()


class TestAlertRunbooks(RunbookCase):
    async def test_an_alert_runs_its_rules_runbook(self):
        from kowan.events import Event

        _, rb = await self.call("POST", "/runbooks", "operator", json={
            "name": "Diagnose", "steps": [self.step('echo "$KOWAN_ALERT_RULE on $KOWAN_ALERT_CONTAINER: $KOWAN_ALERT_MESSAGE"')]},
            expect=201)
        _, rule = await self.call("POST", "/alert-rules", "operator", json={
            "name": "web crashed", "type": "container_exited", "severity": "critical", "enabled": True}, expect=201)
        _, link = await self.call("GET", f"/alert-rules/{rule['id']}/runbook", "viewer", expect=200)
        self.assertIsNone(link)
        await self.call("PUT", f"/alert-rules/{rule['id']}/runbook", "viewer", json={"runbookId": rb["id"]}, expect=403)
        await self.call("PUT", f"/alert-rules/{rule['id']}/runbook", "operator", json={"runbookId": "nope"}, expect=404)
        await self.call("PUT", f"/alert-rules/{rule['id']}/runbook", "operator",
                        json={"runbookId": rb["id"], "cooldownMinutes": 30}, expect=200)

        notices = self.svc.bus.subscribe("notice")
        alert = {"id": "a1", "ruleId": rule["id"], "hostId": "local", "level": "critical", "title": "web crashed",
                 "message": "container shop-web-1 exited with code 137", "containerName": "shop-web-1"}
        self.svc.bus.publish(Event(type="alert", source="monitor", payload=alert))
        for _ in range(100):
            runs = self.svc.store.list_runbook_runs(rb["id"])
            if runs and runs[0]["status"] != "running":
                break
            await asyncio.sleep(0.05)
        [run] = runs
        self.assertEqual((run["trigger"], run["user"], run["status"]), ("alert", "alert: web crashed", "succeeded"))
        self.assertEqual(run["steps"][0]["output"], "web crashed on shop-web-1: container shop-web-1 exited with code 137\n")
        notice = await asyncio.wait_for(notices.__anext__(), 5)
        self.assertIn("Runbook Diagnose succeeded", notice.payload["title"])
        notices.cancel()
        # Cooling down for the same container; another container goes.
        self.assertIsNone(await self.svc.alert_runbooks.handle(alert))
        self.assertIsNotNone(await self.svc.alert_runbooks.handle({**alert, "containerName": "shop-web-2"}))
        # Deleting the rule drops the link.
        await self.call("DELETE", f"/alert-rules/{rule['id']}", "operator", expect=200)
        self.assertIsNone(self.svc.store.get_rule_runbook(rule["id"]))

    async def test_a_runbook_needing_a_value_cannot_be_linked(self):
        _, rb = await self.call("POST", "/runbooks", "operator", json={
            "name": "Needs", "params": [{"name": "X", "required": True}], "steps": [self.step("true")]}, expect=201)
        _, rule = await self.call("POST", "/alert-rules", "operator", json={
            "name": "r", "type": "container_exited", "enabled": True}, expect=201)
        await self.call("PUT", f"/alert-rules/{rule['id']}/runbook", "operator", json={"runbookId": rb["id"]}, expect=400)


from tests.test_mcp_e2e import MCPCase  # noqa: E402


class TestRunbookTools(MCPCase, RunbookCase):
    async def test_list_and_run_through_mcp(self):
        _, rb = await self.call("POST", "/runbooks", "operator", json={
            "name": "Purge", "confirm": True, "params": [{"name": "KEY", "default": "all"}],
            "steps": [self.step('echo "purged $KEY"')]}, expect=201)
        out, _ = await self.tool("list_runbooks", "viewer", workload="stack~local~shop")
        self.assertEqual([i["name"] for i in out["items"]], ["Purge"])
        await self.tool("run_runbook", "viewer", ok=False, runbook=rb["id"], confirm=True)  # role
        await self.tool("run_runbook", "operator", ok=False, runbook=rb["id"])  # asks for confirmation
        out, _ = await self.tool("run_runbook", "operator", runbook=rb["id"], confirm=True, params={"KEY": "user:1"})
        self.assertEqual(out["status"], "succeeded", out)
        self.assertEqual(out["steps"][0]["output"], "purged user:1\n")
        _, runs = await self.call("GET", f"/runbooks/{rb['id']}/runs", "operator", expect=200)
        self.assertEqual(runs[0]["trigger"], "mcp")
