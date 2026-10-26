"""Disk cleanup: the df details, the plan and its default picks (deployment
containers and named volumes are kept), the run re-checking every item, the
schedule (validation, due slots, the safe picks, the notice) and the API."""

import asyncio
import unittest
from datetime import datetime, timezone
from unittest import mock

from kowan import cleanup
from kowan.docker.local import LocalClient
from kowan.models import DiskItem, DiskUsage
from tests.apicase import ServerCase

H = "/hosts/local"
ANON = "a" * 64


class _Store:
    def __init__(self, deps=()):
        self.deps = list(deps)
        self.settings = {}

    def list_deployments(self):
        return self.deps

    def get_setting(self, k):
        return self.settings.get(k, "")

    def set_setting(self, k, v):
        self.settings[k] = v


class TestPolicy(unittest.TestCase):
    def test_validate(self):
        self.assertEqual(cleanup.validate_policy({}), cleanup.DEFAULT_POLICY)
        p = cleanup.validate_policy({"enabled": True, "every": "daily", "hour": 3, "imagesOlderThanDays": 30})
        self.assertEqual((p["every"], p["hour"], p["imagesOlderThanDays"]), ("daily", 3, 30))
        for bad, msg in (({"every": "hourly"}, "every"), ({"hour": 24}, "hour"), ({"weekday": 7}, "weekday"),
                         ({"hour": True}, "hour"), ({"imagesOlderThanDays": -1}, "imagesOlderThanDays"),
                         ({"enabled": "yes"}, "enabled"), ({"x": 1}, "unknown")):
            with self.assertRaisesRegex(ValueError, msg):
                cleanup.validate_policy(bad)

    def test_due(self):
        p = dict(cleanup.DEFAULT_POLICY, enabled=True, every="weekly", weekday=6, hour=4)
        sunday4 = datetime(2026, 10, 25, 4, 10, tzinfo=timezone.utc)
        self.assertTrue(cleanup.due(p, "", sunday4))
        self.assertFalse(cleanup.due(p, "2026-10-25T04:00:00Z", sunday4))  # done in this slot
        self.assertTrue(cleanup.due(p, "2026-10-18T04:00:00Z", sunday4))
        self.assertFalse(cleanup.due(p, "", sunday4.replace(hour=5)))
        self.assertFalse(cleanup.due(p, "", datetime(2026, 10, 26, 4, tzinfo=timezone.utc)))  # Monday
        self.assertTrue(cleanup.due(dict(p, every="daily"), "", datetime(2026, 10, 26, 4, tzinfo=timezone.utc)))
        self.assertFalse(cleanup.due(dict(p, enabled=False), "", sunday4))

    def test_store_roundtrip_and_human(self):
        st = _Store()
        self.assertEqual(cleanup.get_policy(st, "h"), cleanup.DEFAULT_POLICY)
        cleanup.set_policy(st, "h", dict(cleanup.DEFAULT_POLICY, hour=2))
        self.assertEqual(cleanup.get_policy(st, "h")["hour"], 2)
        self.assertIsNone(cleanup.last_run(st, "h"))
        self.assertEqual([cleanup._human(n) for n in (5, 1536, 5 << 19, 3 << 30, 4 << 40)],
                         ["5 B", "1.5 KB", "2.5 MB", "3.0 GB", "4.0 TB"])


