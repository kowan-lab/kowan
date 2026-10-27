"""The network map and host metrics over REST with the fake Engine: graph of
containers / networks / published ports with stack facets and filters, the
all-hosts view tolerating an unreachable host, and metric samples by time."""

import unittest

from kowan.monitor.monitor import Monitor
from tests.apicase import ServerCase


class TestNetmap(ServerCase):
    async def test_host_and_fleet_maps(self):
        self.engine.add_network("shop_default")
        self.engine.add_container("shop-web-1", ports=[(80, 8080)], networks=["shop_default"],
                                  labels={"com.docker.compose.project": "shop", "com.docker.compose.service": "web"})
        self.engine.add_container("shop-db-1", "postgres:16", networks=["shop_default"],
                                  labels={"com.docker.compose.project": "shop", "com.docker.compose.service": "db"})
        self.engine.add_container("lonely")
        _, g = await self.call("GET", "/hosts/local/netmap", "viewer", expect=200)
        self.assertEqual(g["counts"]["containers"], 3)
        self.assertIn("shop", g["stacks"])
        self.assertIn("shop_default", g["networks"])
        self.assertGreaterEqual(g["counts"]["ingress"], 1)
        _, f = await self.call("GET", "/hosts/local/netmap", "viewer", params={"stack": "shop"}, expect=200)
        self.assertEqual(f["counts"]["containers"], 2)
        _, f = await self.call("GET", "/hosts/local/netmap", "viewer", params={"network": "shop_default"}, expect=200)
        self.assertEqual(f["counts"]["containers"], 2)
        await self.call("GET", "/hosts/nope/netmap", "viewer", expect=404)
        # An edge host that is not connected shows up with an error, the rest still renders.
        await self.call("POST", "/hosts", "admin", json={"name": "far", "mode": "edge"}, expect=201)
        _, fleet = await self.call("GET", "/netmap", "viewer", expect=200)
        errs = {h["name"]: h["error"] for h in fleet["hosts"]}
        self.assertEqual(errs["local"], "")
        self.assertTrue(errs["far"])
        self.assertEqual(fleet["counts"]["containers"], 3)


class TestMetrics(ServerCase):
    async def test_samples_and_since(self):
        self.engine.add_container("busy")
        await Monitor(self.svc)._tick()
        _, samples = await self.call("GET", "/hosts/local/metrics", "viewer", expect=200)
        self.assertGreaterEqual(len(samples), 1)  # the server's own monitor also ticked at startup
        _, none = await self.call("GET", "/hosts/local/metrics", "viewer", params={"since": "2999-01-01T00:00:00Z"},
                                  expect=200)
        self.assertEqual(none, [])
        _, some = await self.call("GET", "/hosts/local/metrics", "viewer",
                                  params={"since": "2000-01-01T00:00:00+02:00"}, expect=200)
        # Since 2000 the range is decades: every recent sample lands in one or
        # two averaged buckets.
        self.assertIn(len(some), (1, 2))
        await self.call("GET", "/hosts/local/metrics", "viewer", params={"since": "yesterday"}, expect=400)
        await self.call("GET", "/hosts/nope/metrics", "viewer", expect=404)

    async def test_long_range_is_downsampled(self):
        from datetime import datetime, timedelta, timezone

        from kowan.api.system import MAX_METRIC_POINTS
        from kowan.models import MetricSample

        now = datetime.now(timezone.utc)
        for i in range(3000):  # a sample every 30 s over 25 hours
            t = (now - timedelta(seconds=30 * i)).strftime("%Y-%m-%dT%H:%M:%SZ")
            self.svc.store.create_metric(MetricSample(hostId="local", time=t, running=i % 3, total=5, images=2,
                                                      cpuPercent=10.0 if i % 2 else 30.0, memUsed=100, memTotal=1000))
        since = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _, pts = await self.call("GET", "/hosts/local/metrics", "viewer", params={"since": since}, expect=200)
        self.assertLessEqual(len(pts), MAX_METRIC_POINTS + 1)
        self.assertGreater(len(pts), MAX_METRIC_POINTS // 2)
        mid = pts[len(pts) // 2]
        self.assertAlmostEqual(mid["cpuPercent"], 20.0, delta=10.0)
        self.assertEqual(mid["memUsed"], 100)
        self.assertEqual(mid["total"], 5)
        self.assertEqual([p["time"] for p in pts], sorted(p["time"] for p in pts))
        # A short range keeps every sample.
        since = (now - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _, recent = await self.call("GET", "/hosts/local/metrics", "viewer", params={"since": since}, expect=200)
        self.assertGreaterEqual(len([p for p in recent if p["memUsed"] == 100]), 20)


if __name__ == "__main__":
    unittest.main()
