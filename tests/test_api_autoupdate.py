"""Auto-update REST API against the fake Engine: settings, per-container and
per-deployment policies (labels win over stored values), the overview, a manual
check that finds a newer image, and the history."""

import asyncio
import unittest

from tests.apicase import ServerCase

H = "/hosts/local"


class AutoUpdateCase(ServerCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()

        async def remote(ref, refresh):
            return "sha256:" + "f" * 64, ""

        # A registry answering with a digest different from the local image.
        self.svc.autoupdater._remote_digest = remote


class TestSettings(AutoUpdateCase):
    async def test_get_put_and_validation(self):
        _, cfg = await self.call("GET", "/autoupdate/settings", "viewer", expect=200)
        self.assertIn("intervalSeconds", cfg)
        await self.roles("PUT", "/autoupdate/settings", "admin", json={"enabled": False})
        _, cfg = await self.call("PUT", "/autoupdate/settings", "admin", json={
            "enabled": False, "intervalSeconds": 7200, "defaultScanBlock": "critical", "verifyTimeoutSeconds": 99999},
            expect=200)
        self.assertEqual((cfg["enabled"], cfg["intervalSeconds"]), (False, 7200))
        self.assertEqual(cfg["verifyTimeoutSeconds"], 1800)  # clamped
        await self.call("PUT", "/autoupdate/settings", "admin", json={"intervalSeconds": 1}, expect=400)
        await self.call("PUT", "/autoupdate/settings", "admin", json={"defaultScanBlock": "sometimes"}, expect=400)


class TestContainerPolicy(AutoUpdateCase):
    async def test_store_read_delete(self):
        cid = self.engine.add_container("web", "nginx:1.27")
        path = f"{H}/containers/{cid}/autoupdate"
        _, pol = await self.call("GET", path, "viewer", expect=200)
        self.assertEqual(pol["effective"]["mode"], "off")
        await self.roles("PUT", path, "operator", json={"mode": "notify"})
        _, pol = await self.call("PUT", path, "operator", json={"mode": "apply", "window": "02:00-05:00", "minAge": "3d"},
                                 expect=200)
        self.assertEqual(pol["effective"]["mode"], "apply")
        self.assertEqual(pol["stored"]["window"], "02:00-05:00")
        await self.call("PUT", path, "operator", json={"mode": "sometimes"}, expect=400)
        await self.call("PUT", path, "operator", json={"mode": "apply", "window": "25:00-26:00"}, expect=400)
        _, pol = await self.call("DELETE", path, "operator", expect=200)
        self.assertIsNone(pol["stored"])

    async def test_labels_win_over_stored(self):
        cid = self.engine.add_container("labelled", labels={"kowan.update": "notify"})
        path = f"{H}/containers/{cid}/autoupdate"
        await self.call("PUT", path, "operator", json={"mode": "apply"}, expect=200)
        _, pol = await self.call("GET", path, "viewer", expect=200)
        self.assertEqual(pol["effective"]["mode"], "notify")
        self.assertEqual(pol["effective"]["sources"]["mode"], "label")

    async def test_unknown_container(self):
        await self.call("GET", f"{H}/containers/missing/autoupdate", "viewer", expect=404)


class TestDeploymentPolicyAndCheck(AutoUpdateCase):
    async def test_deployment_policy(self):
        _, dep = await self.call("POST", "/deployments", "operator", json={
            "name": "app", "type": "container", "strategy": "standalone", "targets": ["local"],
            "spec": {"image": "nginx:1.27"}})
        path = f"/deployments/{dep['id']}/autoupdate"
        await self.call("PUT", path, "operator", json={"mode": "notify"}, expect=200)
        _, pol = await self.call("GET", path, "viewer", expect=200)
        self.assertEqual(pol["effective"]["mode"], "notify")
        _, ov = await self.call("GET", "/autoupdate/overview", "viewer", expect=200)
        self.assertIn("app", [o["name"] for o in ov["objects"]])
        await self.call("DELETE", path, "operator", expect=200)
        await self.call("GET", "/deployments/missing/autoupdate", "viewer", expect=404)

    async def test_a_policy_equal_to_the_defaults_is_not_kept(self):
        # Cycling the Updates knob ends on "off": that saves no row, and an old
        # row equal to the defaults does not show in the overview.
        _, dep = await self.call("POST", "/deployments", "operator", json={
            "name": "knob", "type": "container", "strategy": "standalone", "targets": ["local"],
            "spec": {"image": "nginx:1.27"}})
        path = f"/deployments/{dep['id']}/autoupdate"
        await self.call("PUT", path, "operator", json={"mode": "notify"}, expect=200)
        _, pol = await self.call("PUT", path, "operator", json={"mode": "off", "window": "", "minAge": "", "scanBlock": ""}, expect=200)
        self.assertIsNone(pol["stored"])
        self.assertIsNone(self.svc.store.get_autoupdate_policy("deployment", "", dep["id"]))
        # Off with a window is a real choice and stays.
        _, pol = await self.call("PUT", path, "operator", json={"mode": "off", "window": "02:00-05:00"}, expect=200)
        self.assertEqual(pol["stored"]["window"], "02:00-05:00")
        cid = self.engine.add_container("plain", "nginx:1.27")
        _, pol = await self.call("PUT", f"{H}/containers/{cid}/autoupdate", "operator", json={"mode": "off"}, expect=200)
        self.assertIsNone(pol["stored"])
        self.svc.store.upsert_autoupdate_policy({"kind": "deployment", "hostId": "", "target": dep["id"], "mode": "off",
                                                 "window": "", "minAge": "", "scanBlock": "", "updatedAt": "t", "updatedBy": "x"})
        _, ov = await self.call("GET", "/autoupdate/overview", "viewer", expect=200)
        self.assertNotIn("knob", [o["name"] for o in ov["objects"]])

    async def test_manual_check_notifies_and_records_history(self):
        self.engine.add_image("redis:7")
        self.engine.add_container("cache", "redis:7", labels={"kowan.update": "notify"})
        await self.roles("POST", "/autoupdate/check", "operator")
        _, started = await self.call("POST", "/autoupdate/check", "operator", json={}, expect=202)
        self.assertEqual(started["hosts"], ["local"])
        for _ in range(100):
            _, hist = await self.call("GET", "/autoupdate/history", "viewer")
            if hist["items"]:
                break
            await asyncio.sleep(0.05)
        self.assertEqual(hist["items"][0]["outcome"], "notified")
        self.assertEqual(hist["items"][0]["targetName"], "cache")
        _, filtered = await self.call("GET", "/autoupdate/history", "viewer", params={"outcome": "updated"})
        self.assertEqual(filtered["items"], [])
        _, ov = await self.call("GET", "/autoupdate/overview", "viewer", expect=200)
        cache = next(o for o in ov["objects"] if o["name"] == "cache")
        self.assertTrue(cache["available"])

    async def test_check_scope_errors(self):
        await self.call("POST", "/autoupdate/check", "operator", json={"hostId": "nope"}, expect=404)
        await self.call("POST", "/autoupdate/check", "operator",
                        json={"kind": "deployment", "target": "missing"}, expect=404)


class TestApiEdges(AutoUpdateCase):
    async def test_validation_and_overview_merging(self):
        await self.call("GET", "/autoupdate/history", "viewer", params={"limit": "x"}, expect=400)
        await self.call("PUT", "/autoupdate/settings", "admin", data=b"[1]", expect=400)
        cid = self.engine.add_container("mixed", "nginx:1.27", labels={"kowan.update": "notify"})
        # Stored window + label mode: the overview shows each field's source.
        await self.call("PUT", f"{H}/containers/{cid}/autoupdate", "operator",
                        json={"mode": "apply", "window": "02:00-04:00"}, expect=200)
        host = self.svc.store.get_host("local")
        await self.svc.autoupdater.process_host(host, check_registry=True, only=("container", "mixed"))
        _, ov = await self.call("GET", "/autoupdate/overview", "viewer", expect=200)
        row = next(o for o in ov["objects"] if o["name"] == "mixed")
        self.assertEqual(row["effective"]["mode"], "notify")
        self.assertEqual(row["effective"]["sources"]["mode"], "label")
        self.assertEqual(row["effective"]["window"], "02:00-04:00")
        self.assertTrue(row["window"]["valid"])
        await self.call("PUT", f"{H}/containers/{cid}/autoupdate", "operator", data=b"[1]", expect=400)
        await self.call("PUT", f"{H}/containers/missing/autoupdate", "operator", json={"mode": "notify"},
                        expect=404)
        await self.call("DELETE", f"{H}/containers/missing/autoupdate", "operator", expect=404)
        _, dep = await self.call("POST", "/deployments", "operator", json={
            "name": "d", "type": "container", "strategy": "standalone", "targets": ["local"],
            "spec": {"image": "nginx:1.27"}})
        await self.call("PUT", f"/deployments/{dep['id']}/autoupdate", "operator", json={"mode": "nope"},
                        expect=400)
        await self.call("PUT", "/deployments/missing/autoupdate", "operator", json={"mode": "notify"}, expect=404)
        _, started = await self.call("POST", "/autoupdate/check", "operator", data=b"not json", expect=202)
        self.assertEqual(started["hosts"], ["local"])
        _, started = await self.call("POST", "/autoupdate/check", "operator",
                                     json={"kind": "deployment", "target": dep["id"]}, expect=202)
        self.assertEqual(started["hosts"], ["local"])


if __name__ == "__main__":
    unittest.main()
