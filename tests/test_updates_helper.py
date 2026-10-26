"""The self-update helper's swap and EVERY rollback branch, against a fake daemon.

Each test checks the end state the failure matrix in docs/updates-design.md
promises: which container holds the name, which is running, restart policies,
what was (not) deleted, whether the DB restore ran, and the result status.
"""

import unittest

from kowan.updater.helper import Swap, exit_code
from kowan.updater.plan import parse_journal
from tests.updates_fakes import FakeClock, FakeDaemon, FakeEngine


def _plan(**kw):
    p = {"id": "u1234567abcd", "component": "server", "mode": "update", "target": "kowan-server",
         "image": "kowan:v2", "waitSeconds": 0, "verifyTimeout": 60, "stableSeconds": 5,
         "stopTimeout": 5}
    p.update(kw)
    return p


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.d = FakeDaemon()
        self.d.add_image("kowan:v1", "healthy", version="1.0")
        self.old = self.d.add_container("kowan-server", "kowan:v1", env=["KOWAN_X=1"])
        self.out = []
        self.restores = []

    def _restore(self, db, snap, aside):
        self.restores.append((db, snap))
        return {"moved": [db], "keys": []}

    async def run_swap(self, **kw):
        clock = FakeClock()
        swap = Swap(FakeEngine(self.d), _plan(**kw), out=self.out.append, sleep=clock.sleep,
                    monotonic=clock.monotonic, restore_db=self._restore)
        return await swap.run()

    def holder(self, name="kowan-server"):
        return self.d.by_name(name)


class TestUpdateSuccess(_Base):
    async def test_swap_parks_previous_and_runs_new(self):
        self.d.add_image("kowan:v2", "healthy", version="2.0")
        res = await self.run_swap()
        self.assertEqual(res["result"], "updated", res)
        new = self.holder()
        self.assertNotEqual(new["Id"], self.old)
        self.assertEqual(new["Config"]["Image"], "kowan:v2")
        self.assertTrue(new["State"]["Running"])
        self.assertEqual(new["Config"]["Labels"]["kowan.update.id"], "u1234567abcd")
        self.assertIn("KOWAN_X=1", new["Config"]["Env"])
        # restart policy cloned onto the new one, parked one set to "no"
        self.assertEqual(new["HostConfig"]["RestartPolicy"]["Name"], "unless-stopped")
        prev = self.holder("kowan-server-previous")
        self.assertEqual(prev["Id"], self.old)
        self.assertFalse(prev["State"]["Running"])
        self.assertEqual(prev["HostConfig"]["RestartPolicy"]["Name"], "no")
        self.assertEqual(self.restores, [])
        self.assertEqual(exit_code(res["result"]), 0)
        # image defaults follow the NEW image (not the old version label)
        self.assertEqual(new["Config"]["Labels"].get("org.opencontainers.image.version"), "2.0")

    async def test_older_previous_is_retired_then_removed_on_commit(self):
        self.d.add_image("kowan:v0", "healthy")
        older = self.d.add_container("kowan-server-previous", "kowan:v0", running=False)
        self.d.add_image("kowan:v2", "healthy")
        res = await self.run_swap()
        self.assertEqual(res["result"], "updated")
        self.assertNotIn(older, self.d.containers)
        self.assertEqual(self.holder("kowan-server-previous")["Id"], self.old)
        self.assertEqual(sorted(self.d.names()), ["kowan-server", "kowan-server-previous"])

    async def test_journal_lines_parse(self):
        self.d.add_image("kowan:v2", "healthy")
        await self.run_swap()
        parsed = parse_journal("\n".join(self.out))
        phases = [p["phase"] for p in parsed["phases"]]
        self.assertEqual(phases[:5], ["wait", "inspect", "stop", "park", "create"])
        self.assertEqual(phases[-1], "commit")
        self.assertTrue(all(p["ok"] for p in parsed["phases"]))

    async def test_no_healthcheck_image_needs_stability_only(self):
        self.d.add_image("kowan:v2", "nohc")
        res = await self.run_swap()
        self.assertEqual(res["result"], "updated", res)


