"""More of the deployment service on the fake Engine + fake docker CLI: swarm
stacks (container and compose types, inactive swarm), compose stop/start and
per-service status, the logs WebSocket, and the reconcile pass that brings a
deployed-but-down deployment back."""

import asyncio
import unittest

from tests.test_api_deployments import DeploymentCase


class TestSwarm(DeploymentCase):
    async def test_container_and_compose_stacks(self):
        self.engine.swarm = True
        dep = await self.create(name="stacky", type="container", strategy="swarm", spec={
            "image": "nginx:1.27", "env": [{"key": "A", "value": "1"}]})
        await self.call("POST", f"/deployments/{dep['id']}/deploy", "operator")
        await self.wait_status(dep["id"], "deployed")
        deploy = [c for c in self.cli.calls() if c["argv"][:2] == ["stack", "deploy"]]
        self.assertTrue(deploy, self.cli.calls())
        compose = next(iter(deploy[-1]["files"].values()))
        self.assertIn("nginx:1.27", compose)
        self.assertIn("A", compose)
        _, status = await self.call("GET", f"/deployments/{dep['id']}/status", "viewer", expect=200)
        self.assertEqual(status[0]["state"], "not_deployed")  # no task container yet
        # A swarm task container shows up as running.
        self.engine.add_container("stacky_web.1", labels={"com.docker.stack.namespace": "stacky",
                                                          "com.docker.compose.project": "stacky"})
        _, status = await self.call("GET", f"/deployments/{dep['id']}/status", "viewer", expect=200)
        self.assertEqual(status[0]["state"], "running")
        await self.call("POST", f"/deployments/{dep['id']}/destroy", "operator")
        await self.wait_status(dep["id"], "stopped")
        self.assertTrue([c for c in self.cli.calls() if c["argv"][:2] == ["stack", "rm"]])

        comp = await self.create(name="cstack", type="compose", strategy="swarm",
                                 compose="services:\n  web:\n    image: nginx:1.27\n")
        await self.call("POST", f"/deployments/{comp['id']}/deploy", "operator")
        await self.wait_status(comp["id"], "deployed")

    async def test_inactive_swarm_is_an_error(self):
        dep = await self.create(name="nostack", type="container", strategy="swarm", spec={"image": "nginx:1.27"})
        await self.call("POST", f"/deployments/{dep['id']}/deploy", "operator")
        await self.wait_status(dep["id"], "error")
        _, status = await self.call("GET", f"/deployments/{dep['id']}/status", "viewer", expect=200)
        self.assertEqual(status[0]["state"], "inactive")


class TestComposeRuntime(DeploymentCase):
    COMPOSE = "services:\n  web:\n    image: nginx:1.27\n  db:\n    image: postgres:16\n"

    async def test_stop_start_services_and_status(self):
        dep = await self.create(name="shop", type="compose", compose=self.COMPOSE)
        await self.call("POST", f"/deployments/{dep['id']}/deploy", "operator")
        await self.wait_status(dep["id"], "deployed")
        for svc in ("web", "db"):
            self.engine.add_container(f"shop-{svc}-1", labels={
                "com.docker.compose.project": "shop", "com.docker.compose.service": svc})
        _, services = await self.call("GET", f"/deployments/{dep['id']}/services", "viewer", expect=200)
        self.assertEqual(sorted(s["service"] for s in services), ["db", "web"])
        _, status = await self.call("GET", f"/deployments/{dep['id']}/status", "viewer", expect=200)
        self.assertEqual(status[0]["state"], "running")

        # Stop brings the project down; Start brings it up again (recreated).
        await self.call("POST", f"/deployments/{dep['id']}/stop", "operator")
        await self.wait_status(dep["id"], "stopped")
        self.assertTrue([c for c in self.cli.calls() if c["argv"][:4] == ["compose", "-p", "shop", "down"]])
        ups = len([c for c in self.cli.calls() if "up" in c["argv"]])
        await self.call("POST", f"/deployments/{dep['id']}/start", "operator")
        await self.wait_status(dep["id"], "deployed")
        self.assertEqual(len([c for c in self.cli.calls() if "up" in c["argv"]]), ups + 1)

    async def test_logs_websocket(self):
        dep = await self.create(name="logged", type="container", spec={"image": "nginx:1.27"})
        await self.call("POST", f"/deployments/{dep['id']}/deploy", "operator")
        await self.wait_status(dep["id"], "deployed")
        cid = self.engine.container("logged")["Id"]
        self.engine.logs[cid] = [(1, "hello from the app"), (2, "a warning")]
        ws = await self.client.ws_connect(f"/api/v1/deployments/{dep['id']}/logs?follow=0&tail=10",
                                          headers=self.h["viewer"])
        got = []
        async for msg in ws:
            got.append(msg.data if isinstance(msg.data, str) else msg.data.decode())
        self.assertIn("hello from the app", "".join(got))
        # A container that isn't part of the deployment is refused on the socket.
        other = self.engine.add_container("stranger")
        ws = await self.client.ws_connect(f"/api/v1/deployments/{dep['id']}/logs?container={other}",
                                          headers=self.h["viewer"])
        msg = await ws.receive()
        self.assertIn("not part of deployment", msg.data)
        r = await self.client.get("/api/v1/deployments/missing/logs", headers=self.h["viewer"])
        self.assertEqual(r.status, 404)


class TestReconcile(DeploymentCase):
    async def test_down_container_is_redeployed_once(self):
        dep = await self.create(name="keepup", type="container", spec={"image": "nginx:1.27"})
        await self.call("POST", f"/deployments/{dep['id']}/deploy", "operator")
        await self.wait_status(dep["id"], "deployed")
        cid = self.engine.container("keepup")["Id"]
        self.engine.containers[cid]["State"].update(Running=False, Status="exited")
        svc = self.svc.deployments
        self.assertEqual(await svc.reconcile_once(), 1)
        await self.wait_status(dep["id"], "deployed")
        for _ in range(100):
            if (self.engine.container("keepup") or {}).get("State", {}).get("Running"):
                break
            await asyncio.sleep(0.05)
        self.assertTrue(self.engine.container("keepup")["State"]["Running"])
        # Cooldown: not again right away, and paused deployments are left alone.
        self.engine.container("keepup")["State"].update(Running=False, Status="exited")
        self.assertEqual(await svc.reconcile_once(), 0)
        await self.call("PUT", f"/deployments/{dep['id']}/enabled", "operator", json={"enabled": False}, expect=200)
        svc._reconcile_at.clear()
        self.assertEqual(await svc.reconcile_once(), 0)


if __name__ == "__main__":
    unittest.main()
