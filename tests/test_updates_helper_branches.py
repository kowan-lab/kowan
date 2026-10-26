"""The self-update helper, the branches the main suite leaves: a daemon that
errors on inspect, unreadable logs, a database restore that fails, the old
image gone, extra networks, a retired previous that cannot be removed, the
restore itself failing, the rollback mode's stop / park / database / start /
revert failures, the last-resort crash guard and the CLI entry point."""

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest import mock

from kowan.errors import BadGateway
from kowan.updater import helper as hp
from kowan.updater.helper import Swap
from kowan.updater.plan import encode_plan
from tests.test_updates_helper import _Base, _plan
from tests.updates_fakes import FakeClock, FakeEngine


class TestUpdateBranches(_Base):
    async def test_daemon_errors_and_old_image_gone(self):
        self.d.add_image("kowan:v2", "healthy")
        # A non-"not found" inspect error is a crash, caught by the guard.
        self.d.fail("inspect_container", err=BadGateway("daemon down"), times=1)
        res = await self.run_swap()
        self.assertIn(res["result"], ("aborted", "failed"))
        # The old image cannot be inspected: config cloned verbatim, with a warning.
        self.setUp()
        self.d.add_image("kowan:v2", "healthy")
        self.d.fail("inspect_image", match=self.d.containers[self.old]["Image"])
        res = await self.run_swap()
        self.assertEqual(res["result"], "updated", res)
        self.assertIn("old image not inspectable; cloning config verbatim", res.get("warnings", []))

    async def test_crashing_new_version_with_failed_db_restore_and_logs(self):
        self.d.add_image("kowan:v3", "crash")
        self.d.fail("logs_tail")

        def broken_restore(db, snap, aside):
            raise OSError("read-only")
        self._restore = broken_restore
        res = await self.run_swap(image="kowan:v3", db={"path": "/d/kowan.db", "rollbackRestore": "/s.db"})
        self.assertEqual(res["result"], "rolled_back", res)
        self.assertIn("WARNING: database snapshot could not be restored", res["error"])
        self.assertTrue(any("could not read logs" in line for line in res["logs"]))

    async def test_restore_of_previous_fails(self):
        self.d.add_image("kowan:v3", "crash")
        self.d.fail("start", match=self.old)
        res = await self.run_swap(image="kowan:v3")
        self.assertEqual(res["result"], "failed")
        self.assertIn("restore failed", res["error"])
        self.assertTrue(res["operator"])

    async def test_retired_previous_not_removable_still_commits(self):
        self.d.add_image("kowan:v0", "healthy")
        self.d.add_container("kowan-server-previous", "kowan:v0", running=False)
        self.d.add_image("kowan:v2", "healthy")
        self.d.fail("remove")
        res = await self.run_swap()
        self.assertEqual(res["result"], "updated", res)

    async def test_crash_guard(self):
        clock = FakeClock()
        swap = Swap(FakeEngine(self.d), _plan(), out=self.out.append, sleep=clock.sleep, monotonic=clock.monotonic,
                    restore_db=self._restore)
        with mock.patch.object(swap, "_run_update", side_effect=RuntimeError("bug")):
            res = await swap.run()
        self.assertEqual(res["result"], "failed")
        self.assertIn("helper error: bug", res["error"])
        steps = swap._operator_steps("", "x-previous", {})
        self.assertNotIn("docker stop", " ".join(steps))


