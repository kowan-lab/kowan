"""The workload, history and operations MCP tools end to end: workloads
grouped like the UI, stack actions, the log archive, resource history,
revisions and rollback, promotion, uptime, cleanup, schedules, backups and
the audit log."""

import json
import unittest
from datetime import datetime, timedelta, timezone

from tests.test_mcp_e2e import MCPCase

STACK = {"com.docker.compose.project": "monitoring", "com.docker.compose.service": "grafana"}


def ts(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


class TestWorkloadTools(MCPCase):
    async def test_list_get_and_act_on_workloads(self):
        self.engine.add_container("grafana", "grafana/grafana:11", labels=STACK)
        self.engine.add_container("prom", "prom/prometheus:2", labels={**STACK, "com.docker.compose.service": "prom"},
                                  state="exited", exit_code=1)
        self.engine.add_container("lone", "redis:7")
        _, dep = await self.call("POST", "/deployments", "operator", json={
            "name": "shop", "type": "container", "strategy": "standalone", "targets": ["local"],
            "spec": {"image": "nginx:1.27"}}, expect=201)

        out, _ = await self.tool("list_workloads", "viewer")
        by_name = {w["name"]: w for w in out["items"]}
        self.assertEqual(by_name["monitoring"]["id"], "stack~local~monitoring")
        self.assertEqual(by_name["monitoring"]["state"], "needs attention")
        self.assertEqual(sorted(by_name["monitoring"]["containers"]), ["grafana", "prom"])
        self.assertEqual(by_name["lone"]["kind"], "container")
        self.assertEqual(by_name["shop"]["id"], f"dep~{dep['id']}")
        self.assertEqual(by_name["shop"]["state"], "no containers")
        self.assertEqual(out["items"][0]["name"], "monitoring")  # problems first
        out, _ = await self.tool("list_workloads", "viewer", kind="stack")
        self.assertEqual([w["name"] for w in out["items"]], ["monitoring"])

        out, _ = await self.tool("get_workload", "viewer", workload="monitoring")
        self.assertEqual({c["name"]: c["problem"] for c in out["containers"]}, {"grafana": False, "prom": True})
        await self.tool("get_workload", "viewer", ok=False, workload="nope")

        await self.tool("workload_action", "viewer", ok=False, workload="monitoring", action="stop")
        out, _ = await self.tool("workload_action", "operator", workload="stack~local~monitoring", action="stop")
        self.assertEqual(out["containers"], ["grafana"])  # prom was not running
        self.assertFalse(self.engine.container("grafana")["State"]["Running"])
        out, _ = await self.tool("workload_action", "operator", workload="monitoring", action="start")
        self.assertEqual(sorted(out["containers"]), ["grafana", "prom"])
        out, _ = await self.tool("workload_action", "operator", workload="shop", action="start")
        self.assertEqual(out["via"], "deployment")

    async def test_resources_and_memory_growth(self):
        self.engine.add_container("lone", "redis:7")
        wid = "ctr~local~lone"
        out, _ = await self.tool("workload_resources", "viewer", workload=wid)
        self.assertIn("no history", out["note"])
        mib = 1024 * 1024
        rows = [{"workload": wid, "host_id": "local", "time": ts(300 - i * 5), "samples": 1, "containers": 1,
                 "cpu_avg": 5.0, "cpu_max": 9.0, "mem_avg": (100 + i * 4) * mib, "mem_max": (101 + i * 4) * mib,
                 "mem_limit": 1024 * mib, "net_rx": 0, "net_tx": 0} for i in range(60)]
        self.svc.store.add_workload_metrics(rows)
        self.svc.store.set_workload_labels(wid, "local", {})
        out, _ = await self.tool("workload_resources", "viewer", workload="lone", hours=6)
        self.assertEqual(out["summary"]["cpuAvgPercent"], 5.0)
        self.assertTrue(out["memoryTrend"]["growing"])
        out, _ = await self.tool("memory_growth", "viewer")
        self.assertEqual([w["workload"] for w in out["items"]], [wid])
        self.assertIn("hoursToLimit", out["items"][0])

    async def test_log_archive(self):
        self.engine.add_container("lone", "redis:7")
        out, _ = await self.tool("search_logs", "viewer", query="boom")
        self.assertIn("no workload keeps its logs", out["note"])
        await self.tool("set_log_retention", "viewer", ok=False, workload="lone", days=7)
        out, _ = await self.tool("set_log_retention", "operator", workload="lone", days=7)
        self.assertEqual(out, {"workload": "ctr~local~lone", "kept": True, "days": 7})
        logs = self.svc.logs
        src, _ = logs.source("local", "abc", "lone", "ctr~local~lone", "", "")
        logs.append(src, [("2026-01-01T00:00:00.000000000Z", "started"),
                          ("2026-01-01T00:00:01.000000000Z", "panic: boom " + "x" * 3000)])
        out, raw = await self.tool("search_logs", "viewer", query="boom", workload="lone")
        self.assertEqual(len(out["items"]), 1)
        self.assertTrue(out["items"][0]["line"].endswith("[cut]"))
        out, _ = await self.tool("search_logs", "viewer", limit=1)
        self.assertIn("next", out)
        out, _ = await self.tool("search_logs", "viewer", limit=1, before=out["next"])
        self.assertEqual(out["items"][0]["line"], "started")
        out, _ = await self.tool("set_log_retention", "operator", workload="ctr~local~lone", days=0)
        self.assertEqual(out["linesDeleted"], 2)


class TestHistoryTools(MCPCase):
    async def test_revisions_and_rollback(self):
        self.engine.add_image("nginx:1.28")
        _, dep = await self.call("POST", "/deployments", "operator", json={
            "name": "web", "type": "container", "strategy": "standalone", "targets": ["local"],
            "spec": {"image": "nginx:1.27", "env": [{"key": "PW", "value": "hunter2"}]}}, expect=201)
        _, got = await self.call("GET", f"/deployments/{dep['id']}", "operator")
        got["spec"]["image"] = "nginx:1.28"
        await self.call("PUT", f"/deployments/{dep['id']}", "operator", json=got, expect=200)

        out, _ = await self.tool("list_revisions", "viewer", deployment="web")
        self.assertEqual([r["number"] for r in out["items"]], [2, 1])
        out, raw = await self.tool("get_revision", "viewer", deployment="web", revision=2)
        self.assertNotIn("hunter2", json.dumps(raw))
        self.assertTrue(any(line["op"] == "+" and "nginx:1.28" in line["text"] for line in out["lines"]))
        await self.tool("get_revision", "viewer", ok=False, deployment="web", revision=9)

        await self.tool("rollback_deployment", "viewer", ok=False, deployment="web", revision=1)
        out, _ = await self.tool("rollback_deployment", "operator", deployment="web", revision=1, deploy=False)
        self.assertEqual(out["rolledBackTo"], 1)
        self.assertEqual(self.svc.store.get_deployment(dep["id"]).spec.image, "nginx:1.27")
        out, _ = await self.tool("list_revisions", "viewer", deployment="web", limit=1)
        self.assertEqual((out["items"][0]["source"], out["items"][0]["note"]), ("rollback", "rollback to #1"))
        self.assertIn("(mcp)", out["items"][0]["author"])

    async def test_promotion_and_previews(self):
        for name, image in (("staging", "nginx:1.28"), ("prod", "nginx:1.27")):
            await self.call("POST", "/deployments", "operator", json={
                "name": name, "type": "container", "strategy": "standalone", "targets": ["local"],
                "spec": {"image": image}}, expect=201)
        await self.tool("promotion_plan", "viewer", ok=False, deployment="staging")  # no target configured
        out, _ = await self.tool("promotion_plan", "viewer", deployment="staging", to="prod")
        self.assertFalse(out["upToDate"])
        self.assertTrue(out["changes"])
        await self.tool("promote_deployment", "viewer", ok=False, deployment="staging", to="prod")
        out, _ = await self.tool("promote_deployment", "operator", deployment="staging", to="prod", deploy=False)
        self.assertEqual(out["to"], "prod")
        self.assertEqual(self.svc.store.get_deployment_by_name("prod").spec.image, "nginx:1.28")
        out, _ = await self.tool("promotion_plan", "viewer", deployment="staging", to="prod")
        self.assertTrue(out["upToDate"])
        await self.tool("promote_deployment", "operator", ok=False, deployment="staging", to="prod")
        out, _ = await self.tool("list_previews", "viewer", deployment="staging")
        self.assertEqual(out["items"], [])


class TestOperationsTools(MCPCase):
    async def test_uptime_cleanup_schedules_backups_audit(self):
        _, check = await self.call("POST", "/uptime/checks", "operator", json={
            "name": "closed", "kind": "tcp", "target": "127.0.0.1:1", "intervalSec": 3600}, expect=201)
        out, _ = await self.tool("uptime_status", "viewer")
        self.assertEqual([c["name"] for c in out["checks"]], ["closed"])
        await self.tool("run_uptime_check", "viewer", ok=False, check="closed")
        out, _ = await self.tool("run_uptime_check", "operator", check="closed")
        self.assertFalse(out["ok"])
        await self.tool("run_uptime_check", "operator", ok=False, check="missing")

        self.engine.add_container("old", "redis:7", state="exited")
        self.engine.add_volume("0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")
        self.engine.add_volume("precious")
        out, _ = await self.tool("cleanup_plan", "viewer")
        cats = {c["category"]: c for c in out["categories"]}
        self.assertEqual([i["name"] for i in cats["containers"]["items"]], ["old"])
        vols = {i["name"]: i["suggested"] for i in cats["volumes"]["items"]}
        self.assertFalse(vols["precious"])
        await self.tool("run_cleanup", "operator", ok=False, picks="suggested", confirm=True)
        await self.tool("run_cleanup", ok=False, picks="suggested")  # needs confirm
        out, _ = await self.tool("run_cleanup", picks="suggested", confirm=True)
        self.assertEqual(out["removed"]["containers"], 1)
        self.assertIsNone(self.engine.container("old"))
        self.assertIn("precious", self.engine.volumes)

        out, _ = await self.tool("list_schedules", "viewer")
        self.assertEqual(out["items"], [])
        await self.tool("run_schedule", "operator", ok=False, schedule="nope")
        out, _ = await self.tool("list_backups", "viewer")
        self.assertEqual(out["jobs"], [])
        await self.tool("run_backup", "operator", ok=False, job="nope")

        await self.tool("audit_log", "operator", ok=False)
        out, _ = await self.tool("audit_log", path="/mcp/tools/run_cleanup")
        self.assertTrue(out["items"])
        self.assertIn("source=mcp", out["items"][0]["detail"])


if __name__ == "__main__":
    unittest.main()
