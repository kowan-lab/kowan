"""Unit tests for the new/generalized workflow step handlers.

Covers three engine step handlers WITHOUT touching real Docker or the store:

  * ``_run_prune``          — summarizes the right prune op(s) for each target;
  * ``_run_run_container``  — creates/starts/polls a one-shot container, captures
                              its stdout, exports outputVar + outputVar_EXIT into
                              the threaded run env, and raises on a non-zero exit
                              while preserving the captured output;
  * ``_run_deploy``         — dispatches the configured action to the matching
                              DeploymentService method (default redeploy).

Everything is driven with tiny synchronous fakes in the ``test_volumeauth.py`` /
``test_workflow_connections.py`` style: a fake Ops with canned return values, a
fake docker provider whose ``ops(host)`` returns it, a fake store exposing only
``get_host`` / ``get_deployment`` / ``get_deployment_by_name``, and a fake
DeploymentService recording its calls. Handlers are called directly (not through
``_execute``), so we build the threaded ``env`` dict ourselves.
"""

import asyncio
import unittest

from kowan.models import WorkflowStep
from kowan.workflow.engine import Deps, Engine, _StepError


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeLogStream:
    """A one-shot async byte stream over a fixed ``bytes`` payload.

    Satisfies the ``AsyncByteStream`` shape ``container_logs`` returns: an async
    ``read(n)`` that drains the buffer and returns b"" at EOF, plus a ``close``.
    """

    def __init__(self, data: bytes):
        self._data = data
        self.closed = False

    async def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            chunk, self._data = self._data, b""
            return chunk
        chunk, self._data = self._data[:n], self._data[n:]
        return chunk

    def close(self):
        self.closed = True


class _FakeOps:
    """A minimal Ops implementing just the methods the new steps touch.

    Prune ops return canned counts; the run-container path is scripted by a
    sequence of inspect States (``inspect_states``) consumed one per poll, with
    the last one repeated once exhausted, and by fixed log bytes. Every call is
    recorded so tests can assert what was invoked.
    """

    def __init__(self, *, inspect_states=None, logs=b"", create_id="cid-1"):
        self.calls = []
        self._inspect_states = list(inspect_states or [])
        self._logs = logs
        self._create_id = create_id
        # Canned prune return values (bytes reclaimed; networks returns None).
        self.prune_containers_bytes = 100
        self.prune_images_dangling_bytes = 200
        self.prune_images_all_bytes = 500
        self.prune_volumes_bytes = 300

    # -- prune ops -----------------------------------------------------------

    async def prune_containers(self) -> int:
        self.calls.append(("prune_containers",))
        return self.prune_containers_bytes

    async def prune_images(self, all: bool) -> int:
        self.calls.append(("prune_images", all))
        return self.prune_images_all_bytes if all else self.prune_images_dangling_bytes

    async def prune_volumes(self) -> int:
        self.calls.append(("prune_volumes",))
        return self.prune_volumes_bytes

    async def prune_networks(self) -> None:
        self.calls.append(("prune_networks",))

    # -- run-container ops ---------------------------------------------------

    async def create_container(self, name, spec, start) -> str:
        self.calls.append(("create_container", name, spec, start))
        return self._create_id

    async def start_container(self, id: str) -> None:
        self.calls.append(("start_container", id))

    async def inspect_container(self, id: str) -> dict:
        self.calls.append(("inspect_container", id))
        if self._inspect_states:
            state = self._inspect_states.pop(0)
        else:
            state = {"Running": False, "ExitCode": 0}
        return {"State": state}

    async def container_logs(self, id: str, follow: bool, tail: int):
        self.calls.append(("container_logs", id, follow, tail))
        return _FakeLogStream(self._logs)

    async def remove_container(self, id: str, force: bool) -> None:
        self.calls.append(("remove_container", id, force))


class _FakeProvider:
    """A docker provider whose ``ops(host)`` always returns the same fake Ops."""

    def __init__(self, ops):
        self._ops = ops

    def ops(self, host):
        return self._ops


class _FakeHost:
    def __init__(self, host_id, name):
        self.id = host_id
        self.name = name


class _FakeDeployment:
    def __init__(self, dep_id, name):
        self.id = dep_id
        self.name = name


