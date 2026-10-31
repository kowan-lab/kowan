"""Crash snapshots: a container that dies with an error and was not stopped
on purpose is captured (logs, config, environment with secrets hidden);
repeats count on the last snapshot; the API shows it to its workload's
readers."""

import json
import unittest

from kowan.crashes import redact_env
from tests.apicase import ServerCase


def _ev(action, cid, **attrs):
    return json.dumps({"Type": "container", "Action": action, "Actor": {"ID": cid, "Attributes": {"name": "x", **attrs}},
                       "time": 1, "timeNano": 1}).encode()


class TestRedact(unittest.TestCase):
    def test_secret_names_are_hidden(self):
        env = redact_env(["DB_PASSWORD=hunter2", "API_TOKEN=abc", "MODE=prod", "EMPTY_SECRET=", "PATH=/bin"])
        self.assertEqual([e["value"] for e in env], ["••••••", "••••••", "prod", "", "/bin"])


class TestCrashes(ServerCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.ops = self.svc.docker.ops(self.svc.store.get_host("local"))
        labels = {"com.docker.compose.project": "shop", "com.docker.compose.service": "web"}
        self.cid = self.engine.add_container("shop-web-1", "app:1", labels=labels, state="exited", exit_code=1,
                                             env=["DB_PASSWORD=hunter2", "MODE=prod"],
                                             logs=["starting", "boom: cannot connect to db"])

    async def test_a_crash_is_captured_once_then_counted(self):
        w = self.svc.crashes
        snap = await w.on_event("local", self.ops, _ev("die", self.cid, exitCode="1"))
        self.assertIsNotNone(snap)
        self.assertEqual((snap["containerName"], snap["workload"], snap["exitCode"]), ("shop-web-1", "stack~local~shop", 1))
        data = snap["data"]
        self.assertIn("boom: cannot connect to db", data["logs"])
        self.assertIn({"name": "DB_PASSWORD", "value": "••••••"}, data["env"])
        self.assertIn({"name": "MODE", "value": "prod"}, data["env"])
        # The same container crashing again soon counts on that snapshot.
        self.assertIsNone(await w.on_event("local", self.ops, _ev("die", self.cid, exitCode="2")))
        [row] = self.svc.store.list_crash_snapshots("stack~local~shop")
        self.assertEqual((row["repeats"], row["exitCode"]), (1, 2))

    async def test_a_stop_or_a_clean_exit_is_not_a_crash(self):
        w = self.svc.crashes
        self.assertIsNone(await w.on_event("local", self.ops, _ev("die", self.cid, exitCode="0")))
        await w.on_event("local", self.ops, _ev("kill", self.cid, signal="15"))
        self.assertIsNone(await w.on_event("local", self.ops, _ev("die", self.cid, exitCode="143")))
        off = self.engine.add_container("quiet", "app:1", labels={"kowan.snapshot": "off"}, state="exited", exit_code=1)
        self.assertIsNone(await w.on_event("local", self.ops, _ev("die", off, exitCode="1")))
        self.assertEqual(self.svc.store.list_crash_snapshots(), [])

    async def test_the_watcher_follows_each_hosts_events(self):
        import asyncio

        self.engine.emit("container", "die", self.cid, {"exitCode": "1", "name": "shop-web-1"})
        for _ in range(100):
            if self.svc.store.list_crash_snapshots():
                break
            await asyncio.sleep(0.05)
        [snap] = self.svc.store.list_crash_snapshots()
        self.assertEqual(snap["containerName"], "shop-web-1")
        self.assertTrue(self.svc.crashes.connected.get("local"))

    async def test_api(self):
        snap = await self.svc.crashes.on_event("local", self.ops, _ev("die", self.cid, exitCode="1"))
        _, items = await self.call("GET", "/crash-snapshots", "viewer", params={"workload": "stack~local~shop"}, expect=200)
        self.assertEqual([i["id"] for i in items], [snap["id"]])
        self.assertNotIn("data", items[0])
        _, full = await self.call("GET", f"/crash-snapshots/{snap['id']}", "viewer", expect=200)
        self.assertIn("boom", full["data"]["logs"])
        await self.call("DELETE", f"/crash-snapshots/{snap['id']}", "viewer", expect=403)
        await self.call("DELETE", f"/crash-snapshots/{snap['id']}", "operator", expect=200)
        await self.call("GET", f"/crash-snapshots/{snap['id']}", "viewer", expect=404)


if __name__ == "__main__":
    unittest.main()