class TestUpdateRollback(_Base):
    def assert_restored(self, res, status="rolled_back"):
        self.assertEqual(res["result"], status, res)
        cur = self.holder()
        self.assertEqual(cur["Id"], self.old)
        self.assertTrue(cur["State"]["Running"])
        self.assertEqual(cur["HostConfig"]["RestartPolicy"]["Name"], "unless-stopped")

    async def test_crashing_new_version_rolls_back_with_db_and_logs(self):
        self.d.add_image("kowan:v3", "crash")
        res = await self.run_swap(image="kowan:v3", db={"path": "/data/kowan.db", "rollbackRestore": "/data/backups/pre-update-u1.db"})
        self.assert_restored(res)
        self.assertEqual(self.restores, [("/data/kowan.db", "/data/backups/pre-update-u1.db")])
        self.assertIn("Traceback: boom", res["logs"])
        self.assertIn("exited", res["error"])
        # the broken container is gone only AFTER the old one verified
        self.assertEqual(self.d.names(), ["kowan-server"])

    async def test_unhealthy_new_version_rolls_back(self):
        self.d.add_image("kowan:v3", "unhealthy")
        res = await self.run_swap(image="kowan:v3")
        self.assert_restored(res)
        self.assertIn("unhealthy", res["error"])

    async def test_health_never_ready_times_out_and_rolls_back(self):
        self.d.add_image("kowan:v3", "starting")
        res = await self.run_swap(image="kowan:v3", verifyTimeout=20)
        self.assert_restored(res)
        self.assertIn("not healthy after", res["error"])

    async def test_restart_loop_counts_as_failure(self):
        self.d.add_image("kowan:v3", "restarting")
        # the fake bumps RestartCount at each start; simulate a second restart mid-verify
        eng_inspect = FakeEngine.inspect_container
        calls = {"n": 0}

        async def flaky(eng, ref):
            r = await eng_inspect(eng, ref)
            calls["n"] += 1
            if r["Config"].get("Image") == "kowan:v3" and calls["n"] > 3:
                r["RestartCount"] += 1
            return r

        FakeEngine.inspect_container = flaky
        try:
            res = await self.run_swap(image="kowan:v3")
        finally:
            FakeEngine.inspect_container = eng_inspect
        self.assert_restored(res)
        self.assertIn("restarted", res["error"])

    async def test_create_failure_restores_without_db_restore(self):
        self.d.add_image("kowan:v2", "healthy")
        self.d.fail("create", "kowan-server")
        res = await self.run_swap(db={"path": "/data/kowan.db", "rollbackRestore": "/s.db"})
        self.assert_restored(res)
        self.assertEqual(self.restores, [], "DB untouched: the new version never ran")

    async def test_start_failure_restores(self):
        self.d.add_image("kowan:v2", "healthy")
        orig_start = FakeEngine.start

        async def start(eng, ref):
            c = eng.d.resolve(ref)
            if c["Config"].get("Image") == "kowan:v2":
                raise RuntimeError("port is already allocated")
            return await orig_start(eng, ref)

        FakeEngine.start = start
        try:
            res = await self.run_swap()
        finally:
            FakeEngine.start = orig_start
        self.assert_restored(res)
        self.assertIn("start failed", res["error"])

    async def test_stop_failure_restarts_target_as_is(self):
        self.d.add_image("kowan:v2", "healthy")
        self.d.fail("stop", "kowan-server")
        res = await self.run_swap()
        self.assert_restored(res)
        self.assertEqual(self.d.names(), ["kowan-server"])

    async def test_park_failure_is_undone(self):
        self.d.add_image("kowan:v2", "healthy")
        self.d.fail("set_restart_policy")
        res = await self.run_swap()
        self.assertEqual(res["result"], "rolled_back", res)
        cur = self.holder()
        self.assertEqual(cur["Id"], self.old)
        self.assertTrue(cur["State"]["Running"])
        self.assertIsNone(self.holder("kowan-server-previous"))

    async def test_park_failure_restores_older_previous(self):
        self.d.add_image("kowan:v0", "healthy")
        older = self.d.add_container("kowan-server-previous", "kowan:v0", running=False)
        self.d.add_image("kowan:v2", "healthy")
        self.d.fail("set_restart_policy", times=1)
        res = await self.run_swap()
        self.assertEqual(res["result"], "rolled_back")
        self.assertEqual(self.holder("kowan-server-previous")["Id"], older)

    async def test_failed_rollback_keeps_everything(self):
        self.d.add_image("kowan:v3", "crash")
        # the old version also refuses to come back
        self.d.images[self.d.containers[self.old]["Image"]]["_behavior"] = "crash"
        res = await self.run_swap(image="kowan:v3", db={"path": "/d/kowan.db", "rollbackRestore": "/s.db"})
        self.assertEqual(res["result"], "failed", res)
        self.assertEqual(exit_code("failed"), 2)
        # both containers still exist, nothing deleted
        self.assertIn(self.old, self.d.containers)
        self.assertIn("kowan-server-failed-u1234567", self.d.names())
        self.assertTrue(any("docker rename" in s for s in res["operator"]))

    async def test_cannot_free_name_is_failed_and_old_kept_parked(self):
        self.d.add_image("kowan:v3", "crash")
        orig_rename = FakeEngine.rename

        async def rename(eng, ref, new):
            if new.startswith("kowan-server-failed"):
                raise RuntimeError("rename refused")
            return await orig_rename(eng, ref, new)

        self.d.fail("remove")
        FakeEngine.rename = rename
        try:
            res = await self.run_swap(image="kowan:v3")
        finally:
            FakeEngine.rename = orig_rename
        self.assertEqual(res["result"], "failed")
        self.assertEqual(self.holder("kowan-server-previous")["Id"], self.old)


