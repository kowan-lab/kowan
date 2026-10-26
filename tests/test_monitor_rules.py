"""The monitor against the fake Engine: every alert rule type fires on the right
container state, deduplicates while it persists and auto-resolves when it clears;
the sustained-duration gate, host metrics and the alert / alert-rule REST API."""

import unittest

from kowan.monitor.monitor import Monitor, compare, parse_exit_code
from tests.apicase import ServerCase


class MonitorCase(ServerCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.monitor = Monitor(self.svc)

    async def rule(self, **body):
        _, r = await self.call("POST", "/alert-rules", "operator", json={"name": body.get("type"), **body},
                               expect=201)
        return r

    async def open_alerts(self):
        _, alerts = await self.call("GET", "/alerts", "viewer", expect=200)
        return [a for a in alerts if not a["resolved"]]


class TestRuleTypes(MonitorCase):
    async def test_container_down_fires_once_and_resolves(self):
        cid = self.engine.add_container("web")
        await self.rule(type="container_down", severity="critical")
        await self.monitor._tick()  # seen running
        self.assertEqual(await self.open_alerts(), [])
        self.engine.containers[cid]["State"].update(Running=False, Status="exited")
        await self.monitor._tick()
        await self.monitor._tick()  # still down: no duplicate
        alerts = await self.open_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertIn("web", alerts[0]["message"])
        self.engine.containers[cid]["State"].update(Running=True, Status="running")
        await self.monitor._tick()
        self.assertEqual(await self.open_alerts(), [])

    async def test_resource_and_disk_rules(self):
        self.engine.add_container("busy")
        await self.rule(type="cpu_high", operator=">", threshold=5)
        await self.rule(type="mem_high", operator=">", threshold=1)
        await self.rule(type="disk_high", operator=">=", threshold=0)
        await self.monitor._tick()
        titles = sorted(a["message"] for a in await self.open_alerts())
        self.assertEqual(len(titles), 3, titles)
        self.assertTrue(any("GB" in t for t in titles))
        # A metric sample was recorded for the host.
        _, metrics = await self.call("GET", "/hosts/local/metrics", "viewer", expect=200)
        self.assertTrue(metrics)

    async def test_unhealthy_restart_loop_and_exited(self):
        sick = self.engine.add_container("sick", health="unhealthy")
        loop = self.engine.add_container("loopy")
        self.engine.containers[loop]["RestartCount"] = 7
        job = self.engine.add_container("job")
        await self.rule(type="container_unhealthy")
        await self.rule(type="container_restart_loop", operator=">=", threshold=3)
        await self.rule(type="container_exited")
        await self.monitor._tick()
        self.engine.containers[job]["State"].update(Running=False, Status="exited", ExitCode=2)
        await self.monitor._tick()
        msgs = " | ".join(a["message"] for a in await self.open_alerts())
        self.assertIn("sick", msgs)
        self.assertIn("restarted 7 times", msgs)
        self.assertIn("exited with code 2", msgs)
        self.assertTrue(sick)

    async def test_host_down_and_sustained_gate(self):
        await self.call("POST", "/hosts", "admin", json={"name": "far", "mode": "edge"}, expect=201)
        await self.rule(type="host_down")
        # A rule that must hold for an hour never fires within the test.
        self.engine.add_container("gated")
        await self.rule(type="cpu_high", operator=">", threshold=0, forDuration=3600)
        await self.monitor._tick()
        alerts = await self.open_alerts()
        self.assertEqual([a["resource"] for a in alerts], ["far"])
        await self.call("POST", f"/alerts/{alerts[0]['id']}/resolve", "operator", expect=200)
        self.assertEqual(await self.open_alerts(), [])

    async def test_label_and_name_match(self):
        self.engine.add_container("api-1", labels={"tier": "front"})
        self.engine.add_container("db-1", labels={"tier": "back"})
        await self.rule(type="cpu_high", operator=">", threshold=0, labelKey="tier", labelValue="front")
        await self.monitor._tick()
        self.assertEqual([a["resource"] for a in await self.open_alerts()], ["api-1"])


class TestMonitorResilience(MonitorCase):
    async def test_store_and_daemon_failures_never_break_a_tick(self):
        from unittest import mock

        self.engine.add_container("web")
        await self.rule(type="cpu_high", operator=">", threshold=0)
        await self.rule(type="container_restart_loop", operator=">=", threshold=1)
        await self.rule(type="disk_high", operator=">", threshold=0)
        store = self.svc.store
        for name in ("list_hosts", "list_alert_rules", "create_metric", "create_alert", "prune_metrics"):
            with mock.patch.object(store, name, side_effect=RuntimeError("db locked")):
                await self.monitor._tick()
                self.monitor._prune_metrics()
        # The daemon failing individual calls.
        for method, path in (("GET", r"/containers/json"), ("GET", r"/info"), ("GET", r"/system/df"),
                             ("GET", r"/containers/.+/json"), ("GET", r"/containers/.+/stats")):
            self.engine.fail[(method, path)] = (500, "daemon hiccup")
            await self.monitor._tick()
            del self.engine.fail[(method, path)]

    async def test_host_transitions_publish_notices(self):
        _, created = await self.call("POST", "/hosts", "admin", json={"name": "flaky", "mode": "edge"}, expect=201)
        host = self.svc.store.get_host(created["host"]["id"])
        notices = []
        orig = self.monitor._publish_notice
        self.monitor._publish_notice = lambda *a, **k: notices.append(a) or orig(*a, **k)
        self.monitor._note_host_transition(host, False)  # first observation: silent
        self.monitor._note_host_transition(host, True)   # came online
        self.monitor._note_host_transition(host, False)  # went offline
        self.monitor._note_host_transition(host, False)  # no change
        self.assertEqual(len(notices), 2)

    async def test_start_stop_loop(self):
        import asyncio as _a

        m = Monitor(self.svc)
        m._interval = 0.01
        task = _a.ensure_future(m.start())
        await _a.sleep(0.05)
        m.stop()
        await _a.wait_for(task, 2)


class TestRuleApi(MonitorCase):
    async def test_crud_and_validation(self):
        await self.roles("POST", "/alert-rules", "operator", json={"name": "x", "type": "cpu_high"})
        r = await self.rule(type="cpu_high", operator=">", threshold=80)
        self.assertTrue(r["enabled"] and r["autoResolve"])
        _, got = await self.call("GET", f"/alert-rules/{r['id']}", "viewer", expect=200)
        self.assertEqual(got["threshold"], 80)
        _, upd = await self.call("PUT", f"/alert-rules/{r['id']}", "operator", json={"enabled": False}, expect=200)
        self.assertFalse(upd["enabled"])
        self.assertEqual(upd["threshold"], 80)
        _, rules = await self.call("GET", "/alert-rules", "viewer", expect=200)
        self.assertEqual(len(rules), 1)
        _, types = await self.call("GET", "/alert-rule-types", "viewer", expect=200)
        self.assertIn("host_down", [t["type"] for t in types])
        for bad in ({"name": "", "type": "cpu_high"}, {"name": "x", "type": "nope"},
                    {"name": "x", "type": "cpu_high", "operator": "~"}, {"name": "x", "type": "cpu_high", "threshold": "a"},
                    {"name": "x", "type": "cpu_high", "threshold": "nan"}, {"name": "x", "type": "cpu_high", "severity": "meh"},
                    {"name": "x", "type": "cpu_high", "forDuration": -1}, {"name": "x", "type": "cpu_high", "forDuration": "z"}):
            status, _ = await self.call("POST", "/alert-rules", "operator", json=bad)
            self.assertEqual(status, 400, bad)
        await self.call("DELETE", f"/alert-rules/{r['id']}", "operator", expect=200)
        await self.call("GET", f"/alert-rules/{r['id']}", "viewer", expect=404)
        await self.call("DELETE", f"/alert-rules/{r['id']}", "operator", expect=404)
        _, alerts = await self.call("GET", "/alerts", "viewer", params={"limit": "5"}, expect=200)
        self.assertEqual(alerts, [])

    def test_helpers(self):
        self.assertTrue(compare(5, ">", 3) and compare(3, ">=", 3) and compare(1, "<", 2) and compare(2, "<=", 2))
        self.assertTrue(compare(2, "==", 2))
        self.assertTrue(compare(1, "?", 0))  # unknown falls back to ">"
        self.assertEqual(parse_exit_code("Exited (137) 3 seconds ago"), (137, True))
        self.assertEqual(parse_exit_code("Up 2 hours")[1], False)


if __name__ == "__main__":
    unittest.main()
