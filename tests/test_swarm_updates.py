"""Image updates of swarm services and plain-HTTP registries.

* A swarm task runs ``repo:tag@sha256:...``: the digest is the one swarm
  resolved for the tag at deploy time, so the task has an update when the tag
  now serves another digest. A digest written by hand (no tag) stays pinned.
* Updating a swarm task updates its service (``docker service update
  --image``), once per service, and never through an old task kept as history.
* A registry the host's daemon lists in insecure-registries, or a stored one
  with an ``http://`` URL, is asked over HTTP when HTTPS fails.
* ``LocalClient`` reports each container's image id, the daemon's insecure
  registries, and points a service at a new image.
"""

import asyncio
import json
import unittest
from unittest import mock

from aiohttp import web

from kowan.api import updates
from kowan.api.updates import DigestCache, has_tag, registry_insecure
from kowan.docker.local import LocalClient
from kowan.models import Container
from kowan.triggers.dockerimage import _new_docker_image
from kowan.web import SERVICES_KEY
from tests.fakeengine import FakeEngine

OLD = "sha256:" + "1" * 64
NEW = "sha256:" + "2" * 64


def _task(cid, service="svc-web", name="shop_web", image=f"reg.lan:5000/web:1@{OLD}", state="running"):
    return Container(id=cid, names=[f"/{name}.1.{cid}"], image=image, state=state, labels={
        "com.docker.swarm.service.id": service, "com.docker.swarm.service.name": name,
        "com.docker.stack.namespace": "shop"})


class _Ops:
    def __init__(self, containers, insecure=()):
        self.containers = containers
        self.insecure = list(insecure)
        self.service_updates = []

    async def list_containers(self, all):
        return self.containers

    async def system_info(self):
        return mock.Mock(insecure_registries=self.insecure)

    async def inspect_container(self, cid):
        c = next(c for c in self.containers if c.id == cid)
        return {"Image": "img", "Name": c.names[0], "Config": {"Image": c.image, "Labels": c.labels}}

    async def inspect_image(self, image_id):
        return {"RepoDigests": []}

    async def update_service_image(self, stack, service_id, image, auth):
        self.service_updates.append((stack, service_id, image))


class TestHelpers(unittest.TestCase):
    def test_has_tag(self):
        self.assertTrue(has_tag(f"nginx:1.27@{OLD}"))
        self.assertTrue(has_tag("reg.lan:5000/web:1"))
        self.assertFalse(has_tag(f"nginx@{OLD}"))
        self.assertFalse(has_tag(f"reg.lan:5000/web@{OLD}"))

    def test_registry_insecure(self):
        entries = ["reg.lan:5000", "10.175.0.0/16", "127.0.0.0/8"]
        self.assertTrue(registry_insecure("reg.lan:5000", entries))
        self.assertTrue(registry_insecure("10.175.101.30:5000", entries))
        self.assertTrue(registry_insecure("localhost:5000", entries))
        self.assertFalse(registry_insecure("reg.lan:5001", entries))
        self.assertFalse(registry_insecure("10.176.0.1:5000", entries))
        self.assertFalse(registry_insecure("ghcr.io", entries))
        self.assertFalse(registry_insecure("10.175.0.1:5000", ["not-a-cidr/x"]))


class TestSwarmCheck(unittest.TestCase):
    def rows(self, containers, remote=NEW, insecure=()):
        seen = []

        async def fake_remote(store, ref, plain_http=False):
            seen.append((ref.key, plain_http))
            return remote

        async def run():
            with mock.patch.object(updates, "_remote_digest", fake_remote):
                return await updates.check_host(mock.Mock(store=None), _Ops(containers, insecure), cache=DigestCache())

        return {r["containerId"]: r for r in asyncio.run(run())}, seen

    def test_a_task_behind_its_tag_has_an_update(self):
        rows, seen = self.rows([_task("a")], insecure=["reg.lan:5000"])
        self.assertTrue(rows["a"]["updateAvailable"])
        self.assertEqual((rows["a"]["localDigest"], rows["a"]["remoteDigest"]), (OLD, NEW))
        self.assertNotIn("error", rows["a"])
        # The daemon pulls from reg.lan:5000 over HTTP: the check may too.
        self.assertEqual(seen, [("reg.lan:5000/web:1", True)])

    def test_a_task_on_the_tag_digest_is_up_to_date(self):
        rows, _ = self.rows([_task("a")], remote=OLD)
        self.assertFalse(rows["a"]["updateAvailable"])
        self.assertNotIn("error", rows["a"])

    def test_a_hand_pinned_digest_stays_pinned(self):
        lone = Container(id="b", names=["/lone"], image=f"reg.lan:5000/web:1@{OLD}")
        rows, seen = self.rows([_task("a", image=f"reg.lan:5000/web@{OLD}"), lone])
        for cid in ("a", "b"):
            self.assertFalse(rows[cid]["updateAvailable"])
            self.assertEqual(rows[cid]["error"], "image pinned by digest")
        self.assertEqual(seen, [])