class TestPlan(unittest.TestCase):
    def test_defaults(self):
        from kowan.models import Deployment

        du = DiskUsage(
            stoppedContainers=[DiskItem(id="c1", name="old-job", size=100, image="alpine", state="exited"),
                               DiskItem(id="c2", name="api", size=50, state="exited", labels={"kowan.deployment.id": "d1"}),
                               DiskItem(id="c3", name="shop-web-1", size=7, state="created", labels={"com.docker.compose.project": "shop"})],
            unusedImages=[DiskItem(id="sha256:" + "b" * 64, name="", size=10, created=0),
                          DiskItem(id="sha256:i2", name="nginx:1.25", size=1000, created=1_700_000_000),
                          DiskItem(id="sha256:i3", name="app:dev", size=70, created=1_792_195_200)],
            unusedVolumes=[DiskItem(id=ANON, name=ANON, size=5), DiskItem(id="pgdata", name="pgdata", size=900)],
            buildCacheReclaimable=300, imagesSize=1, containersSize=2, volumesSize=3, buildCacheSize=4,
        )
        st = _Store([Deployment(id="d1", name="api", type="container"),
                     Deployment(id="d2", name="shop", type="compose", targets=["local"])])
        p = cleanup.plan(du, "local", st, now=datetime(2026, 10, 26, tzinfo=timezone.utc))
        by = {c["id"]: c for c in p["categories"]}
        self.assertEqual([(i["id"], i["selected"]) for i in by["containers"]["items"]], [("c1", True), ("c2", False), ("c3", False)])
        self.assertIn("part of api", by["containers"]["items"][1]["note"])
        self.assertIn("part of shop", by["containers"]["items"][2]["note"])
        self.assertEqual(by["dangling"]["items"][0]["name"], "b" * 12)
        self.assertEqual((by["images"]["items"][0]["detail"], by["images"]["items"][0]["selected"]), ("1076 days old", True))
        self.assertEqual([cleanup._age(d) for d in (None, 0, 1, 5)], ["", "from today", "1 day old", "5 days old"])
        self.assertEqual([(i["name"], i["selected"]) for i in by["volumes"]["items"]], [(ANON[:12], True), ("pgdata", False)])
        self.assertEqual(by["build"]["items"][0]["size"], 300)
        recent = by["images"]["items"][1]
        self.assertEqual((recent["detail"], recent["selected"]), ("9 days old", False))
        self.assertIn("newer than 30 days are not picked", by["images"]["help"])
        self.assertEqual(p["total"], 100 + 50 + 7 + 10 + 1000 + 70 + 5 + 900 + 300)
        self.assertEqual(p["selectedTotal"], 100 + 10 + 1000 + 5 + 300)
        self.assertEqual(p["disk"], {"images": 1, "containers": 2, "volumes": 3, "buildCache": 4})
        # What a schedule picks.
        pol = dict(cleanup.DEFAULT_POLICY, imagesOlderThanDays=1000, anonymousVolumes=True)
        pick = cleanup.scheduled_pick(p, pol, datetime.now(timezone.utc))
        self.assertEqual((pick.containers, pick.volumes, pick.build_cache), (["c1"], [ANON], True))
        self.assertEqual(pick.images, ["sha256:" + "b" * 64, "sha256:i2"])
        none = cleanup.scheduled_pick(p, dict(cleanup.DEFAULT_POLICY, containers=False, dangling=False, buildCache=False),
                                      datetime.now(timezone.utc))
        self.assertEqual((none.containers, none.images, none.volumes, none.build_cache), ([], [], [], False))
        empty = cleanup.plan(DiskUsage(), "local", _Store())
        self.assertEqual((empty["total"], [c["items"] for c in empty["categories"]]), (0, [[]] * 5))


class TestDfDetails(unittest.TestCase):
    def test_parse(self):
        from kowan.docker.local import _disk_usage_from_engine

        du = _disk_usage_from_engine({
            "Images": [{"Id": "i1", "Size": 10, "SharedSize": 4, "Containers": 0, "RepoTags": ["<none>:<none>"]},
                       {"Id": "i2", "Size": 5, "Containers": 1, "RepoTags": ["x:1"]}, None],
            "Containers": [{"Id": "c1", "State": "exited", "SizeRw": 3, "Names": ["/a"], "Image": "x:1"},
                           {"Id": "c2", "State": "running", "SizeRw": 1}, {"Id": "c3", "State": "paused"}, None],
            "Volumes": [{"Name": "v", "UsageData": {"Size": -1, "RefCount": 0}}, {"Name": "w"}, None],
            "BuildCache": [{"Size": 7, "InUse": False}, {"Size": 2, "InUse": True}, None],
        })
        self.assertEqual([(i.id, i.name, i.size) for i in du.unused_images], [("i1", "", 6)])
        self.assertEqual([(c.id, c.name, c.state) for c in du.stopped_containers], [("c1", "a", "exited")])
        self.assertEqual([(v.name, v.size) for v in du.unused_volumes], [("v", 0)])
        self.assertEqual((du.build_cache_size, du.build_cache_reclaimable), (9, 7))


