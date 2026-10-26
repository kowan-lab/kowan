"""Deployments end to end: REST -> DeploymentService -> LocalClient/fake Engine
(container deployments) or the fake ``docker`` CLI (compose), with the async
deploy dispatch, status, protection, webhooks and secret resolution."""

import asyncio
import unittest

from tests.apicase import ServerCase
from tests.fakecli import FakeDockerCLI


class DeploymentCase(ServerCase):
    async def asyncSetUp(self):
        self.cli = FakeDockerCLI()
        await super().asyncSetUp()
        self.engine.add_image("nginx:1.27")

    async def asyncTearDown(self):
        await super().asyncTearDown()
        self.cli.close()

    async def create(self, **body):
        base = {"strategy": "standalone", "targets": ["local"]}
        status, dep = await self.call("POST", "/deployments", "operator", json={**base, **body})
        self.assertIn(status, (200, 201), dep)
        return dep

    async def wait_status(self, dep_id, *want, timeout=10.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            _, d = await self.call("GET", f"/deployments/{dep_id}", "operator")
            if d["status"] in want:
                return d
            if asyncio.get_running_loop().time() > deadline:
                self.fail(f"status stayed {d['status']!r}, wanted {want}")
            await asyncio.sleep(0.05)


class TestContainerDeployments(DeploymentCase):
    async def test_full_lifecycle(self):
        dep = await self.create(name="web", type="container", spec={
            "image": "nginx:1.27", "ports": [{"hostPort": 8080, "containerPort": 80, "protocol": "tcp"}],
            "env": [{"key": "MODE", "value": "prod"}]})
        self.assertEqual(dep["status"], "stopped")
        await self.roles("POST", f"/deployments/{dep['id']}/deploy", "operator")
        await self.call("POST", f"/deployments/{dep['id']}/deploy", "operator")
        await self.wait_status(dep["id"], "deployed")
        c = self.engine.container("web")
        self.assertIsNotNone(c)
        self.assertTrue(c["State"]["Running"])
        self.assertEqual(c["Config"]["Labels"].get("kowan.deployment.id"), dep["id"])
        self.assertIn("MODE=prod", c["Config"]["Env"])

        _, status = await self.call("GET", f"/deployments/{dep['id']}/status", "viewer", expect=200)
        self.assertTrue(status)
        _, services = await self.call("GET", f"/deployments/{dep['id']}/services", "viewer", expect=200)
        self.assertEqual(services[0]["state"], "running")

        await self.call("POST", f"/deployments/{dep['id']}/stop", "operator")
        await self.wait_status(dep["id"], "stopped")
        self.assertFalse(self.engine.container("web")["State"]["Running"])
        await self.call("POST", f"/deployments/{dep['id']}/start", "operator")
        await self.wait_status(dep["id"], "deployed")

        # An update and a redeploy recreate the container with the new spec.
        _, got = await self.call("GET", f"/deployments/{dep['id']}", "operator")
        got["spec"]["env"] = [{"key": "MODE", "value": "staging"}]
        await self.call("PUT", f"/deployments/{dep['id']}", "operator", json=got, expect=200)
        await self.call("POST", f"/deployments/{dep['id']}/redeploy", "operator")
        await self.wait_status(dep["id"], "deployed")
        self.assertIn("MODE=staging", self.engine.container("web")["Config"]["Env"])

        await self.call("POST", f"/deployments/{dep['id']}/destroy", "operator")
        await self.wait_status(dep["id"], "stopped")
        self.assertIsNone(self.engine.container("web"))
        await self.call("DELETE", f"/deployments/{dep['id']}", "operator", expect=200)
        await self.call("GET", f"/deployments/{dep['id']}", "operator", expect=404)

    async def test_secrets_resolved_at_deploy(self):
        await self.call("POST", "/credentials", "admin", json={
            "kind": "secret", "name": "db-password", "fields": {"value": "s3cr3t"}}, expect=201)
        dep = await self.create(name="app", type="container", spec={
            "image": "nginx:1.27", "env": [{"key": "DB_PASSWORD", "value": "[[db-password]]"}]})
        await self.call("POST", f"/deployments/{dep['id']}/deploy", "operator")
        await self.wait_status(dep["id"], "deployed", "failed")
        env = self.engine.container("app")["Config"]["Env"]
        self.assertIn("DB_PASSWORD=s3cr3t", env)
        # The stored record still holds the reference, not the value.
        _, stored = await self.call("GET", f"/deployments/{dep['id']}", "operator")
        self.assertEqual(stored["spec"]["env"][0]["value"], "[[db-password]]")

    async def test_protection_blocks_delete(self):
        dep = await self.create(name="keep", type="container", spec={"image": "nginx:1.27"})
        await self.call("PUT", f"/deployments/{dep['id']}/protection", "operator", json={"protected": True}, expect=200)
        await self.call("DELETE", f"/deployments/{dep['id']}", "operator", expect=409)
        await self.call("PUT", f"/deployments/{dep['id']}/protection", "operator", json={"protected": False}, expect=200)
        await self.call("DELETE", f"/deployments/{dep['id']}", "operator", expect=200)

    async def test_enabled_toggle_and_validation(self):
        dep = await self.create(name="toggle", type="container", spec={"image": "nginx:1.27"})
        _, d = await self.call("PUT", f"/deployments/{dep['id']}/enabled", "operator", json={"enabled": False}, expect=200)
        self.assertFalse(d["enabled"])
        for bad in ({"name": "", "type": "container", "spec": {"image": "x"}},
                    {"name": "nohost", "type": "container", "targets": ["missing"], "spec": {"image": "x"}},
                    {"name": "nospec", "type": "container"},
                    {"name": "wrongtype", "type": "helm"}):
            status, _ = await self.call("POST", "/deployments", "operator",
                                        json={"strategy": "standalone", "targets": ["local"], **bad})
            self.assertEqual(status, 400, bad)
        await self.roles("POST", "/deployments", "operator", json={"name": "x", "type": "container"})

    async def test_webhook_redeploys(self):
        dep = await self.create(name="hooked", type="container", spec={"image": "nginx:1.27"})
        _, hook = await self.call("POST", f"/deployments/{dep['id']}/webhook", "operator", expect=200)
        r = await self.client.post("/hooks/deploy/" + hook["token"])
        self.assertIn(r.status, (200, 202))
        await self.wait_status(dep["id"], "deployed")
        r = await self.client.post("/hooks/deploy/not-a-token")
        self.assertEqual(r.status, 404)
        await self.call("DELETE", f"/deployments/{dep['id']}/webhook", "operator", expect=200)
        r = await self.client.post("/hooks/deploy/" + hook["token"])
        self.assertEqual(r.status, 404)


class TestDeploymentApiRules(DeploymentCase):
    async def test_validation_conflicts_and_masking(self):
        base = {"type": "container", "spec": {"image": "nginx:1.27"}}
        for bad in ({"name": "s1", "strategy": "k8s", **base},
                    {"name": "s2", "strategy": "standalone", "targets": [], **base},
                    {"name": "s3", "strategy": "swarm", "targets": ["local", "local2"], **base}):
            await self.call("POST", "/deployments", "operator", json=bad, expect=400)
        # A non-numeric replica count is ignored (replicas only matter for swarm).
        _, s4 = await self.call("POST", "/deployments", "operator", json={
            "name": "s4", "strategy": "standalone", "targets": ["local"], "replicas": "many", **base}, expect=201)
        self.assertEqual(s4["replicas"], 0)
        await self.call("POST", "/deployments", "operator", data=b"[1]", expect=400)
        dep = await self.create(name="taken", type="container", spec={
            "image": "nginx:1.27", "env": [{"key": "PW", "value": "secret-value"}], "extraArgs": ["--cap-add=X"]})
        await self.call("POST", "/deployments", "operator", json={
            "name": "taken", "strategy": "standalone", "targets": ["local"], **base}, expect=409)
        # Names that resolve to the same container/project name collide too.
        await self.call("POST", "/deployments", "operator", json={
            "name": "Taken", "strategy": "standalone", "targets": ["local"], **base}, expect=409)
        other = await self.create(name="other", type="container", spec={"image": "nginx:1.27"})
        _, got = await self.call("GET", f"/deployments/{other['id']}", "operator")
        await self.call("PUT", f"/deployments/{other['id']}", "operator", json={**got, "name": "taken"}, expect=409)
        await self.call("PUT", f"/deployments/{other['id']}", "operator", data=b"[1]", expect=400)
        # Viewers never see env values or extra args.
        _, seen = await self.call("GET", f"/deployments/{dep['id']}", "viewer", expect=200)
        self.assertNotIn("secret-value", str(seen))
        self.assertNotIn("--cap-add=X", str(seen))
        _, listing = await self.call("GET", "/deployments", "viewer", expect=200)
        self.assertNotIn("secret-value", str(listing))
        await self.call("PUT", f"/deployments/{dep['id']}/enabled", "operator", data=b"[1]", expect=400)
        await self.call("PUT", f"/deployments/{dep['id']}/protection", "operator", data=b"[1]", expect=400)

    async def test_git_webhook_secret_and_sync(self):
        dep = await self.create(name="gitapp", type="compose", git={
            "repoUrl": "https://user:pw@git.example.com/a/b.git", "ref": "main", "path": "compose.yml"})
        _, seen = await self.call("GET", f"/deployments/{dep['id']}", "viewer", expect=200)
        self.assertNotIn("pw@", seen["git"]["repoUrl"])
        _, hook = await self.call("POST", f"/deployments/{dep['id']}/webhook", "operator", expect=200)
        self.assertTrue(hook.get("secret") or hook.get("token"))
        # Sync reaches out to the (unreachable) repository and reports it.
        status, body = await self.call("POST", f"/deployments/{dep['id']}/sync", "operator")
        self.assertIn(status, (400, 502), body)


class TestComposeDeployments(DeploymentCase):
    COMPOSE = "services:\n  web:\n    image: nginx:1.27\n    ports: ['8080:80']\n"

    async def test_compose_up_and_down(self):
        dep = await self.create(name="shop", type="compose", compose=self.COMPOSE)
        await self.call("POST", f"/deployments/{dep['id']}/deploy", "operator")
        await self.wait_status(dep["id"], "deployed")
        up = [c for c in self.cli.calls() if "up" in c["argv"]]
        self.assertTrue(up, self.cli.calls())
        argv = up[-1]["argv"]
        self.assertEqual(argv[:3], ["compose", "-p", "shop"])
        compose_text = next(iter(up[-1]["files"].values()))
        self.assertIn("nginx:1.27", compose_text)

        await self.call("POST", f"/deployments/{dep['id']}/destroy", "operator")
        await self.wait_status(dep["id"], "stopped")
        self.assertTrue([c for c in self.cli.calls() if c["argv"][:4] == ["compose", "-p", "shop", "down"]])

    async def test_failed_deploy_reports_the_error(self):
        dep = await self.create(name="broken", type="compose", compose=self.COMPOSE)
        # The compose check passes; the daemon refuses at "up" time.
        self.cli.fail_with(1, "Error response from daemon: port is already allocated", only="up")
        await self.call("POST", f"/deployments/{dep['id']}/deploy", "operator")
        await self.wait_status(dep["id"], "error")

    async def test_invalid_compose_is_refused_before_deploying(self):
        dep = await self.create(name="checked", type="compose", compose=self.COMPOSE)
        self.cli.fail_with(1, "service web: invalid port")
        status, body = await self.call("POST", f"/deployments/{dep['id']}/deploy", "operator")
        self.assertEqual(status, 400, body)
        self.assertIn("invalid port", body["error"])

    async def test_secrets_in_compose_resolve_only_on_the_wire(self):
        await self.call("POST", "/credentials", "admin", json={
            "kind": "secret", "name": "api-key", "fields": {"value": "k-123"}}, expect=201)
        compose = "services:\n  api:\n    image: nginx:1.27\n    environment:\n      KEY: '[[api-key]]'\n"
        dep = await self.create(name="api", type="compose", compose=compose)
        await self.call("POST", f"/deployments/{dep['id']}/deploy", "operator")
        await self.wait_status(dep["id"], "deployed")
        sent = next(iter([c for c in self.cli.calls() if "up" in c["argv"]][-1]["files"].values()))
        self.assertIn("k-123", sent)
        _, stored = await self.call("GET", f"/deployments/{dep['id']}", "operator")
        self.assertIn("[[api-key]]", stored["compose"])


if __name__ == "__main__":
    unittest.main()
