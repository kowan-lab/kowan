"""The deployment service's failure paths against a scriptable fake host:
what a failed pull, create, compose, swarm, secret resolution, stop/start or
teardown leaves behind (status, the per-host error shown in the UI), the
reconcile pass's skips, and the git helpers' errors."""

import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from kowan.deployment import service as svcmod
from kowan.deployment.service import DeploymentService
from kowan.errors import Conflict, Validation
from kowan.models import Container, ContainerSpec, Deployment, GitSource
from kowan.store import Store


class FakeHost:
    """Ops that fail on the steps listed in ``fail``."""

    def __init__(self, fail=(), containers=()):
        self.fail = set(fail)
        self.calls = []
        self.containers = list(containers)

    def _step(self, name, *args):
        self.calls.append((name, *args))
        if name in self.fail:
            raise RuntimeError(f"{name} broke")

    async def pull_image(self, image, auth):
        self._step("pull_image", image)

        async def stream():
            yield b"{}"
        return stream()

    async def create_container(self, name, spec, start):
        self._step("create_container", name)

    async def compose_up(self, project, text):
        self._step("compose_up", project)
        return ""

    async def compose_down(self, project):
        self._step("compose_down", project)

    async def swarm_active(self):
        self._step("swarm_active")
        return "inactive_swarm" not in self.fail

    async def swarm_deploy(self, stack, text):
        self._step("swarm_deploy", stack, text)

    async def swarm_remove(self, stack):
        self._step("swarm_remove", stack)

    async def list_containers(self, all_):
        self._step("list_containers")
        return self.containers

    async def remove_container(self, cid, force):
        self._step("remove_container", cid)

    async def start_container(self, cid):
        self._step("start_container", cid)

    async def stop_container(self, cid):
        self._step("stop_container", cid)


def dep(**kw):
    base = dict(id="d1", name="app", type="container", strategy="standalone", targets=["h1"],
                spec=ContainerSpec(image="nginx:1.27"), status="stopped", createdAt="c", updatedAt="u")
    base.update(kw)
    return Deployment(**base)


class ServiceCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.tmp, "t.db"))
        svc = SimpleNamespace(store=self.store, docker=object(),
                              config=SimpleNamespace(data_dir=os.path.join(self.tmp, "data")))
        self.s = DeploymentService(svc)
        self.host = FakeHost()
        self.s._ops_for_host = lambda host_id: (self.host, None)  # type: ignore[assignment]

    async def asyncTearDown(self):
        self.store.close()

    def save(self, d):
        self.store.create_deployment(d)
        return d

    def status(self, dep_id="d1"):
        return self.store.get_deployment(dep_id).status

    async def target_error(self, dep_id="d1", host="h1"):
        return await self.s._target_error(dep_id, host)


class TestDeployFailures(ServiceCase):
    async def test_container_pull_failure_still_creates(self):
        self.save(dep())
        self.host.fail = {"pull_image"}
        await self.s.deploy("d1")
        self.assertEqual(self.status(), "deployed")
        self.assertIn(("create_container", "app"), self.host.calls)
        self.assertEqual(await self.target_error(), "")

    async def test_create_failure_records_the_host_error(self):
        self.save(dep())
        self.host.fail = {"create_container"}
        with self.assertRaisesRegex(Validation, "create container on host 'h1'"):
            await self.s.deploy("d1")
        self.assertEqual(self.status(), "error")
        self.assertIn("create_container broke", await self.target_error())
        # A later success clears it.
        self.host.fail = set()
        await self.s.deploy("d1")
        self.assertEqual(await self.target_error(), "")

    async def test_secret_and_spec_errors(self):
        self.save(dep())
        with mock.patch.object(DeploymentService, "_resolve_spec_secrets", side_effect=RuntimeError("no such secret")):
            with self.assertRaisesRegex(Validation, "resolve secrets for 'app'"):
                await self.s.deploy("d1")
        self.save(dep(id="d2", name="nospec", spec=None))
        with self.assertRaisesRegex(Validation, "container spec is required"):
            await self.s.deploy("d2")
        self.save(dep(id="d3", name="odd", type="weird", spec=None))
        with self.assertRaisesRegex(Validation, "unknown type"):
            await self.s.deploy("d3")
        self.save(dep(id="d4", name="nohost", targets=[]))
        with self.assertRaisesRegex(Validation, "has no target host"):
            await self.s.deploy("d4")

    async def test_compose_failures(self):
        self.save(dep(type="compose", spec=None, compose="services: {}"))
        self.host.fail = {"compose_up"}
        with self.assertRaisesRegex(Validation, "compose up on host"):
            await self.s.deploy("d1")
        self.host.fail = set()
        with mock.patch.object(self.store, "resolve_secrets", side_effect=RuntimeError("vault down")):
            with self.assertRaisesRegex(Validation, "resolve secrets"):
                await self.s.deploy("d1")

    async def test_persisting_the_status_can_fail_too(self):
        self.save(dep())
        self.host.fail = {"create_container"}
        with mock.patch.object(self.store, "update_deployment", side_effect=RuntimeError("disk full")):
            with self.assertRaisesRegex(Validation, "and persist status: disk full"):
                await self.s.deploy("d1")
            self.host.fail = set()
            with self.assertRaisesRegex(Validation, "persist deployment status: disk full"):
                await self.s.deploy("d1")