class _FakeStore:
    """Store stub: resolves hosts and deployments by id (and deployments by
    name). Raises KeyError for unknown ids, which the handlers catch."""

    def __init__(self, *, hosts=None, deployments=None, deployments_by_name=None):
        self._hosts = hosts or {}
        self._deployments = deployments or {}
        self._deployments_by_name = deployments_by_name or {}

    def get_host(self, host_id):
        return self._hosts[host_id]

    def get_deployment(self, ref):
        return self._deployments[ref]

    def get_deployment_by_name(self, ref):
        return self._deployments_by_name[ref]


class _FakeDeployments:
    """DeploymentService stub recording which lifecycle method ran on which id."""

    def __init__(self):
        self.calls = []

    async def deploy(self, dep_id):
        self.calls.append(("deploy", dep_id))

    async def redeploy(self, dep_id):
        self.calls.append(("redeploy", dep_id))

    async def start(self, dep_id):
        self.calls.append(("start", dep_id))

    async def stop(self, dep_id):
        self.calls.append(("stop", dep_id))

    async def destroy(self, dep_id):
        self.calls.append(("destroy", dep_id))


def _engine(*, store=None, docker=None, deployments=None) -> Engine:
    return Engine(Deps(store=store, docker=docker, deployments=deployments))


# ---------------------------------------------------------------------------
# prune
# ---------------------------------------------------------------------------


class TestRunPrune(unittest.TestCase):
    def _run(self, target, *, ops=None):
        ops = ops or _FakeOps()
        store = _FakeStore(hosts={"h1": _FakeHost("h1", "host-one")})
        eng = _engine(store=store, docker=_FakeProvider(ops))
        step = WorkflowStep(
            name="p", type="prune", config={"host": "h1", "target": target}
        )
        out = asyncio.run(eng._run_prune(step))
        return out, ops

    def test_containers_target(self):
        out, ops = self._run("containers")
        self.assertIn(("prune_containers",), ops.calls)
        self.assertNotIn(("prune_volumes",), ops.calls)
        self.assertIn("100", out)
        self.assertIn("host-one", out)

    def test_images_dangling(self):
        out, ops = self._run("images")
        self.assertIn(("prune_images", False), ops.calls)
        self.assertIn("200", out)

    def test_images_all(self):
        out, ops = self._run("images-all")
        self.assertIn(("prune_images", True), ops.calls)
        self.assertIn("500", out)

    def test_volumes_target(self):
        out, ops = self._run("volumes")
        self.assertIn(("prune_volumes",), ops.calls)
        self.assertIn("300", out)

    def test_networks_target(self):
        out, ops = self._run("networks")
        self.assertIn(("prune_networks",), ops.calls)
        self.assertIn("networks", out)

    def test_all_target_runs_every_prune(self):
        out, ops = self._run("all")
        self.assertIn(("prune_containers",), ops.calls)
        self.assertIn(("prune_images", True), ops.calls)  # images-all under "all"
        self.assertIn(("prune_volumes",), ops.calls)
        self.assertIn(("prune_networks",), ops.calls)

    def test_missing_host_raises(self):
        eng = _engine(store=_FakeStore(), docker=_FakeProvider(_FakeOps()))
        step = WorkflowStep(type="prune", config={"target": "containers"})
        with self.assertRaises(ValueError):
            asyncio.run(eng._run_prune(step))

    def test_missing_target_raises(self):
        store = _FakeStore(hosts={"h1": _FakeHost("h1", "h")})
        eng = _engine(store=store, docker=_FakeProvider(_FakeOps()))
        step = WorkflowStep(type="prune", config={"host": "h1"})
        with self.assertRaises(ValueError):
            asyncio.run(eng._run_prune(step))

    def test_unknown_target_raises(self):
        store = _FakeStore(hosts={"h1": _FakeHost("h1", "h")})
        eng = _engine(store=store, docker=_FakeProvider(_FakeOps()))
        step = WorkflowStep(type="prune", config={"host": "h1", "target": "bogus"})
        with self.assertRaises(ValueError):
            asyncio.run(eng._run_prune(step))