class TestCleanupAPI(ServerCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.svc.cleanup.stop()
        e = self.engine
        self.job = e.add_container("old-job", image="alpine:3.22", state="exited")
        self.dep_ctr = e.add_container("api-1", image="ghcr.io/acme/api:1", state="exited", labels={"kowan.deployment.id": "dx"})
        self.web = e.add_container("web", image="nginx:1.27", binds=["used:/data"])
        self.unused = e.add_image("redis:7")
        e.add_volume(ANON)
        e.add_volume("pgdata")
        e.add_volume("used")

    async def test_plan_and_run(self):
        _, p = await self.call("GET", H + "/cleanup", "viewer", expect=200)
        by = {c["id"]: c for c in p["categories"]}
        self.assertEqual({i["name"]: i["selected"] for i in by["containers"]["items"]}, {"old-job": True, "api-1": False})
        self.assertIn("redis:7", [i["name"] for i in by["images"]["items"]])
        self.assertNotIn("nginx:1.27", [i["name"] for i in by["images"]["items"]])  # used by web
        self.assertEqual({i["id"] for i in by["volumes"]["items"]}, {ANON, "pgdata"})
        self.assertEqual(by["build"]["items"][0]["size"], 512)
        self.assertEqual(p["schedule"], cleanup.DEFAULT_POLICY)
        self.assertIsNone(p["lastRun"])

        await self.roles("POST", H + "/cleanup", "operator", json={"buildCache": True})
        _, out = await self.call("POST", H + "/cleanup", "operator", json={
            "containers": [self.job, self.web], "images": [self.unused, "sha256:nope"], "volumes": [ANON, "used"], "buildCache": True,
        }, expect=200)
        self.assertEqual(out["removed"], {"containers": 1, "images": 1, "volumes": 1, "buildCache": 1})
        self.assertEqual(out["freed"], 1024 + 10_000_000 + 4096 + 512)
        self.assertEqual({p["id"] for p in out["problems"]}, {self.web, "sha256:nope", "used"})
        self.assertNotIn(self.job, self.engine.containers)
        self.assertIn("pgdata", self.engine.volumes)
        self.assertNotIn(ANON, self.engine.volumes)
        # The image of a removed container can go in the same run.
        img = self.engine.containers[self.dep_ctr]["Image"]
        _, out = await self.call("POST", H + "/cleanup", json={"containers": [self.dep_ctr], "images": [img]}, expect=200)
        self.assertEqual(out["removed"]["images"], 1)
        for body in ({}, {"containers": "x"}, {"images": [1]}, {"x": 1}, ["x"]):
            await self.call("POST", H + "/cleanup", json=body, expect=400)
        await self.call("GET", "/hosts/nope/cleanup", expect=404)
        await self.call("POST", "/hosts/nope/cleanup", json={"buildCache": True}, expect=404)

    async def test_run_reports_removal_errors(self):
        async def boom(*a, **k):
            raise RuntimeError("engine said no")

        ops = self.svc.docker.ops(self.svc.store.get_host("local"))
        with mock.patch.object(LocalClient, "remove_container", boom), mock.patch.object(LocalClient, "remove_image", boom), \
                mock.patch.object(LocalClient, "remove_volume", boom), mock.patch.object(LocalClient, "prune_build_cache", boom):
            out = await cleanup.run(ops, cleanup.Picked([self.job], [self.unused], [ANON], True))
        self.assertEqual(out["freed"], 0)
        self.assertEqual([p["error"] for p in out["problems"]], ["engine said no"] * 4)
        with mock.patch.object(LocalClient, "disk_usage", boom):
            _, err = await self.call("GET", H + "/cleanup", expect=502)
            self.assertIn("engine said no", err["error"])
            _, err = await self.call("POST", H + "/cleanup", json={"buildCache": True}, expect=502)

    async def test_schedule(self):
        await self.roles("PUT", H + "/cleanup/schedule", "operator", json={})
        _, pol = await self.call("PUT", H + "/cleanup/schedule", "operator",
                                 json={"enabled": True, "every": "daily", "hour": 4, "anonymousVolumes": True}, expect=200)
        self.assertTrue(pol["enabled"])
        await self.call("PUT", H + "/cleanup/schedule", json={"hour": 30}, expect=400)
        await self.call("PUT", H + "/cleanup/schedule", data=b"[1]", expect=400)
        await self.call("PUT", "/hosts/nope/cleanup/schedule", json={}, expect=404)

        sub = self.svc.bus.subscribe("notice")
        at4 = datetime(2026, 10, 26, 4, 5, tzinfo=timezone.utc)
        self.assertEqual(await self.svc.cleanup.tick(at4.replace(hour=3)), 0)
        self.assertEqual(await self.svc.cleanup.tick(at4), 1)
        self.assertEqual(await self.svc.cleanup.tick(at4), 0)  # already ran in this slot
        ev = await asyncio.wait_for(sub.__aiter__().__anext__(), 2)
        self.assertIn("Cleanup on local freed", ev.payload["title"])
        self.assertEqual(ev.payload["source"], "cleanup")
        self.assertNotIn(self.job, self.engine.containers)  # unmanaged stopped container
        self.assertIn(self.dep_ctr, self.engine.containers)  # a deployment's: kept
        self.assertIn("pgdata", self.engine.volumes)  # named: kept
        self.assertNotIn(ANON, self.engine.volumes)
        _, p = await self.call("GET", H + "/cleanup", expect=200)
        self.assertEqual(p["lastRun"]["at"], "2026-10-26T04:05:00Z")
        self.assertEqual(p["lastRun"]["removed"]["volumes"], 1)

        # A failing host is recorded and announced.
        async def boom(*a, **k):
            raise RuntimeError("host offline")

        with mock.patch.object(LocalClient, "disk_usage", boom):
            rec = await self.svc.cleanup.run_host(self.svc.store.get_host("local"), at4.replace(day=27))
        self.assertEqual(rec["error"], "host offline")
        ev = await asyncio.wait_for(sub.__aiter__().__anext__(), 2)
        self.assertIn("failed", ev.payload["title"])

        # The loop keeps going after errors.
        with mock.patch.object(cleanup, "_CHECK_EVERY", 0.01):
            s = cleanup.Scheduler(self.svc)
            calls = []

            async def bad(now=None):
                calls.append(1)
                raise RuntimeError("x")

            s.tick = bad
            s.start()
            await asyncio.sleep(0.05)
            await s.stop()
            await s.stop()
        self.assertGreater(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