class TestSwarm(ServiceCase):
    async def test_container_compose_and_failures(self):
        self.save(dep(strategy="swarm", spec=ContainerSpec(image="nginx:1.27")))
        await self.s.deploy("d1")
        stack = next(c for c in self.host.calls if c[0] == "swarm_deploy")
        self.assertIn("nginx:1.27", stack[2])
        self.save(dep(id="d2", name="cs", type="compose", strategy="swarm", spec=None, compose="services:\n  w:\n    image: x\n"))
        await self.s.deploy("d2")
        self.host.fail = {"swarm_deploy"}
        with self.assertRaisesRegex(Validation, "swarm"):
            await self.s.deploy("d2")
        self.assertEqual(self.status("d2"), "error")
        self.host.fail = {"inactive_swarm"}
        with self.assertRaisesRegex(Validation, "swarm"):
            await self.s.deploy("d2")
        self.host.fail = set()
        self.save(dep(id="d3", name="ns", strategy="swarm", spec=None))
        with self.assertRaisesRegex(Validation, "container spec is required"):
            await self.s.deploy("d3")
        self.save(dep(id="d4", name="nt", strategy="swarm", targets=[]))
        with self.assertRaisesRegex(Validation, "has no target host"):
            await self.s.deploy("d4")
        self.save(dep(id="d5", name="wt", type="weird", strategy="swarm", spec=None))
        with self.assertRaisesRegex(Validation, "unknown type"):
            await self.s.deploy("d5")
        with mock.patch.object(DeploymentService, "_resolve_spec_secrets", side_effect=RuntimeError("x")):
            with self.assertRaisesRegex(Validation, "resolve secrets"):
                await self.s.deploy("d1")
        with mock.patch.object(self.store, "resolve_secrets", side_effect=RuntimeError("y")):
            with self.assertRaisesRegex(Validation, "resolve secrets"):
                await self.s.deploy("d2")
        # Start re-deploys a stack; stop removes it, and its failure is an error status.
        await self.s.start("d1")
        self.assertEqual(self.status("d1"), "deployed")
        self.host.fail = {"swarm_remove"}
        with self.assertRaisesRegex(Validation, "stop: swarm remove"):
            await self.s.stop("d1")
        self.assertEqual(self.status("d1"), "error")
        with self.assertRaisesRegex(Validation, "destroy: swarm remove"):
            await self.s.destroy("d1")


