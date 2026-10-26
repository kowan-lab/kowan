"""Manual container updates through the API on the fake Engine: the update
report, a successful pull + recreate + verify, a crashing new image rolled back
(502), the scan gate blocking a vulnerable image (409), and refusals."""

import json
import unittest
from unittest import mock

from kowan.api import updates
from kowan.scan import trivy
from tests.apicase import ServerCase

H = "/hosts/local"
NEW = "sha256:" + "e" * 64


class UpdateCase(ServerCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.call("PUT", "/autoupdate/settings", "admin", json={"stableSeconds": 0, "verifyTimeoutSeconds": 15},
                        expect=200)
        self.engine.add_image("nginx:1.27")
        self.cid = self.engine.add_container("web", "nginx:1.27", ports=[(80, 8080)])
        self.engine.registry["nginx:1.27"] = NEW

        async def remote(store, ref):
            return NEW

        self._p = mock.patch.object(updates, "_remote_digest", remote)
        self._p.start()
        updates._CACHE.clear() if hasattr(updates._CACHE, "clear") else None

    async def asyncTearDown(self):
        self._p.stop()
        await super().asyncTearDown()

    def image_of(self, name):
        c = self.engine.container(name)
        return next(i for i in self.engine.images.values() if i["Id"] == c["Image"])


class TestUpdate(UpdateCase):
    async def test_report_and_update(self):
        _, rows = await self.call("GET", H + "/updates", "viewer", params={"refresh": "1"}, expect=200)
        row = next(r for r in rows if r["containerName"] == "web")
        self.assertTrue(row["updateAvailable"], row)
        await self.roles("POST", f"{H}/containers/{self.cid}/update", "operator")
        _, res = await self.call("POST", f"{H}/containers/{self.cid}/update", "operator", json={}, expect=200)
        self.assertEqual(res["outcome"], "updated", res)
        self.assertIn(NEW, self.image_of("web")["RepoDigests"][0])
        _, hist = await self.call("GET", "/autoupdate/history", "viewer")
        self.assertEqual(hist["items"][0]["trigger"], "manual")

    async def test_crash_rolls_back(self):
        def crash(c):
            img = next(i for i in self.engine.images.values() if i["Id"] == c["Image"])
            return (1, ["boom"]) if any(NEW in d for d in img["RepoDigests"]) else None

        self.engine.runners.append(("nginx", crash))
        status, res = await self.call("POST", f"{H}/containers/{self.cid}/update", "operator", json={})
        self.assertEqual(status, 502, res)
        self.assertEqual(res["outcome"], "rolled_back")
        self.assertTrue(self.engine.container("web")["State"]["Running"])
        self.assertNotIn(NEW, self.image_of("web")["RepoDigests"][0])

    async def test_scan_gate_blocks(self):
        self.svc.scans.poll = 0.01
        self.engine.add_image(trivy.trivy_image())
        report = {"Results": [{"Target": "x", "Vulnerabilities": [
            {"VulnerabilityID": "CVE-1", "PkgName": "p", "InstalledVersion": "1", "Severity": "CRITICAL"}]}]}
        self.engine.runners.append(("aquasec/trivy", lambda c: (0, [json.dumps(report)])))
        status, res = await self.call("POST", f"{H}/containers/{self.cid}/update", "operator",
                                      json={"scanBlock": "critical"})
        self.assertEqual(status, 409, res)
        self.assertEqual(res["outcome"], "blocked")
        self.assertNotIn(NEW, self.image_of("web")["RepoDigests"][0])
        await self.call("POST", f"{H}/containers/{self.cid}/update", "operator", json={"scanBlock": "sometimes"},
                        expect=400)

    async def test_failed_recreate_rolls_back(self):
        self.engine.fail_once[("POST", r"/containers/create")] = (500, "no space left on device")
        status, res = await self.call("POST", f"{H}/containers/{self.cid}/update", "operator", json={})
        self.assertEqual(status, 502, res)
        self.assertIn(res["outcome"], ("rolled_back", "failed"))
        web = self.engine.container("web")
        self.assertIsNotNone(web)
        self.assertTrue(web["State"]["Running"])

    async def test_crash_loop_and_unstable_are_rolled_back(self):
        def restarting(c):
            img = next(i for i in self.engine.images.values() if i["Id"] == c["Image"])
            if any(NEW in d for d in img["RepoDigests"]):
                c["State"].update(Restarting=True, Status="restarting")
            return None

        self.engine.runners.append(("nginx", restarting))
        status, res = await self.call("POST", f"{H}/containers/{self.cid}/update", "operator", json={})
        self.assertEqual(status, 502, res)
        self.assertIn("restarting", res["error"])

    async def test_refusals(self):
        pinned = self.engine.add_container("pinned", "nginx@sha256:" + "1" * 64)
        await self.call("POST", f"{H}/containers/{pinned}/update", "operator", json={}, expect=400)
        status, _ = await self.call("POST", f"{H}/containers/missing/update", "operator", json={})
        self.assertEqual(status, 502)
        await self.call("GET", "/hosts/nope/updates", "viewer", expect=404)
        _, rows = await self.call("GET", H + "/updates", "viewer", expect=200)
        self.assertEqual(next(r for r in rows if r["containerName"] == "pinned")["error"], "image pinned by digest")


if __name__ == "__main__":
    unittest.main()
