"""Auto-update of whole deployments, end to end on the fake Engine: the registry
has a newer image for the deployment's tag, and the runner notifies, pulls, waits
for its window / minimum age, applies (redeploy + verify) or rolls back when the
new image crashes, remembering bad versions. Also the containers' own pass and
the periodic tick."""

import asyncio
import unittest
from datetime import datetime

from tests.apicase import ServerCase

NEW = "sha256:" + "f" * 64


class DeploymentUpdateCase(ServerCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.engine.add_image("nginx:1.27")
        au = self.svc.autoupdater

        async def remote(ref, refresh):
            return NEW, ""

        au._remote_digest = remote
        await self.call("PUT", "/autoupdate/settings", "admin",
                        json={"stableSeconds": 0, "verifyTimeoutSeconds": 15}, expect=200)
        _, dep = await self.call("POST", "/deployments", "operator", json={
            "name": "app", "type": "container", "strategy": "standalone", "targets": ["local"],
            "spec": {"image": "nginx:1.27", "ports": [{"hostPort": 8080, "containerPort": 80}]}})
        self.dep = dep
        await self.call("POST", f"/deployments/{dep['id']}/deploy", "operator")
        await self.wait_deployed()
        self.engine.registry["nginx:1.27"] = NEW

    async def wait_deployed(self):
        for _ in range(200):
            _, d = await self.call("GET", f"/deployments/{self.dep['id']}", "operator")
            if d["status"] not in ("pending", "deploying"):
                return d
            await asyncio.sleep(0.05)
        self.fail("deploy did not finish")

    async def policy(self, **body):
        await self.call("PUT", f"/deployments/{self.dep['id']}/autoupdate", "operator", json=body, expect=200)

    async def run_pass(self):
        host = self.svc.store.get_host("local")
        res = await self.svc.autoupdater.process_host(host, check_registry=True, only=("deployment", self.dep["id"]),
                                                     trigger="manual")
        return res[0]

    def image_of_app(self):
        c = self.engine.container("app")
        return next(i for i in self.engine.images.values() if i["Id"] == c["Image"])


class TestModes(DeploymentUpdateCase):
    async def test_notify_once_per_version(self):
        await self.policy(mode="notify")
        self.assertEqual((await self.run_pass())["result"], "available")
        self.assertEqual((await self.run_pass())["result"], "available")
        _, hist = await self.call("GET", "/autoupdate/history", "viewer")
        self.assertEqual([h["outcome"] for h in hist["items"]], ["notified"])

    async def test_pull_does_not_redeploy(self):
        await self.policy(mode="pull")
        before = self.engine.container("app")["Image"]
        self.assertEqual((await self.run_pass())["result"], "pulled")
        self.assertEqual(self.engine.container("app")["Image"], before)
        self.assertTrue(any(NEW in d for i in self.engine.images.values() for d in i["RepoDigests"]))

    async def test_apply_redeploys_and_verifies(self):
        await self.policy(mode="apply")
        res = await self.run_pass()
        self.assertEqual(res["result"], "updated", res)
        self.assertIn(NEW, self.image_of_app()["RepoDigests"][0])
        # Now up to date.
        self.assertEqual((await self.run_pass())["result"], "up_to_date")
        _, pol = await self.call("GET", f"/deployments/{self.dep['id']}/autoupdate", "viewer", expect=200)
        self.assertEqual(pol["state"]["lastResult"], "up_to_date")

    async def test_closed_window_and_min_age_wait(self):
        h = (datetime.now().hour + 2) % 24  # a window that opens in two hours
        await self.policy(mode="apply", window=f"{h:02d}:00-{(h + 1) % 24:02d}:00")
        res = await self.run_pass()
        self.assertEqual(res["result"], "waiting")
        await self.policy(mode="apply", minAge="30d")
        self.assertEqual((await self.run_pass())["result"], "waiting")

    async def test_crashing_new_image_rolls_back_and_is_remembered(self):
        await self.policy(mode="apply")
        old_image = self.engine.container("app")["Image"]

        def crash_if_new(c):
            img = next(i for i in self.engine.images.values() if i["Id"] == c["Image"])
            if any(NEW in d for d in img["RepoDigests"]):
                return 1, ["panic: new version is broken"]
            return None  # the old image keeps running

        self.engine.runners.append(("nginx", crash_if_new))
        res = await self.run_pass()
        self.assertEqual(res["result"], "rolled_back", res)
        self.assertEqual(self.engine.container("app")["Image"], old_image)
        self.assertTrue(self.engine.container("app")["State"]["Running"])
        # The same version is not tried again.
        self.assertEqual((await self.run_pass())["result"], "skipped")


class TestGatesAndFailures(DeploymentUpdateCase):
    def trivy(self, severity):
        import json as _json
        from kowan.scan import trivy as _trivy

        self.svc.scans.poll = 0.01
        self.engine.add_image(_trivy.trivy_image())
        report = {"Results": [{"Target": "x", "Vulnerabilities": [
            {"VulnerabilityID": "CVE-9", "PkgName": "p", "InstalledVersion": "1", "Severity": severity}]}]}
        self.engine.runners.append(("aquasec/trivy", lambda c: (0, [_json.dumps(report)])))

    async def test_scan_gate_blocks_deployment(self):
        self.trivy("CRITICAL")
        await self.policy(mode="apply", scanBlock="critical")
        res = await self.run_pass()
        self.assertEqual(res["result"], "blocked", res)
        self.assertNotIn(NEW, self.image_of_app()["RepoDigests"][0])

    async def test_scan_gate_worse_than_current(self):
        self.trivy("HIGH")
        await self.policy(mode="apply", scanBlock="worse")
        res = await self.run_pass()
        # Same findings before and after: not worse, the update goes ahead.
        self.assertEqual(res["result"], "updated", res)

    async def test_pull_failures(self):
        self.engine.fail[("POST", r"/images/create")] = (500, "registry unreachable")
        await self.policy(mode="pull")
        self.assertEqual((await self.run_pass())["result"], "error")
        await self.policy(mode="apply")
        self.assertEqual((await self.run_pass())["result"], "failed")
        del self.engine.fail[("POST", r"/images/create")]

    async def test_registry_errors_are_reported(self):
        async def broken(ref, refresh):
            return "", "registry said no"

        self.svc.autoupdater._remote_digest = broken
        await self.policy(mode="notify")
        res = await self.run_pass()
        self.assertEqual(res["result"], "error")
        _, pol = await self.call("GET", f"/deployments/{self.dep['id']}/autoupdate", "viewer", expect=200)
        self.assertIn("registry said no", pol["state"]["lastMessage"])


class TestContainerOutcomes(ServerCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()

        async def remote(ref, refresh):
            return NEW, ""

        self.svc.autoupdater._remote_digest = remote
        await self.call("PUT", "/autoupdate/settings", "admin",
                        json={"stableSeconds": 0, "verifyTimeoutSeconds": 15}, expect=200)

    async def pass_for(self, name):
        host = self.svc.store.get_host("local")
        res = await self.svc.autoupdater.process_host(host, check_registry=True, only=("container", name),
                                                     trigger="manual")
        return res[0]["result"]

    async def test_unsupported_up_to_date_pull_and_failures(self):
        self.engine.add_container("pinned", "nginx@sha256:" + "1" * 64, labels={"kowan.update": "notify"})
        self.assertEqual(await self.pass_for("pinned"), "unsupported")
        local = self.engine.add_container("built", "myapp:dev", labels={"kowan.update": "notify"})
        img = next(i for i in self.engine.images.values() if i["Id"] == self.engine.containers[local]["Image"])
        img["RepoDigests"] = []  # built locally, never pulled
        self.assertEqual(await self.pass_for("built"), "unsupported")

        self.engine.add_image("redis:7")
        cur = next(i for i in self.engine.images.values() if "redis:7" in i["RepoTags"])
        cur["RepoDigests"] = ["redis@" + NEW]
        self.engine.add_container("current", "redis:7", labels={"kowan.update": "apply"})
        self.assertEqual(await self.pass_for("current"), "up_to_date")

        self.engine.add_image("memcached:1")
        self.engine.registry["memcached:1"] = NEW
        self.engine.add_container("cache", "memcached:1", labels={"kowan.update": "pull"})
        self.assertEqual(await self.pass_for("cache"), "pulled")
        self.engine.fail[("POST", r"/images/create")] = (500, "nope")
        self.engine.add_image("memcached:2")
        self.engine.add_container("cache2", "memcached:2", labels={"kowan.update": "pull"})
        self.assertEqual(await self.pass_for("cache2"), "error")
        del self.engine.fail[("POST", r"/images/create")]

        async def broken(ref, refresh):
            return "", "timeout"

        self.svc.autoupdater._remote_digest = broken
        self.engine.add_image("mongo:7")
        self.engine.add_container("db", "mongo:7", labels={"kowan.update": "notify"})
        self.assertEqual(await self.pass_for("db"), "error")


class TestContainersAndTick(ServerCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()

        async def remote(ref, refresh):
            return NEW, ""

        self.svc.autoupdater._remote_digest = remote
        await self.call("PUT", "/autoupdate/settings", "admin",
                        json={"stableSeconds": 0, "verifyTimeoutSeconds": 15}, expect=200)
        self.engine.add_image("redis:7")
        self.engine.registry["redis:7"] = NEW

    async def test_apply_label_updates_a_plain_container(self):
        self.engine.add_container("cache", "redis:7", labels={"kowan.update": "apply"})
        host = self.svc.store.get_host("local")
        res = await self.svc.autoupdater.process_host(host, check_registry=True, trigger="manual")
        self.assertEqual(res[0]["result"], "updated", res)
        img = next(i for i in self.engine.images.values() if i["Id"] == self.engine.container("cache")["Image"])
        self.assertIn(NEW, img["RepoDigests"][0])

    async def test_tick_once_checks_due_hosts(self):
        self.engine.add_container("cache", "redis:7", labels={"kowan.update": "notify"})
        au = self.svc.autoupdater
        au._next_check["local"] = 0  # due now
        await au.tick_once()
        _, hist = await self.call("GET", "/autoupdate/history", "viewer")
        self.assertEqual(hist["items"][0]["outcome"], "notified")
        self.assertIsNotNone(au.next_check_at("local"))
        # Disabled: nothing happens.
        await self.call("PUT", "/autoupdate/settings", "admin", json={"enabled": False}, expect=200)
        au._next_check["local"] = 0
        await au.tick_once()
        _, hist2 = await self.call("GET", "/autoupdate/history", "viewer")
        self.assertEqual(len(hist2["items"]), 1)


if __name__ == "__main__":
    unittest.main()


class TestUpdateNow(DeploymentUpdateCase):
    """The workload's Update button: no policy needed, one redeploy."""

    async def test_update_deployment_now(self):
        url = f"/deployments/{self.dep['id']}/update"
        await self.roles("POST", url, "operator")
        _, res = await self.call("POST", url, "operator", expect=200)
        self.assertEqual(res["outcome"], "updated", res)
        self.assertIn(NEW, self.image_of_app()["RepoDigests"][0])
        _, res = await self.call("POST", url, "operator", expect=200)
        self.assertEqual(res["outcome"], "up_to_date", res)
        await self.call("POST", "/deployments/nope/update", "operator", expect=404)

    async def test_retries_a_version_rolled_back_before(self):
        def crash_if_new(c):
            img = next(i for i in self.engine.images.values() if i["Id"] == c["Image"])
            return (1, ["panic"]) if any(NEW in d for d in img["RepoDigests"]) else None

        self.engine.runners.append(("nginx", crash_if_new))
        status, res = await self.call("POST", f"/deployments/{self.dep['id']}/update", "operator")
        self.assertEqual((status, res["outcome"]), (502, "rolled_back"), res)
        self.engine.runners.clear()
        _, res = await self.call("POST", f"/deployments/{self.dep['id']}/update", "operator", expect=200)
        self.assertEqual(res["outcome"], "updated", res)

    async def test_container_update_goes_through_its_deployment(self):
        cid = self.engine.container("app")["Id"]
        _, res = await self.call("POST", f"/hosts/local/containers/{cid}/update", "operator", json={}, expect=200)
        self.assertEqual((res["deploymentId"], res["outcome"]), (self.dep["id"], "updated"), res)
        self.assertIn(NEW, self.image_of_app()["RepoDigests"][0])


class TestUpdateStack(DeploymentUpdateCase):
    async def test_update_an_unmanaged_stack(self):
        from unittest import mock

        from kowan.api import updates

        labels = {"com.docker.compose.project": "mon", "com.docker.compose.service": "web"}
        self.engine.add_container("mon-web-1", "nginx:1.27", labels=labels)
        self.engine.add_container("mon-db-1", "redis:7", labels={**labels, "com.docker.compose.service": "db"})

        async def remote(store, ref, plain_http=False):
            return NEW if ref.repository == "library/nginx" else "sha256:" + "0" * 64

        with mock.patch.object(updates, "_remote_digest", remote):
            await self.roles("POST", "/hosts/local/stacks/mon/update", "operator")
            _, res = await self.call("POST", "/hosts/local/stacks/mon/update", "operator", expect=200)
            self.assertEqual(res["updated"], 1, res)
            self.assertEqual({r["name"]: r["outcome"] for r in res["results"]}["mon-web-1"], "updated")
            # A Kowan deployment's project is refused: its deployment is updated instead.
            status, _ = await self.call("POST", "/hosts/local/stacks/app/update", "operator")
        self.assertIn(status, (200, 409))