class TestLifecycle(ServiceCase):
    async def test_destroy_paths(self):
        mine = Container(id="c1", names=["/app"], labels={"kowan.deployment.id": "d1"})
        other = Container(id="c2", names=["/x"], labels={})
        self.host.containers = [mine, other]
        self.save(dep())
        await self.s.destroy("d1")
        self.assertEqual([c for c in self.host.calls if c[0] == "remove_container"], [("remove_container", "c1")])
        self.assertEqual(self.status(), "stopped")
        for fail, msg in (("remove_container", "remove container c1"), ("list_containers", "list containers")):
            self.host.fail = {fail}
            with self.assertRaisesRegex(Validation, msg):
                await self.s.destroy("d1")
        self.host.fail = {"compose_down"}
        self.save(dep(id="d2", name="cmp", type="compose", spec=None, compose="services: {}"))
        with self.assertRaisesRegex(Validation, "compose down on host"):
            await self.s.destroy("d2")
        self.save(dep(id="d3", name="wt", type="weird", spec=None))
        with self.assertRaisesRegex(Validation, "unknown type"):
            await self.s.destroy("d3")
        with mock.patch.object(self.store, "update_deployment", side_effect=RuntimeError("ro")):
            with self.assertRaisesRegex(Validation, "and persist status: ro"):
                await self.s.destroy("d3")
            self.host.fail = set()
            with self.assertRaisesRegex(Validation, "persist deployment status: ro"):
                await self.s.destroy("d1")

    async def test_start_stop_paths(self):
        self.host.containers = [Container(id="c1", names=["/app"], labels={"kowan.deployment.id": "d1"})]
        self.save(dep())
        await self.s.stop("d1")
        await self.s.start("d1")
        self.assertIn(("stop_container", "c1"), self.host.calls)
        self.assertIn(("start_container", "c1"), self.host.calls)
        for fail, op, call in (("start_container", "start", self.s.start), ("stop_container", "stop", self.s.stop)):
            self.host.fail = {fail}
            with self.assertRaisesRegex(Validation, f"{op}: {op} container c1"):
                await call("d1")
            self.assertEqual(self.status(), "error")
        self.host.fail = {"compose_down"}
        self.save(dep(id="d2", name="cmp", type="compose", spec=None, compose="services: {}"))
        with self.assertRaisesRegex(Validation, "compose down"):
            await self.s.stop("d2")
        self.host.fail = {"compose_up"}
        with self.assertRaisesRegex(Validation, "compose up"):
            await self.s.start("d2")
        self.save(dep(id="d3", name="wt", type="weird", spec=None))
        for call in (self.s.start, self.s.stop):
            with self.assertRaisesRegex(Validation, "unknown type"):
                await call("d3")
        self.host.fail = set()
        with mock.patch.object(self.store, "update_deployment", side_effect=RuntimeError("ro")):
            with self.assertRaisesRegex(Validation, "persist deployment status: ro"):
                await self.s.stop("d1")
            self.host.fail = {"stop_container"}
            with self.assertRaisesRegex(Validation, "and persist status: ro"):
                await self.s.stop("d1")


_PHASE = """services:
  nginx: {image: nginx, restart: always}
  backend: {image: app, restart: unless-stopped}
  frontend: {image: web, restart: unless-stopped}
  migrations: {image: app}
  debug: {image: busybox, profiles: [debug]}
  spare: {image: app, scale: 0}
"""


def ctr(service, state="running", status="Up 1 hour", project="phase"):
    return Container(id=f"id-{service}", names=[f"/{project}-{service}"], state=state, status=status,
                     labels={"com.docker.compose.project": project, "com.docker.compose.service": service})


