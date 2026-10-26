"""Deployments when things go wrong: the daemon failing list/create/stop/remove,
the compose CLI failing on down, an unreachable edge target, swarm removal
failing, and the reconcile pass and status views reporting all of it instead of
crashing."""

import asyncio
import unittest

from tests.test_api_deployments import DeploymentCase


class TestDaemonFailures(DeploymentCase):
    async def deployed(self, **body):
        dep = await self.create(**body)
        await self.call("POST", f"/deployments/{dep['id']}/deploy", "operator")
        await self.wait_status(dep["id"], "deployed")
        return dep

    async def test_container_deployment_failures(self):
        dep = await self.deployed(name="fragile", type="container", spec={
            "image": "nginx:1.27", "entrypoint": ["/bin/sh"], "command": ["-c", "echo [[nope]]"]})
        # Listing fails: status says so, services/logs/stop/start/destroy report errors.
        self.engine.fail[("GET", r"/containers/json")] = (500, "daemon is sick")
        _, status = await self.call("GET", f"/deployments/{dep['id']}/status", "viewer", expect=200)
        self.assertEqual(status[0]["state"], "not_deployed")
        self.assertIn("sick", status[0]["message"])
        status, _ = await self.call("GET", f"/deployments/{dep['id']}/services", "viewer")
        self.assertEqual(status, 400)
        await self.call("POST", f"/deployments/{dep['id']}/stop", "operator")
        await self.wait_status(dep["id"], "error")
        self.assertEqual(await self.svc.deployments.reconcile_once(), 0)  # only "deployed" ones are maintained
        del self.engine.fail[("GET", r"/containers/json")]

        # Removing fails during destroy: the record is still marked stopped (torn down).
        self.engine.fail[("DELETE", r"/containers/.+")] = (500, "cannot remove")
        await self.call("POST", f"/deployments/{dep['id']}/destroy", "operator")
        await self.wait_status(dep["id"], "stopped")
        self.assertIsNotNone(self.engine.container("fragile"))
        del self.engine.fail[("DELETE", r"/containers/.+")]
        # Create fails on redeploy.
        self.engine.fail[("POST", r"/containers/create")] = (500, "no space left on device")
        await self.call("POST", f"/deployments/{dep['id']}/redeploy", "operator")
        await self.wait_status(dep["id"], "error")
        _, status = await self.call("GET", f"/deployments/{dep['id']}/status", "viewer", expect=200)
        self.assertIn("no space left", status[0]["message"])
        del self.engine.fail[("POST", r"/containers/create")]

    async def test_compose_down_failure_and_logs_without_container(self):
        dep = await self.deployed(name="cmp", type="compose",
                                  compose="services:\n  web:\n    image: nginx:1.27\n")
        status, _ = await self.call("GET", f"/deployments/{dep['id']}/services", "viewer")
        self.assertEqual(status, 200)
        ws = await self.client.ws_connect(f"/api/v1/deployments/{dep['id']}/logs", headers=self.h["viewer"])
        msg = await ws.receive()
        self.assertIn("no managed container", msg.data)
        self.cli.fail_with(1, "permission denied", only="down")
        await self.call("POST", f"/deployments/{dep['id']}/stop", "operator")
        await self.wait_status(dep["id"], "error")
        await self.call("POST", f"/deployments/{dep['id']}/destroy", "operator")
        await self.wait_status(dep["id"], "stopped")

    async def test_unreachable_edge_target(self):
        _, created = await self.call("POST", "/hosts", "admin", json={"name": "far", "mode": "edge"}, expect=201)
        far = created["host"]["id"]
        dep = await self.create(name="faraway", type="container", targets=[far], spec={"image": "nginx:1.27"})
        await self.call("POST", f"/deployments/{dep['id']}/deploy", "operator")
        await self.wait_status(dep["id"], "error")
        _, status = await self.call("GET", f"/deployments/{dep['id']}/status", "viewer", expect=200)
        self.assertEqual(status[0]["state"], "not_deployed")
        self.assertTrue(status[0]["message"])
        # The host disappears: status still answers.
        await self.call("DELETE", f"/hosts/{far}", "admin", expect=200)
        _, status = await self.call("GET", f"/deployments/{dep['id']}/status", "viewer", expect=200)
        self.assertTrue(status[0]["message"])

    async def test_swarm_failures(self):
        self.engine.swarm = True
        dep = await self.deployed(name="stk", type="compose", strategy="swarm",
                                  compose="services:\n  web:\n    image: nginx:1.27\n")
        self.engine.fail[("GET", r"/containers/json")] = (500, "boom")
        _, status = await self.call("GET", f"/deployments/{dep['id']}/status", "viewer", expect=200)
        self.assertEqual(status[0]["state"], "active")
        del self.engine.fail[("GET", r"/containers/json")]
        self.cli.fail_with(1, "stack not found", only="rm")
        await self.call("POST", f"/deployments/{dep['id']}/stop", "operator")
        await self.wait_status(dep["id"], "error")
        self.cli.fail_with(1, "invalid compose", only="deploy")
        await self.call("POST", f"/deployments/{dep['id']}/start", "operator")
        await self.wait_status(dep["id"], "error")
        self.engine.fail[("GET", r"/info")] = (500, "daemon down")
        _, status = await self.call("GET", f"/deployments/{dep['id']}/status", "viewer", expect=200)
        self.assertEqual(status[0]["state"], "inactive")

    async def test_reconcile_loop_runs_and_stops(self):
        task = asyncio.ensure_future(self.svc.deployments.run_reconcile_loop(interval=0.01))
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task


if __name__ == "__main__":
    unittest.main()