# ---------------------------------------------------------------------------
# run-container
# ---------------------------------------------------------------------------


class TestRunRunContainer(unittest.TestCase):
    def _engine_with(self, ops):
        store = _FakeStore(hosts={"h1": _FakeHost("h1", "host-one")})
        return _engine(store=store, docker=_FakeProvider(ops))

    def test_captures_stdout_and_exports_var(self):
        # One poll: already exited with code 0, logs are "hello\n".
        ops = _FakeOps(
            inspect_states=[{"Running": False, "ExitCode": 0}],
            logs=b"hello\n",
        )
        eng = self._engine_with(ops)
        env = {}
        step = WorkflowStep(
            type="run-container",
            config={
                "host": "h1",
                "image": "alpine:latest",
                "command": "echo hello",
                "outputVar": "GREETING",
            },
        )
        out = asyncio.run(eng._run_run_container(step, env))

        # Output is the trimmed stdout.
        self.assertEqual(out, "hello")
        # Exported into the threaded env for later steps.
        self.assertEqual(env["GREETING"], "hello")
        self.assertEqual(env["GREETING_EXIT"], "0")

        # It created (start=False), started, polled inspect, and read logs.
        kinds = [c[0] for c in ops.calls]
        self.assertIn("create_container", kinds)
        self.assertIn("start_container", kinds)
        self.assertIn("inspect_container", kinds)
        self.assertIn("container_logs", kinds)

        # The command was shell-split into the spec.
        create = next(c for c in ops.calls if c[0] == "create_container")
        _, name, spec, start = create
        self.assertFalse(start)  # created without starting; started explicitly
        self.assertEqual(spec.image, "alpine:latest")
        self.assertEqual(spec.command, ["echo", "hello"])

    def test_env_lines_become_spec_env(self):
        ops = _FakeOps(
            inspect_states=[{"Running": False, "ExitCode": 0}], logs=b"ok"
        )
        eng = self._engine_with(ops)
        step = WorkflowStep(
            type="run-container",
            config={
                "host": "h1",
                "image": "img",
                "env": "FOO=bar\n# comment\nBAZ=qux=extra\n",
            },
        )
        asyncio.run(eng._run_run_container(step, {}))
        create = next(c for c in ops.calls if c[0] == "create_container")
        spec = create[2]
        kv = {e.key: e.value for e in spec.env}
        self.assertEqual(kv, {"FOO": "bar", "BAZ": "qux=extra"})

    def test_polls_until_not_running(self):
        # Two "running" polls then an exit; the poll loop must keep going.
        ops = _FakeOps(
            inspect_states=[
                {"Running": True},
                {"Running": True},
                {"Running": False, "ExitCode": 0},
            ],
            logs=b"done",
        )
        eng = self._engine_with(ops)
        step = WorkflowStep(
            type="run-container", config={"host": "h1", "image": "img"}
        )
        out = asyncio.run(eng._run_run_container(step, {}))
        self.assertEqual(out, "done")
        inspects = [c for c in ops.calls if c[0] == "inspect_container"]
        self.assertEqual(len(inspects), 3)

    def test_nonzero_exit_raises_but_preserves_output(self):
        ops = _FakeOps(
            inspect_states=[{"Running": False, "ExitCode": 2}],
            logs=b"boom\n",
        )
        eng = self._engine_with(ops)
        env = {}
        step = WorkflowStep(
            type="run-container",
            config={"host": "h1", "image": "img", "outputVar": "R"},
        )
        with self.assertRaises(_StepError) as ctx:
            asyncio.run(eng._run_run_container(step, env))
        # The captured logs ride along on the failure (like the http step).
        self.assertEqual(ctx.exception.output, "boom")
        # The exports still happened before the raise.
        self.assertEqual(env["R"], "boom")
        self.assertEqual(env["R_EXIT"], "2")

    def test_remove_truthy_force_removes(self):
        ops = _FakeOps(
            inspect_states=[{"Running": False, "ExitCode": 0}], logs=b""
        )
        eng = self._engine_with(ops)
        step = WorkflowStep(
            type="run-container",
            config={"host": "h1", "image": "img", "remove": "true"},
        )
        asyncio.run(eng._run_run_container(step, {}))
        self.assertIn(("remove_container", "cid-1", True), ops.calls)

    def test_no_remove_leaves_container(self):
        ops = _FakeOps(
            inspect_states=[{"Running": False, "ExitCode": 0}], logs=b""
        )
        eng = self._engine_with(ops)
        step = WorkflowStep(
            type="run-container", config={"host": "h1", "image": "img"}
        )
        asyncio.run(eng._run_run_container(step, {}))
        self.assertNotIn("remove_container", [c[0] for c in ops.calls])

    def test_missing_image_raises(self):
        eng = self._engine_with(_FakeOps())
        step = WorkflowStep(type="run-container", config={"host": "h1"})
        with self.assertRaises(ValueError):
            asyncio.run(eng._run_run_container(step, {}))