class TestComposeStatus(ServiceCase):
    """A compose target is judged by every service it declares: one container
    up and the others gone is degraded, and the reconcile loop heals it."""

    async def target(self, *containers):
        self.host.containers = list(containers)
        [t] = await self.s.status("d1")
        return t

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.s._ops_for_host = lambda host_id: (self.host, SimpleNamespace(name="phase-ec2-1"))  # type: ignore[assignment]
        self.save(dep(id="d1", name="phase", type="compose", spec=None, compose=_PHASE, status="deployed"))

    async def test_states(self):
        done = ctr("migrations", "exited", "Exited (0) 6 minutes ago")
        t = await self.target(ctr("nginx"), ctr("backend"), ctr("frontend"), done)
        self.assertEqual((t.state, t.message), ("running", ""))  # the one-shot exit 0 is fine; profiles/scale 0 not expected

        t = await self.target(ctr("nginx"), done)
        self.assertEqual((t.state, t.message), ("degraded", "backend, frontend missing"))
        self.assertEqual(t.container_id, "id-nginx")

        t = await self.target(ctr("nginx"), ctr("backend", "exited", "Exited (137) 1 minute ago"), ctr("frontend"),
                              ctr("migrations", "exited", "Exited (1) 1 minute ago"))
        self.assertEqual((t.state, t.message), ("degraded", "backend, migrations stopped"))

        t = await self.target(ctr("nginx", "exited", "Exited (0) now"), ctr("backend", "exited", "Exited (0) now"))
        self.assertEqual(t.state, "stopped")  # nothing runs: stopped, as before

        # Other projects' containers do not count for this one.
        t = await self.target(ctr("nginx"), ctr("backend", project="other"), ctr("frontend"), done)
        self.assertEqual((t.state, t.message), ("degraded", "backend missing"))

    async def test_unparsable_compose_falls_back_to_the_first_container(self):
        for text in ("services: [", "just text", "services: nope"):
            d = self.store.get_deployment("d1")
            d.compose = text
            self.store.update_deployment(d)
            self.assertEqual((await self.target(ctr("nginx"))).state, "running")

    async def test_reconcile_heals_a_degraded_stack(self):
        self.host.containers = [ctr("nginx"), ctr("migrations", "exited", "Exited (0) now")]
        dispatched = []

        async def dispatch(dep_id, *, redeploy=False):
            dispatched.append((dep_id, redeploy))

        with mock.patch.object(self.s, "dispatch_deploy", dispatch):
            self.assertEqual(await self.s.reconcile_once(), 1)
        self.assertEqual(dispatched, [("d1", True)])

    async def test_reconcile_lists_each_host_once_per_pass(self):
        self.save(dep(id="d2", name="other", status="deployed"))
        self.save(dep(id="d3", name="down", status="deployed", targets=["h2"]))
        self.host.containers = [ctr("nginx"), ctr("backend"), ctr("frontend"),
                                Container(id="x", names=["/other"], state="running", status="Up")]
        hosts = []

        def ops_for(host_id):
            hosts.append(host_id)
            if host_id == "h2":
                raise RuntimeError("host gone")
            return self.host, SimpleNamespace(name=host_id)
        self.s._ops_for_host = ops_for  # type: ignore[assignment]
        with mock.patch.object(self.s, "dispatch_deploy", mock.AsyncMock()) as dispatch:
            await self.s.reconcile_once()
        self.assertIn(mock.call("d3", redeploy=True), dispatch.call_args_list)
        self.assertEqual([c for c in self.host.calls if c[0] == "list_containers"], [("list_containers",)])
        self.assertEqual(sorted(hosts), ["h1", "h1", "h2"])

    async def test_reconcile_leaves_an_unreachable_host_alone(self):
        # An agent updating, or not yet back after a server restart: its
        # containers are unknown, and a redeploy would fail and mark the
        # deployment as errored.
        async def gone(_all):
            raise ConnectionError("agent not connected")
        self.host.list_containers = gone  # type: ignore[method-assign]
        with mock.patch.object(self.s, "dispatch_deploy", mock.AsyncMock()) as dispatch:
            self.assertEqual(await self.s.reconcile_once(), 0)
        dispatch.assert_not_called()
        self.assertEqual(self.store.get_deployment("d1").status, "deployed")

        # A swarm manager that cannot be reached (inactive) is left alone too.
        with mock.patch.object(self.s, "status", mock.AsyncMock(return_value=[SimpleNamespace(state="inactive", host_id="h1")])):
            self.assertFalse(await self.s._needs_reconcile(dep(), {}))

    async def test_reconcile_forgets_deleted_deployments_and_spent_cooldowns(self):
        self.s._reconcile_at.update({"gone": 1.0, "d1": 1.0})
        self.s._deploy_errs.update({"gone": {"h1": "x"}, "d1": {"h1": "y"}})
        self.host.containers = [ctr(n) for n in ("nginx", "backend", "frontend")] + [
            ctr("migrations", "exited", "Exited (0) now")]
        with mock.patch("kowan.deployment.service.time.monotonic", return_value=1.0 + 30), \
                mock.patch.object(self.s, "dispatch_deploy", mock.AsyncMock()):
            await self.s.reconcile_once()
        self.assertEqual((self.s._reconcile_at, self.s._deploy_errs), ({"d1": 1.0}, {"d1": {"h1": "y"}}))
        with mock.patch("kowan.deployment.service.time.monotonic", return_value=1.0 + 10_000), \
                mock.patch.object(self.s, "dispatch_deploy", mock.AsyncMock()):
            await self.s.reconcile_once()
        self.assertEqual(self.s._reconcile_at, {})


