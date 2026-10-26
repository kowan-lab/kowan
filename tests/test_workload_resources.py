"""Resource history per workload: the trend fit and the growth rule, the
5-minute buckets the monitor writes (from the fake Engine's stats), the
memory-growth warning and its recovery, pruning, and the REST API."""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from kowan.models import Container, ContainerStats
from kowan.monitor.monitor import Monitor
from kowan.monitor.workloads import (
    BUCKET_SECS, MIN_POINTS, WorkloadHistory, growth_of, is_growing, memory_trend,
)
from kowan.store import open_store
from tests.apicase import ServerCase

MiB = 1024 * 1024
T0 = datetime(2026, 10, 20, 0, 0, tzinfo=timezone.utc)


def at(minutes: float) -> str:
    return (T0 + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def ctr(cid: str, name: str, labels=None) -> Container:
    return Container(id=cid, names=["/" + name], image="x", state="running", labels=labels or {})


def stats(mem: int, cpu: float = 10.0, limit: int = 0) -> ContainerStats:
    return ContainerStats(cpuPercent=cpu, memUsage=mem, memLimit=limit, netRx=100, netTx=200)


class TestTrend(unittest.TestCase):
    def series(self, n, start, step, limit=0, containers=1):
        return [(at(i * 5), start + i * step, limit, containers) for i in range(n)]

    def test_fit(self):
        self.assertIsNone(memory_trend([]))
        self.assertIsNone(memory_trend(self.series(1, 100, 0)))
        self.assertIsNone(memory_trend([(at(0), 1, 0, 1), (at(0), 2, 0, 1)]))  # no time span
        t = memory_trend(self.series(36, 200 * MiB, 3 * MiB, limit=1024 * MiB))  # 3h, +36 MiB/h
        self.assertEqual(t["points"], 36)
        self.assertAlmostEqual(t["slopeBytesPerHour"] / MiB, 36, places=3)
        self.assertEqual(t["r2"], 1.0)
        self.assertEqual(t["start"], 200 * MiB)
        self.assertFalse(t["restarted"])
        # (1024 - 305) MiB left at 36 MiB/h.
        self.assertAlmostEqual(t["hoursToLimit"], (1024 - 305) / 36, places=0)
        flat = memory_trend(self.series(30, 100 * MiB, 0))
        self.assertEqual((flat["slopeBytesPerHour"], flat["r2"], flat["hoursToLimit"]), (0, 1.0, None))

    def test_growth_rule(self):
        self.assertFalse(is_growing(None))
        self.assertTrue(is_growing(memory_trend(self.series(36, 200 * MiB, 3 * MiB))))
        # Too few points, too short, too small, too small relative, falling, restarted, noisy.
        self.assertFalse(is_growing(memory_trend(self.series(MIN_POINTS - 1, 100 * MiB, 10 * MiB))))
        short = [(at(i * 2), 100 * MiB + i * 10 * MiB, 0, 1) for i in range(40)]  # 78 minutes
        self.assertFalse(is_growing(memory_trend(short)))
        self.assertFalse(is_growing(memory_trend(self.series(36, 100 * MiB, MiB // 2))))  # +17 MiB
        self.assertFalse(is_growing(memory_trend(self.series(36, 4000 * MiB, 2 * MiB))))  # +70 MiB of 4 GiB
        self.assertFalse(is_growing(memory_trend(self.series(36, 900 * MiB, -10 * MiB))))
        restarted = self.series(36, 200 * MiB, 3 * MiB)
        restarted[-1] = (restarted[-1][0], restarted[-1][1], 0, 2)
        self.assertFalse(is_growing(memory_trend(restarted)))
        noisy = [(at(i * 5), 200 * MiB + (i % 2) * 300 * MiB + i * MiB, 0, 1) for i in range(36)]
        self.assertFalse(is_growing(memory_trend(noisy)))


class TestHistory(unittest.TestCase):
    def setUp(self):
        self.store = open_store(os.path.join(tempfile.mkdtemp(), "kowan.db"))
        self.events = []
        self.h = WorkloadHistory(self.store, self)
        self.web = ctr("c1", "web")
        self.api = ctr("c2", "api-1", {"kowan.deployment.id": "d1"})
        self.db = ctr("c3", "shop-db-1", {"com.docker.compose.project": "shop"})

    def publish(self, ev):  # the history publishes on "the bus": this test
        self.events.append(ev)

    def tearDown(self):
        self.store.close()

    def test_buckets(self):
        running = {"c1": self.web, "c2": self.api, "c3": self.db, "c4": ctr("c4", "nostats")}
        # Two ticks in the first bucket, one in the next.
        self.h.observe("local", running, {"c1": stats(100 * MiB, 10), "c2": stats(50 * MiB, 5, 512 * MiB), "c3": stats(MiB)},
                       {("local", "shop"): "d-shop"}, now=T0)
        self.h.observe("local", running, {"c1": stats(200 * MiB, 30), "c2": stats(50 * MiB, 5, 512 * MiB), "c3": stats(MiB)},
                       {("local", "shop"): "d-shop"}, now=T0 + timedelta(seconds=60))
        self.assertEqual(self.store.list_workload_metrics("ctr~local~web", at(-60)), [])  # bucket still open
        self.h.observe("local", running, {"c1": stats(300 * MiB, 20)}, {}, now=T0 + timedelta(seconds=BUCKET_SECS))
        pts = self.store.list_workload_metrics("ctr~local~web", at(-60))
        self.assertEqual(pts, [{"time": at(0), "containers": 1, "cpu": 20.0, "cpuMax": 30.0, "mem": 150 * MiB,
                                "memMax": 200 * MiB, "memLimit": 0, "netRx": 100, "netTx": 200}])
        self.assertEqual(self.store.list_workload_metrics("dep~d1", at(-60))[0]["memLimit"], 512 * MiB)
        # A compose project owned by a deployment counts as that deployment.
        self.assertEqual(len(self.store.list_workload_metrics("dep~d-shop", at(-60))), 1)
        self.assertEqual(self.store.workload_hosts("dep~d1"), {"local": {"kowan.deployment.id": "d1"}})
        self.h.flush()
        self.h.flush()  # nothing open: no-op
        self.assertEqual(len(self.store.list_workload_metrics("ctr~local~web", at(-60))), 2)
        # Long ranges are hourly.
        hourly = self.store.list_workload_metrics("ctr~local~web", at(-60), hours=72)
        self.assertEqual([p["time"] for p in hourly], ["2026-10-20T00:00:00Z"])
        self.assertEqual(hourly[0]["memMax"], 300 * MiB)
        # Pruning drops old rows and the labels of workloads left without history.
        self.assertEqual(self.h.prune(T0 + timedelta(days=31)), 4)
        self.assertEqual(self.store.workload_hosts("dep~d1"), {})

    def grow(self, n, start, step, offset=0):
        for i in range(n):
            self.h.observe("local", {"c1": self.web}, {"c1": stats(start + i * step)}, {},
                           now=T0 + timedelta(seconds=(offset + i) * BUCKET_SECS))
        self.h.flush()

    def test_growth_warning_and_recovery(self):
        self.grow(36, 200 * MiB, 3 * MiB)
        alerts = [a for a in self.store.list_alerts(10) if a.rule_id == "memgrowth:ctr~local~web"]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].level, "warning")
        self.assertIn("Memory keeps growing: web", alerts[0].title)
        self.assertIn("without a restart", alerts[0].message)
        self.assertEqual([e.payload["resolved"] for e in self.events], [False])
        g = growth_of(self.store, "ctr~local~web", ["local"], now=T0 + timedelta(minutes=36 * 5))
        self.assertTrue(g["growing"])
        self.assertEqual(g["hostId"], "local")
        self.assertIsNone(growth_of(self.store, "ctr~local~web", [], now=T0))
        # Still growing: no second alert.
        self.h.check_growth("ctr~local~web", "local", "web", T0 + timedelta(minutes=36 * 5))
        self.assertEqual(len(self.events), 1)
        # Flat for six hours: resolved, and a recovery is published.
        self.grow(72, 310 * MiB, 0, offset=36)
        self.assertFalse(any(not a.resolved for a in self.store.list_alerts(10)))
        self.assertEqual([e.payload["resolved"] for e in self.events], [False, True])
        self.assertIn("stable again", self.events[-1].payload["title"])

    def test_limit_in_message(self):
        for i in range(36):
            self.h.observe("local", {"c1": self.web}, {"c1": stats(200 * MiB + i * 3 * MiB, limit=1024 * MiB)}, {},
                           now=T0 + timedelta(seconds=i * BUCKET_SECS))
        self.h.flush()
        self.assertIn("reaches its limit in about", self.store.list_alerts(1)[0].message)


class TestMonitorAndAPI(ServerCase):
    async def test_monitor_records_and_api_serves(self):
        self.engine.add_container("web", labels={"team": "web"})
        self.engine.add_container("other", labels={"team": "db"})
        m = Monitor(self.svc)
        await m._tick()
        m.history.flush()
        _, res = await self.call("GET", "/resource-history/ctr~local~web", "viewer", expect=200)
        self.assertEqual(res["hours"], 24)
        self.assertEqual(len(res["points"]), 1)
        p = res["points"][0]
        self.assertEqual(p["containers"], 1)
        self.assertGreater(p["mem"], 0)
        self.assertEqual(p["memLimit"], 1 << 30)
        self.assertEqual(res["growth"], None)  # one point: no trend yet
        _, res = await self.call("GET", "/resource-history/ctr~local~none", params={"hours": "720"}, expect=200)
        self.assertEqual((res["points"], res["growth"]), ([], None))
        for params in ({"hours": "0"}, {"hours": "721"}, {"hours": "x"}):
            await self.call("GET", "/resource-history/ctr~local~web", params=params, expect=400)
        await self.call("GET", "/resource-history/nope", expect=400)

        # A team-scoped viewer only reads its own workloads.
        _, users = await self.call("GET", "/users")
        uid = next(u["id"] for u in users if u["username"] == "viewer")
        _, team = await self.call("POST", "/teams", json={"name": "web"}, expect=201)
        await self.call("POST", f"/teams/{team['id']}/members", json={"userId": uid}, expect=201)
        await self.call("POST", "/grants", json={"subjectType": "team", "subjectId": team["id"], "scope": "labels",
                                                  "target": "team=web", "level": "read"}, expect=201)
        await self.call("GET", "/resource-history/ctr~local~web", "viewer", expect=200)
        await self.call("GET", "/resource-history/ctr~local~other", "viewer", expect=403)
        _, dep = await self.call("POST", "/deployments", json={
            "name": "api", "type": "container", "strategy": "standalone", "targets": ["local"],
            "spec": {"image": "nginx:1.27"}}, expect=201)
        await self.call("GET", f"/resource-history/dep~{dep['id']}", "viewer", expect=403)
        await self.call("POST", "/grants", json={"subjectType": "team", "subjectId": team["id"], "scope": "deployment",
                                                  "target": dep["id"], "level": "read"}, expect=201)
        await self.call("GET", f"/resource-history/dep~{dep['id']}", "viewer", expect=200)


if __name__ == "__main__":
    unittest.main()