class TestUpdateAborts(_Base):
    async def test_missing_target_aborts(self):
        self.d.add_image("kowan:v2")
        res = await self.run_swap(target="nope")
        self.assertEqual(res["result"], "aborted")

    async def test_stopped_target_aborts(self):
        self.d.add_image("kowan:v2")
        self.d.containers[self.old]["State"].update({"Running": False, "Status": "exited"})
        res = await self.run_swap()
        self.assertEqual(res["result"], "aborted")
        self.assertEqual(self.d.names(), ["kowan-server"])

    async def test_missing_new_image_aborts_untouched(self):
        res = await self.run_swap(image="kowan:v9")
        self.assertEqual(res["result"], "aborted")
        self.assertTrue(self.holder()["State"]["Running"])
        self.assertFalse(any(c[0] == "stop" for c in self.d.calls))


class TestRollbackMode(_Base):
    def setUp(self):
        super().setUp()
        # current = v2 running, previous = v1 parked (the state after an update)
        self.d.add_image("kowan:v2", "healthy", version="2.0")
        self.d.containers[self.old]["Name"] = "/kowan-server-previous"
        self.d.containers[self.old]["State"].update({"Running": False, "Status": "exited"})
        self.d.containers[self.old]["HostConfig"]["RestartPolicy"] = {"Name": "no"}
        self.cur = self.d.add_container("kowan-server", "kowan:v2")

    async def test_swap_back_and_park_current(self):
        res = await self.run_swap(mode="rollback", image=None,
                                  db={"path": "/d/kowan.db", "swapRestore": "/b/pre-update-A.db",
                                      "rollbackRestore": "/b/pre-update-B.db"})
        self.assertEqual(res["result"], "updated", res)
        self.assertEqual(self.holder()["Id"], self.old)
        self.assertTrue(self.holder()["State"]["Running"])
        self.assertEqual(self.holder()["HostConfig"]["RestartPolicy"]["Name"], "unless-stopped")
        self.assertEqual(self.holder("kowan-server-previous")["Id"], self.cur)
        self.assertEqual(self.holder("kowan-server-previous")["HostConfig"]["RestartPolicy"]["Name"], "no")
        self.assertEqual(self.restores, [("/d/kowan.db", "/b/pre-update-A.db")])
        self.assertEqual(res["toImage"], "kowan:v1")

    async def test_unhealthy_previous_reverts_to_current_and_db(self):
        self.d.images[self.d.containers[self.old]["Image"]]["_behavior"] = "crash"
        res = await self.run_swap(mode="rollback", image=None,
                                  db={"path": "/d/kowan.db", "swapRestore": "/b/A.db", "rollbackRestore": "/b/B.db"})
        self.assertEqual(res["result"], "rolled_back", res)
        self.assertEqual(self.holder()["Id"], self.cur)
        self.assertTrue(self.holder()["State"]["Running"])
        self.assertEqual(self.holder("kowan-server-previous")["Id"], self.old)
        self.assertEqual(self.restores, [("/d/kowan.db", "/b/A.db"), ("/d/kowan.db", "/b/B.db")])

    async def test_without_previous_aborts(self):
        del self.d.containers[self.old]
        res = await self.run_swap(mode="rollback", image=None)
        self.assertEqual(res["result"], "aborted")
        self.assertTrue(self.holder()["State"]["Running"])

    async def test_agent_rollback_without_db(self):
        res = await self.run_swap(mode="rollback", image=None, component="agent")
        self.assertEqual(res["result"], "updated")
        self.assertEqual(self.restores, [])


if __name__ == "__main__":
    unittest.main()