class TestReconcileAndBackground(ServiceCase):
    async def test_reconcile_right_after_the_host_boots(self):
        # Monotonic time starts at boot: a deployment never reconciled is not
        # "in cooldown" just because the host came up a minute ago.
        self.save(dep(id="down", name="dn", status="deployed"))
        dispatched = []

        async def status(dep_id, listings=None):
            return [SimpleNamespace(state="exited", host_id="h1")]

        async def dispatch(dep_id, *, redeploy=False):
            dispatched.append(dep_id)

        with mock.patch.object(self.s, "status", status), mock.patch.object(self.s, "dispatch_deploy", dispatch), \
                mock.patch("kowan.deployment.service.time.monotonic", return_value=5.0):
            self.assertEqual(await self.s.reconcile_once(), 1)
            self.assertEqual(await self.s.reconcile_once(), 0)  # now it is
        self.assertEqual(dispatched, ["down"])

    async def test_reconcile_skips_and_dispatches(self):
        self.save(dep(id="paused", name="p", status="deployed", enabled=False))
        self.save(dep(id="stopped", name="s", status="stopped"))
        self.save(dep(id="busy", name="b", status="deployed"))
        self.save(dep(id="fine", name="f", status="deployed"))
        self.save(dep(id="down", name="dn", status="deployed"))
        self.save(dep(id="boom", name="bm", status="deployed"))
        self.save(dep(id="racing", name="r", status="deployed"))
        never = asyncio.ensure_future(asyncio.sleep(3600))
        self.s._tasks["busy"] = never
        states = {"fine": "running", "down": "exited", "racing": "exited"}

        async def status(dep_id, listings=None):
            if dep_id == "boom":
                raise RuntimeError("host gone")
            return [SimpleNamespace(state=states[dep_id], host_id="h1")]

        dispatched = []

        async def dispatch(dep_id, *, redeploy=False):
            if dep_id == "racing":
                raise Conflict("already deploying")
            dispatched.append(dep_id)

        with mock.patch.object(self.s, "status", status), mock.patch.object(self.s, "dispatch_deploy", dispatch):
            self.assertEqual(await self.s.reconcile_once(), 1)
            self.assertEqual(dispatched, ["down"])
            # Cooldown: not again right away.
            self.assertEqual(await self.s.reconcile_once(), 0)
        never.cancel()
        with mock.patch.object(self.store, "list_deployments", side_effect=RuntimeError("db")):
            self.assertEqual(await self.s.reconcile_once(), 0)
        with mock.patch.object(self.s, "status", mock.AsyncMock(return_value=[])):
            self.assertFalse(await self.s._needs_reconcile(dep(), {}))

    async def test_loop_survives_a_failing_pass(self):
        calls = []

        async def bad():
            calls.append(1)
            raise RuntimeError("x")

        with mock.patch.object(self.s, "reconcile_once", bad):
            task = asyncio.ensure_future(self.s.run_reconcile_loop(interval=0.01))
            # Wait for a second pass rather than a fixed time: a loaded runner
            # can stretch 0.05s into a single tick.
            for _ in range(500):
                if len(calls) > 1:
                    break
                await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertGreater(len(calls), 1)

    async def test_background_deploy_crash_is_logged(self):
        self.save(dep())

        async def crash(dep_id, redeploy):
            raise RuntimeError("worker died")

        with mock.patch.object(self.s, "_run_deploy_bg", crash), self.assertLogs("kowan.deployment", "ERROR") as logs:
            await self.s.dispatch_deploy("d1")
            for _ in range(50):
                if not self.s._tasks:
                    break
                await asyncio.sleep(0.01)
        self.assertTrue(any("worker died" in m for m in logs.output))

    async def test_marking_deploying_can_fail(self):
        self.save(dep())
        with mock.patch.object(self.s, "_set_status", side_effect=RuntimeError("locked")), \
                mock.patch.object(self.s, "redeploy", mock.AsyncMock()) as redeploy, \
                self.assertLogs("kowan.deployment", "ERROR"):
            await self.s._run_deploy_bg("d1", True)
        redeploy.assert_awaited_once()


class TestGitHelpers(ServiceCase):
    async def test_errors(self):
        plain = dep()
        with self.assertRaisesRegex(Validation, "no git source"):
            await self.s.remote_commit(plain)
        self.save(plain)
        with self.assertRaisesRegex(Validation, "no git source"):
            await self.s.sync("d1")
        g = dep(id="g", name="g", type="compose", spec=None, git=GitSource(repoUrl="https://e.com/r.git", credentialId="c-x"))
        with self.assertRaisesRegex(Validation, "git credential 'c-x'"):
            await self.s.remote_commit(g)
        g.git.credential_id = ""

        async def lsr(url, ref, auth):
            raise svcmod.gitsource.GitError("repository not found")

        with mock.patch.object(svcmod.gitsource, "ls_remote", lsr):
            with self.assertRaisesRegex(Validation, "repository not found"):
                await self.s.remote_commit(g)

        class _Prov:
            def git_auth(self, url):
                return "bot", "tok"

        g.git.credential_id = "c-ok"
        with mock.patch("kowan.connections.resolve", mock.AsyncMock(return_value=_Prov())):
            auth = await self.s._git_auth(g)
        self.assertEqual((auth.username, auth.token), ("bot", "tok"))


if __name__ == "__main__":
    unittest.main()
