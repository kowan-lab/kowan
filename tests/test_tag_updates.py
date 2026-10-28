"""Newer version tags for a deployment: which tags count as newer, the check
per service (the registry is faked), the plan with its diff, and applying it
as a new revision."""

import unittest
from unittest import mock

from kowan.deployment import tagupdates
from kowan.models import ContainerSpec, Deployment
from tests.apicase import ServerCase

COMPOSE = """services:
  api:
    image: ghcr.io/acme/api:2.0   # keep this comment
  web:
    image: nginx:1.27-alpine
  cache:
    image: redis:latest
  db:
    image: postgres@sha256:""" + "a" * 64 + """
  tool:
    image: ${TOOL_IMAGE}
"""

TAGS = {
    "acme/api": ["1.9", "2.0", "2.1", "2.10", "3.0-rc1", "v2.2", "2.1.1", "latest"],
    "library/nginx": ["1.27-alpine", "1.28-alpine", "1.28", "1.29-alpine-slim", "1.26-alpine"],
}


async def fake_tags(store, ref):
    return TAGS[ref.repository]


class TestVersions(unittest.TestCase):
    def test_newer_tags_keep_the_shape(self):
        self.assertEqual(tagupdates.newer_tags("2.0", TAGS["acme/api"]), ["2.10", "2.1"])
        self.assertEqual(tagupdates.newer_tags("1.27-alpine", TAGS["library/nginx"]), ["1.28-alpine"])
        self.assertEqual(tagupdates.newer_tags("v2.1", ["v2.2", "2.3", "v10.0"]), ["v10.0", "v2.2"])
        self.assertEqual(tagupdates.newer_tags("latest", ["1.0"]), [])
        self.assertEqual(tagupdates.newer_tags("2.10", ["2.9", "2.10"]), [])

    def test_with_tag(self):
        self.assertEqual(tagupdates.with_tag("nginx:1.27", "1.28"), "nginx:1.28")
        self.assertEqual(tagupdates.with_tag("host:5000/x/y:1.0", "1.1"), "host:5000/x/y:1.1")
        self.assertEqual(tagupdates.with_tag("host:5000/x", "2"), "host:5000/x:2")


class TestCheckAndPlan(unittest.IsolatedAsyncioTestCase):
    def dep(self):
        return Deployment(id="d1", name="shop", type="compose", strategy="standalone", targets=["local"],
                          compose=COMPOSE)

    async def test_check_per_service(self):
        rows = {r["service"]: r for r in await tagupdates.check(None, self.dep(), list_tags=fake_tags)}
        self.assertEqual((rows["api"]["current"], rows["api"]["latest"], rows["api"]["newer"]), ("2.0", "2.10", ["2.10", "2.1"]))
        self.assertEqual(rows["web"]["newer"], ["1.28-alpine"])
        self.assertIn("not a version tag", rows["cache"]["note"])
        self.assertEqual(rows["db"]["note"], "pinned by digest")
        self.assertIn("variable", rows["tool"]["note"])

    async def test_a_registry_error_is_reported_per_service(self):
        async def broken(store, ref):
            raise ValueError("unauthorized")

        rows = {r["service"]: r for r in await tagupdates.check(None, self.dep(), list_tags=broken)}
        self.assertIn("unauthorized", rows["api"]["error"])
        self.assertEqual(rows["api"]["newer"], [])

    def test_plan_rewrites_only_the_tags(self):
        after, changes, diff = tagupdates.plan(self.dep(), {"api": "2.1", "web": "1.28-alpine"})
        self.assertIn("ghcr.io/acme/api:2.1   # keep this comment", after.compose)
        self.assertIn("nginx:1.28-alpine", after.compose)
        self.assertEqual([(c["service"], c["to"]) for c in changes],
                         [("api", "ghcr.io/acme/api:2.1"), ("web", "nginx:1.28-alpine")])
        self.assertTrue(any(line["op"] == "+" and "api:2.1" in line["text"] for line in diff))
        self.assertEqual(tagupdates.note_of(changes), "new versions: api 2.0 → 2.1, web 1.27-alpine → 1.28-alpine")
        for tags, needle in (({}, "pick at least"), ({"nope": "1"}, "not a service"), ({"api": "bad tag"}, "valid tag"),
                             ({"api": "2.0"}, "already runs")):
            with self.assertRaisesRegex(Exception, needle):
                tagupdates.plan(self.dep(), tags)

    def test_container_deployment(self):
        dep = Deployment(id="d2", name="one", type="container", strategy="standalone", targets=["local"],
                         spec=ContainerSpec(image="nginx:1.27"))
        after, changes, _ = tagupdates.plan(dep, {"": "1.28"})
        self.assertEqual(after.spec.image, "nginx:1.28")
        self.assertEqual(tagupdates.note_of(changes), "new versions: image 1.27 → 1.28")


class TestAPI(ServerCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        p = mock.patch.object(tagupdates, "_list_tags", fake_tags)
        p.start()
        self.addCleanup(p.stop)
        self.dispatched = []

        async def dispatch(dep_id, redeploy=False):
            self.dispatched.append(dep_id)
            return self.svc.store.get_deployment(dep_id)

        q = mock.patch.object(self.svc.deployments, "dispatch_deploy", dispatch)
        q.start()
        self.addCleanup(q.stop)
        _, self.dep = await self.call("POST", "/deployments", "operator", json={
            "name": "one", "type": "container", "strategy": "standalone", "targets": ["local"],
            "spec": {"image": "nginx:1.27-alpine"}}, expect=201)

    async def test_check_preview_apply(self):
        base = f"/deployments/{self.dep['id']}/tag-updates"
        _, out = await self.call("GET", base, "viewer", expect=200)
        self.assertEqual(out["items"][0]["newer"], ["1.28-alpine"])
        await self.roles("POST", base + "/preview", "operator", json={"tags": {"": "1.28-alpine"}})
        _, prev = await self.call("POST", base + "/preview", "operator", json={"tags": {"": "1.28-alpine"}}, expect=200)
        self.assertEqual(prev["changes"][0]["to"], "nginx:1.28-alpine")
        self.assertEqual(self.svc.store.get_deployment(self.dep["id"]).spec.image, "nginx:1.27-alpine")  # untouched
        await self.roles("POST", base, "operator", json={"tags": {"": "1.28-alpine"}})
        _, res = await self.call("POST", base, "operator", json={"tags": {"": "1.28-alpine"}}, expect=200)
        self.assertEqual(self.svc.store.get_deployment(self.dep["id"]).spec.image, "nginx:1.28-alpine")
        self.assertEqual(self.dispatched, [self.dep["id"]])
        _, revs = await self.call("GET", f"/deployments/{self.dep['id']}/revisions", expect=200)
        self.assertEqual((revs[0]["source"], revs[0]["note"]), ("tags", "new versions: image 1.27-alpine → 1.28-alpine"))
        await self.call("POST", base, "operator", json={"tags": {"": "x y"}}, expect=400)
        await self.call("POST", "/deployments/nope/tag-updates", "operator", json={"tags": {"": "1"}}, expect=404)


if __name__ == "__main__":
    unittest.main()