# ---------------------------------------------------------------------------
# deploy (generalized to an action)
# ---------------------------------------------------------------------------


class TestRunDeployAction(unittest.TestCase):
    def _run(self, config):
        deps = _FakeDeployments()
        store = _FakeStore(
            deployments={"d1": _FakeDeployment("d1", "web")},
            deployments_by_name={"web": _FakeDeployment("d1", "web")},
        )
        eng = _engine(store=store, deployments=deps)
        step = WorkflowStep(type="deploy", config=config)
        out = asyncio.run(eng._run_deploy(step))
        return out, deps

    def test_default_action_is_redeploy(self):
        # Empty/missing action -> redeploy (back-compat).
        out, deps = self._run({"deployment": "d1"})
        self.assertEqual(deps.calls, [("redeploy", "d1")])
        self.assertIn("redeploy", out)
        self.assertIn("web", out)

    def test_explicit_deploy_action(self):
        _, deps = self._run({"deployment": "d1", "action": "deploy"})
        self.assertEqual(deps.calls, [("deploy", "d1")])

    def test_start_stop_destroy_dispatch(self):
        for action in ("start", "stop", "destroy"):
            with self.subTest(action=action):
                _, deps = self._run({"deployment": "d1", "action": action})
                self.assertEqual(deps.calls, [(action, "d1")])

    def test_resolves_by_name(self):
        _, deps = self._run({"deployment": "web", "action": "start"})
        self.assertEqual(deps.calls, [("start", "d1")])  # id used, not the name

    def test_unknown_action_raises(self):
        deps = _FakeDeployments()
        store = _FakeStore(deployments={"d1": _FakeDeployment("d1", "web")})
        eng = _engine(store=store, deployments=deps)
        step = WorkflowStep(type="deploy", config={"deployment": "d1", "action": "bogus"})
        with self.assertRaises(ValueError):
            asyncio.run(eng._run_deploy(step))
        self.assertEqual(deps.calls, [])  # nothing dispatched

    def test_deployments_none_raises(self):
        store = _FakeStore(deployments={"d1": _FakeDeployment("d1", "web")})
        eng = _engine(store=store, deployments=None)
        step = WorkflowStep(type="deploy", config={"deployment": "d1"})
        with self.assertRaises(ValueError):
            asyncio.run(eng._run_deploy(step))

    def test_missing_deployment_ref_raises(self):
        deps = _FakeDeployments()
        eng = _engine(store=_FakeStore(), deployments=deps)
        step = WorkflowStep(type="deploy", config={"action": "start"})
        with self.assertRaises(ValueError):
            asyncio.run(eng._run_deploy(step))


class TestStepTypeValidation(unittest.TestCase):
    """Guards that the API validator accepts every step type the engine handles.

    Regression: run-container and prune were added to the model/engine/catalog but
    left out of the API's _VALID_STEP_TYPES, so saving a workflow using them was
    rejected. This keeps the accepted set in sync with the engine's dispatch.
    """

    def test_new_step_types_are_accepted_by_validation(self):
        from kowan.api.workflows import _VALID_STEP_TYPES
        from kowan.models import WorkflowStepPrune, WorkflowStepRunContainer

        self.assertIn(WorkflowStepRunContainer, _VALID_STEP_TYPES)
        self.assertIn(WorkflowStepPrune, _VALID_STEP_TYPES)


if __name__ == "__main__":
    unittest.main()
