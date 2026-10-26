"""Auto-update switch, the rarer branches: a pull before the switch, an old
image already gone, a tag that resolves to nothing, a current-image scan that
fails, a crash loop, a container whose inspect fails mid-verification, a
rollback that fails, a failed switch with nothing to roll back to; plus the
updater's Engine adapter over the real Docker client (fake Engine socket)."""

import asyncio
import unittest

from kowan.autoupdate import engine
from kowan.updater.engine import LocalEngine, is_not_found
from kowan.errors import NotFound
from tests.fakeengine import FakeEngine
from tests.test_autoupdate import FakeDocker, _Clock

V = engine.VerifyOptions(timeout=30, stable=4, poll=1)


def run(docker, **kw):
    clk = _Clock()
    kw.setdefault("verify", V)
    return asyncio.run(engine.apply_container_update(docker, "c0", image_ref=kw.pop("image_ref", "nginx:latest"),
                                                     clock=clk, sleep=clk.sleep, **kw))


class TestSwitchEdges(unittest.TestCase):
    def test_pull_runs_first_and_old_image_may_be_gone(self):
        d = FakeDocker()
        pulled = []

        async def pull():
            pulled.append(True)
        del d.images["sha256:old"]  # pruned meanwhile
        out = run(d, pull=pull)
        self.assertEqual((pulled, out.outcome), ([True], "updated"))
        self.assertEqual(out.rollback_ref, "sha256:old")  # only the id is left

    def test_tag_resolving_to_nothing(self):
        d = FakeDocker()
        real = d.inspect_image

        async def inspect_image(ref):
            if ref == "nginx:latest":
                return {}
            return await real(ref)
        d.inspect_image = inspect_image
        out = run(d)
        self.assertEqual(out.outcome, "failed")
        self.assertIn("not found after pull", out.message)

    def test_current_image_scan_failure_is_noted(self):
        async def scan(img):
            if img["Id"] == "sha256:old":
                raise RuntimeError("old layers missing")
            return {"counts": {"critical": 0, "high": 0}}
        out = run(FakeDocker(), gate_policy="worse", scan=scan)
        self.assertIn("scan current image: old layers missing", out.details)

    def test_crash_loop_and_inspect_failure(self):
        d = FakeDocker()
        real = d.inspect_container
        calls = {"n": 0}

        async def looping(cid):
            got = await real(cid)
            if got["Image"] == "sha256:new":
                got["State"] = {"Running": True, "Restarting": True, "Status": "restarting"}
            return got
        d.inspect_container = looping
        out = run(d)
        self.assertEqual(out.outcome, "rolled_back")
        self.assertIn("crash loop", out.message)

        d = FakeDocker()
        real = d.inspect_container

        async def flaky(cid):
            calls["n"] += 1
            got = await real(cid)
            if got["Image"] == "sha256:new":
                raise RuntimeError("daemon hiccup")
            return got
        d.inspect_container = flaky
        out = run(d)
        self.assertEqual(out.outcome, "failed")  # cannot inspect the new one to roll it back
        self.assertIn("inspect failed: daemon hiccup", out.message)

    def test_rollback_fails_or_is_unhealthy_too(self):
        d = FakeDocker({"sha256:new": "crash"})
        real = d.clone_recreate

        async def no_second(cid, ref, start=True):
            if ref.startswith("nginx@"):
                raise RuntimeError("disk full")
            return await real(cid, ref, start)
        d.clone_recreate = no_second
        out = run(d)
        self.assertEqual(out.outcome, "failed")
        self.assertIn("rollback failed: disk full", out.message)

        d = FakeDocker({"sha256:new": "crash", "sha256:old": "crash"})
        out = run(d)
        self.assertEqual(out.outcome, "rolled_back")
        self.assertIn("previous image is not healthy either", out.message)

    def test_failed_switch_recovery_branches(self):
        # Nothing left under the name: plain failure.
        d = FakeDocker()

        async def boom(cid, ref, start=True):
            d.containers.clear()
            raise RuntimeError("create failed")
        d.clone_recreate = boom
        out = run(d)
        self.assertEqual((out.outcome, out.message), ("failed", "recreate web: create failed"))

        # A clone left on the new image: recreated from the old one...
        def left_on_new(behaviour=None, fail_rollback=False):
            d = FakeDocker(behaviour)
            real = d.clone_recreate
            state = {"n": 0}

            async def clone(cid, ref, start=True):
                state["n"] += 1
                if state["n"] == 1:
                    old = d.containers.pop(cid)
                    d._add("cx", "web", ref, d._resolve(ref))
                    d.containers["cx"]["State"] = {"Running": False, "Status": "created"}
                    del old
                    raise RuntimeError("start failed")
                if fail_rollback:
                    raise RuntimeError("no space")
                return await real(cid, ref, start)
            d.clone_recreate = clone
            return d

        out = run(left_on_new())
        self.assertEqual(out.outcome, "rolled_back")
        self.assertIn("switch failed (start failed); rolled back to nginx@sha256:d-old", out.message)
        out = run(left_on_new(fail_rollback=True))
        self.assertEqual(out.outcome, "failed")
        self.assertIn("rollback failed: no space", out.message)
        out = run(left_on_new({"sha256:old": "crash"}))
        self.assertIn("previous image is not healthy either", out.message)

    def test_rollback_reference(self):
        class Ref:
            name = "docker.io/library/nginx"
        self.assertEqual(engine.rollback_ref_for({"RepoDigests": ["other@sha256:x"]}, Ref()), "other@sha256:x")
        self.assertEqual(engine.rollback_ref_for({"Id": "sha256:id"}, Ref()), "sha256:id")


class TestLocalEngine(unittest.IsolatedAsyncioTestCase):
    async def test_adapter_over_the_engine(self):
        fake = FakeEngine()
        sock = await fake.start()
        try:
            cid = fake.add_container("web", logs=["hello"])
            e = LocalEngine(sock)
            self.assertEqual((await e.inspect_container(cid))["Id"], cid)
            img = fake.containers[cid]["Image"]
            self.assertEqual((await e.inspect_image(img))["Id"], img)
            await e.stop(cid, 5)
            await e.start(cid)
            await e.rename(cid, "web-old")
            await e.set_restart_policy(cid, {"Name": "no"})
            new = await e.create("web-new", {"Image": "nginx:1.27", "HostConfig": {}})
            await e.connect("bridge", new, {})
            self.assertEqual(await e.logs_tail(cid, 5), ["hello"])
            await e.remove(new, True)
            with self.assertRaises(Exception) as cm:
                await e.inspect_container("nope")
            self.assertTrue(is_not_found(cm.exception))
            self.assertTrue(is_not_found(NotFound("x")))
            self.assertFalse(is_not_found(RuntimeError("boom")))
            await e.close()
        finally:
            await fake.stop()


if __name__ == "__main__":
    unittest.main()