class TestSwarmUpdate(unittest.IsolatedAsyncioTestCase):
    def svc(self):
        svc = mock.Mock()
        svc.store.get_autoupdate_state.return_value = {}
        return svc

    async def test_updates_the_service_to_the_tag_digest(self):
        ops = _Ops([_task("a")])

        async def fake_remote(store, ref, plain_http=False):
            return NEW

        with mock.patch.object(updates, "_remote_digest", fake_remote), \
                mock.patch("kowan.registry.docker_auth_for", mock.AsyncMock(return_value="")):
            payload, status = await updates.update_container_now(
                self.svc(), "local", ops, "a", await ops.inspect_container("a"))
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["outcome"], "updated")
        self.assertEqual(ops.service_updates, [("shop", "svc-web", f"reg.lan:5000/web:1@{NEW}")])

    async def test_already_current(self):
        ops = _Ops([_task("a")])

        async def fake_remote(store, ref, plain_http=False):
            return OLD

        with mock.patch.object(updates, "_remote_digest", fake_remote):
            payload, status = await updates.update_container_now(
                self.svc(), "local", ops, "a", await ops.inspect_container("a"))
        self.assertEqual((status, payload["outcome"]), (200, "current"))
        self.assertEqual(ops.service_updates, [])

    async def test_a_stack_updates_each_service_once_and_skips_old_tasks(self):
        containers = [
            _task("a1"), _task("a2"),                                   # two replicas of web
            _task("b1", service="svc-api", name="shop_api", state="exited"),  # old api task...
            _task("b2", service="svc-api", name="shop_api", image=f"reg.lan:5000/web:1@{NEW}"),  # ...its live one is current
        ]
        ops = _Ops(containers)
        svc = self.svc()
        svc.store.get_host.return_value = mock.Mock(id="local")
        svc.docker.ops.return_value = ops
        app = web.Application()
        app[SERVICES_KEY] = svc
        request = mock.Mock(app=app, match_info={"hostID": "local", "project": "shop"})

        async def fake_remote(store, ref, plain_http=False):
            return NEW

        with mock.patch.object(updates, "_remote_digest", fake_remote), \
                mock.patch.object(updates, "_owning_deployment", return_value=None), \
                mock.patch("kowan.registry.docker_auth_for", mock.AsyncMock(return_value="")):
            resp = await updates._update_stack(request)
        body = json.loads(resp.text)
        self.assertEqual(resp.status, 200, body)
        self.assertEqual(body["updated"], 1)
        self.assertEqual(ops.service_updates, [("shop", "svc-web", f"reg.lan:5000/web:1@{NEW}")])


class TestPlainHttpRegistry(unittest.IsolatedAsyncioTestCase):
    """The digest resolver against a registry that speaks only HTTP."""

    async def asyncSetUp(self):
        async def manifest(request):
            return web.Response(headers={"Docker-Content-Digest": NEW})

        app = web.Application()
        app.router.add_route("HEAD", "/v2/{repo:.+}/manifests/{tag}", manifest)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.host = f"127.0.0.1:{site._server.sockets[0].getsockname()[1]}"

    async def asyncTearDown(self):
        await self.runner.cleanup()

    async def test_falls_back_to_http_only_when_allowed(self):
        cfg = {"registry": self.host, "repository": "web", "tag": "1"}
        _, digest = await _new_docker_image({**cfg, "plainHttp": "true"}).evaluate({}, "")
        self.assertEqual(digest, NEW)
        with self.assertRaises(ValueError):
            await _new_docker_image(cfg).evaluate({}, "")


class TestLocalClient(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = FakeEngine()
        self.client = LocalClient(await self.engine.start())

    async def asyncTearDown(self):
        await self.client.close()
        await self.engine.stop()

    async def test_image_id_and_insecure_registries(self):
        cid = self.engine.add_container("web")
        [c] = await self.client.list_containers(True)
        self.assertEqual(c.image_id, self.engine.containers[cid]["Image"])
        self.assertTrue(c.image_id.startswith("sha256:"))
        self.engine.registry_config = {
            "IndexConfigs": {"docker.io": {"Secure": True}, "reg.lan:5000": {"Secure": False}},
            "InsecureRegistryCIDRs": ["127.0.0.0/8"]}
        info = await self.client.system_info()
        self.assertEqual(info.insecure_registries, ["reg.lan:5000", "127.0.0.0/8"])

    async def test_update_service_image(self):
        self.engine.services["svc1"] = {"ID": "svc1", "Version": {"Index": 7}, "Spec": {
            "Name": "shop_web", "TaskTemplate": {"ContainerSpec": {"Image": f"web:1@{OLD}", "Env": ["A=1"]}}}}
        await self.client.update_service_image("shop", "svc1", f"web:1@{NEW}", "dG9rZW4=")
        svc = self.engine.services["svc1"]
        self.assertEqual(svc["Version"]["Index"], 8)
        self.assertEqual(svc["Spec"]["TaskTemplate"]["ContainerSpec"], {"Image": f"web:1@{NEW}", "Env": ["A=1"]})
        self.assertEqual(svc["Spec"]["Name"], "shop_web")
        self.assertEqual(self.engine.service_auth["svc1"], "dG9rZW4=")


if __name__ == "__main__":
    unittest.main()