class TestRollbackBranches(_Base):
    def setUp(self):
        super().setUp()
        self.d.add_image("kowan:v2", "healthy", version="2.0")
        self.d.containers[self.old]["Name"] = "/kowan-server-previous"
        self.d.containers[self.old]["State"].update({"Running": False, "Status": "exited"})
        self.cur = self.d.add_container("kowan-server", "kowan:v2")

    async def rollback(self, **kw):
        return await self.run_swap(mode="rollback", image=None, **kw)

    async def test_stop_failure_restarts_current(self):
        self.d.fail("stop", "kowan-server")
        res = await self.rollback()
        self.assertEqual(res["result"], "rolled_back", res)
        self.assertEqual(self.holder()["Id"], self.cur)

    async def test_park_failures_are_undone(self):
        self.d.fail("set_restart_policy", times=2)
        res = await self.rollback()
        self.assertEqual(self.holder()["Id"], self.cur)
        self.assertIn("swap failed", res["error"])
        # Failing on the rename of the previous container: undone too.
        self.setUp()
        self.d.fail("rename", match=self.old, times=1)
        res = await self.rollback()
        self.assertEqual(self.holder()["Id"], self.cur)

    async def test_database_paths(self):
        def broken(db, snap, aside):
            raise OSError("ro")
        self._restore = broken
        res = await self.rollback(db={"path": "/d/kowan.db", "swapRestore": "/a.db", "rollbackRestore": "/b.db"})
        self.assertEqual(res["result"], "rolled_back")
        self.assertIn("could not restore the database", res["error"])
        self.setUp()
        res = await self.rollback(db={"path": "/d/kowan.db"})
        self.assertIn("no database snapshot for the previous version", " ".join(res.get("warnings", [])))

    async def test_start_failure_and_revert_paths(self):
        self.d.fail("start", match=self.old)
        res = await self.rollback()
        self.assertEqual(res["result"], "rolled_back", res)
        self.assertIn("start failed", res["error"])
        # Revert cannot free the name.
        self.setUp()
        self.d.images[self.d.containers[self.old]["Image"]]["_behavior"] = "crash"
        orig = FakeEngine.rename

        async def rename(eng, ref, new):
            if ref == self.old and new == "kowan-server-previous":
                raise RuntimeError("name busy")
            return await orig(eng, ref, new)
        FakeEngine.rename = rename
        try:
            res = await self.rollback()
        finally:
            FakeEngine.rename = orig
        self.assertEqual(res["result"], "failed")
        self.assertIn("could not free kowan-server", res["error"])
        # The revert's restart fails.
        self.setUp()
        self.d.images[self.d.containers[self.old]["Image"]]["_behavior"] = "crash"
        self.d.fail("start", match=self.cur)
        res = await self.rollback()
        self.assertEqual(res["result"], "failed")
        self.assertIn("restore failed", res["error"])
        # The current version does not come back healthy either.
        self.setUp()
        self.d.images[self.d.containers[self.old]["Image"]]["_behavior"] = "crash"
        self.d.images[self.d.containers[self.cur]["Image"]]["_behavior"] = "crash"
        res = await self.rollback()
        self.assertEqual(res["result"], "failed")
        self.assertIn("did not come back healthy", res["error"])

    async def test_parking_the_rolled_back_container_can_fail(self):
        orig = FakeEngine.rename

        async def rename(eng, ref, new):
            if new == "kowan-server-previous" and ref == self.cur:
                raise RuntimeError("name taken")
            return await orig(eng, ref, new)
        FakeEngine.rename = rename
        try:
            res = await self.rollback()
        finally:
            FakeEngine.rename = orig
        self.assertEqual(res["result"], "updated")
        # Kept under its parking name instead of the "previous" one.
        self.assertIn(self.cur, self.d.containers)
        self.assertNotEqual(self.d.containers[self.cur]["Name"], "/kowan-server-previous")


class TestCLI(unittest.TestCase):
    def test_main(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(hp.main([]), 0)
            self.assertEqual(hp.main(["--plan", "@@@"]), 0)
        lines = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertTrue(all(x["result"] == "aborted" for x in lines))
        plan = {"id": "u1", "component": "agent", "mode": "update", "target": "kowan-agent", "image": "kowan-agent:2"}
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.object(hp, "run_plan", side_effect=RuntimeError("no socket")):
            self.assertEqual(hp.main(["--plan", encode_plan(plan)]), 1)
        self.assertIn("helper crashed: no socket", out.getvalue())
        out = io.StringIO()

        async def fake_run_plan(p, sock):
            return 2
        with redirect_stdout(out), mock.patch.object(hp, "run_plan", fake_run_plan):
            self.assertEqual(hp.main(["--plan", encode_plan(plan), "--socket", "/x.sock"]), 2)

    def test_run_plan_with_an_engine(self):
        from tests.updates_fakes import FakeDaemon

        d = FakeDaemon()
        d.add_image("kowan:v1", "healthy")
        d.add_container("kowan-server", "kowan:v1")

        class Eng(FakeEngine):
            async def close(self):
                raise RuntimeError("already closed")
        out = io.StringIO()
        import asyncio
        with redirect_stdout(out):
            code = asyncio.run(hp.run_plan(_plan(image="kowan:missing"), engine=Eng(d)))
        self.assertEqual(code, 0)
        self.assertIn('"aborted"', out.getvalue())


if __name__ == "__main__":
    unittest.main()
