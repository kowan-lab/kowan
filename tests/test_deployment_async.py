"""Async (background) deploy dispatch — offline, no Docker/network.

Verifies that pressing deploy is non-blocking: ``dispatch_deploy`` flips the record
to ``pending`` and returns immediately, a background task then runs the real deploy
(status ``deploying`` while it runs, terminal status after), a second dispatch of
the same deployment while one is in flight is rejected (409), and once finished a
new dispatch is allowed again.
"""

import asyncio
import unittest
from unittest import mock

from kowan.deployment.service import (
    DeploymentService,
    STATUS_DEPLOYED,
    STATUS_DEPLOYING,
    STATUS_PENDING,
    STATUS_STOPPED,
)
from kowan.errors import Conflict
from kowan.models.deployment import Deployment, DeploymentTarget


class _FakeStore:
    def __init__(self, dep: Deployment):
        self._deps = {dep.id: dep}

    def get_deployment(self, dep_id: str) -> Deployment:
        # Return a copy, like a real DB read would (callers mutate + persist).
        return self._deps[dep_id].model_copy(deep=True)

    def update_deployment(self, dep: Deployment) -> None:
        self._deps[dep.id] = dep.model_copy(deep=True)


class _FakeSvc:
    def __init__(self, store):
        self.store = store
        self.docker = object()


class TestDispatchDeploy(unittest.TestCase):
    def _service(self):
        dep = Deployment(id="d1", name="app", type="container", strategy="standalone", targets=["h1"])
        svc = _FakeSvc(_FakeStore(dep))
        return DeploymentService(svc)

    def test_pending_then_deploying_then_deployed(self):
        async def scenario():
            dep_svc = self._service()
            started = asyncio.Event()
            release = asyncio.Event()

            async def fake_deploy(dep_id: str) -> None:
                started.set()
                await release.wait()
                d = dep_svc.store.get_deployment(dep_id)
                d.status = STATUS_DEPLOYED
                dep_svc.store.update_deployment(d)

            dep_svc.deploy = fake_deploy  # type: ignore[assignment]

            # Dispatch returns immediately with the record already PENDING.
            dep = await dep_svc.dispatch_deploy("d1")
            self.assertEqual(dep.status, STATUS_PENDING)
            task = dep_svc._tasks["d1"]

            # The background task acquires a slot and flips it to DEPLOYING.
            await asyncio.wait_for(started.wait(), timeout=1)
            self.assertEqual(dep_svc.store.get_deployment("d1").status, STATUS_DEPLOYING)

            # A second dispatch while in flight is rejected.
            with self.assertRaises(Conflict):
                await dep_svc.dispatch_deploy("d1")

            # Let it finish; terminal status persisted, task untracked.
            release.set()
            await asyncio.wait_for(task, timeout=1)
            self.assertEqual(dep_svc.store.get_deployment("d1").status, STATUS_DEPLOYED)
            self.assertNotIn("d1", dep_svc._tasks)

            # A fresh dispatch is allowed again after completion.
            release2 = asyncio.Event()
            release2.set()
            dep_svc.deploy = fake_deploy  # type: ignore[assignment]
            release.clear()
            release.set()
            dep2 = await dep_svc.dispatch_deploy("d1")
            self.assertEqual(dep2.status, STATUS_PENDING)
            await asyncio.wait_for(dep_svc._tasks["d1"], timeout=1)

        asyncio.run(scenario())

    def test_redeploy_flag_routes_to_redeploy(self):
        async def scenario():
            dep_svc = self._service()
            calls = []

            async def fake_redeploy(dep_id: str) -> None:
                calls.append(("redeploy", dep_id))

            async def fake_deploy(dep_id: str) -> None:
                calls.append(("deploy", dep_id))

            dep_svc.redeploy = fake_redeploy  # type: ignore[assignment]
            dep_svc.deploy = fake_deploy  # type: ignore[assignment]

            await dep_svc.dispatch_deploy("d1", redeploy=True)
            await asyncio.wait_for(dep_svc._tasks["d1"], timeout=1)
            self.assertEqual(calls, [("redeploy", "d1")])

        asyncio.run(scenario())


class _ListStore:
    def __init__(self, deps):
        self._deps = {d.id: d for d in deps}

    def list_deployments(self):
        return list(self._deps.values())

    def get_deployment(self, dep_id):
        return self._deps[dep_id].model_copy(deep=True)

    def update_deployment(self, dep):
        self._deps[dep.id] = dep.model_copy(deep=True)


class TestReconcile(unittest.TestCase):
    """The desired-state loop redeploys an ENABLED, DEPLOYED deployment whose target
    is down — and leaves everything else alone (paused, stopped, already up, or in
    cooldown)."""

    def _service(self, dep):
        svc = _FakeSvc(_ListStore([dep]))
        dep_svc = DeploymentService(svc)
        dep_svc.dispatch_deploy = mock.AsyncMock()  # type: ignore[assignment]
        return dep_svc

    def _dep(self, **kw):
        base = dict(id="d1", name="app", type="container", strategy="standalone",
                    targets=["h1"], status=STATUS_DEPLOYED, enabled=True)
        base.update(kw)
        return Deployment(**base)

    def _down(self, dep_svc):
        dep_svc.status = mock.AsyncMock(  # type: ignore[assignment]
            return_value=[DeploymentTarget(hostId="h1", state="not_deployed")]
        )

    def _up(self, dep_svc):
        dep_svc.status = mock.AsyncMock(  # type: ignore[assignment]
            return_value=[DeploymentTarget(hostId="h1", state="running")]
        )

    def test_redeploys_down_enabled_deployed(self):
        async def scenario():
            dep_svc = self._service(self._dep())
            self._down(dep_svc)
            n = await dep_svc.reconcile_once()
            self.assertEqual(n, 1)
            dep_svc.dispatch_deploy.assert_awaited_once_with("d1", redeploy=True)
        asyncio.run(scenario())

    def test_skips_when_up(self):
        async def scenario():
            dep_svc = self._service(self._dep())
            self._up(dep_svc)
            self.assertEqual(await dep_svc.reconcile_once(), 0)
            dep_svc.dispatch_deploy.assert_not_awaited()
        asyncio.run(scenario())

    def test_skips_disabled(self):
        async def scenario():
            dep_svc = self._service(self._dep(enabled=False))
            self._down(dep_svc)
            self.assertEqual(await dep_svc.reconcile_once(), 0)
            dep_svc.dispatch_deploy.assert_not_awaited()
        asyncio.run(scenario())

    def test_skips_non_deployed_status(self):
        async def scenario():
            # A manually stopped (or failed) deployment is left alone — no loop.
            dep_svc = self._service(self._dep(status=STATUS_STOPPED))
            self._down(dep_svc)
            self.assertEqual(await dep_svc.reconcile_once(), 0)
            dep_svc.dispatch_deploy.assert_not_awaited()
        asyncio.run(scenario())

    def test_cooldown_blocks_second_pass(self):
        async def scenario():
            dep_svc = self._service(self._dep())
            self._down(dep_svc)
            self.assertEqual(await dep_svc.reconcile_once(), 1)
            # Immediately again: still down, but within the cooldown -> skipped.
            self.assertEqual(await dep_svc.reconcile_once(), 0)
            dep_svc.dispatch_deploy.assert_awaited_once()
        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
