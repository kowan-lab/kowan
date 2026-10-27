"""Orchestrator (status/lock/pull/preflight/snapshot/launch/batch) and
reconciliation, end to end against the in-memory daemon: the helper the
orchestrator launches is actually run (``Swap``) and its journal reconciled."""

import asyncio
import os
import tempfile
import unittest
from unittest import mock

from kowan.models import Host
from kowan.store import Store
from kowan.updater import reconcile as rc
from kowan.updater.helper import Swap
from kowan.updater.orchestrator import UpdateError, UpdateManager, release_image
from kowan.updater.plan import plan_from_cmd, result_line
from kowan.updater.reconcile import Reconciler, fold_result
from tests.updates_fakes import FakeClock, FakeDaemon, FakeEngine, FakeOps


class _Cfg:
    def __init__(self, d):
        self.data_dir = d


class _Docker:
    def __init__(self, ops_by_host):
        self.ops_by_host = ops_by_host

    def ops(self, host):
        return self.ops_by_host[host.id]


class _Hub:
    def __init__(self):
        self.up = set()

    def connected(self, hid):
        return hid in self.up


class _Svc:
    def __init__(self, data_dir, store, docker, hub):
        self.config = _Cfg(data_dir)
        self.store = store
        self.docker = docker
        self.hub = hub


def _host(hid, typ="agent"):
    return Host(id=hid, name=hid, type=typ, mode="edge", address="", token="t",
                lastSeen="", createdAt="2026-01-01T00:00:00Z")


class _Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.dir, "kowan.db"))
        self.store.create_host(_host("local", "local"))
        self.d = FakeDaemon()
        self.d.add_image("kowan:v1", "healthy", version="1.0")
        self.d.add_image("kowan:v2", "healthy", version="2.0")
        mounts = [{"Type": "bind", "Source": "/var/run/docker.sock", "Destination": "/var/run/docker.sock", "RW": True},
                  {"Type": "volume", "Name": "kowan-data", "Destination": self.dir, "RW": True}]
        self.server = self.d.add_container("kowan-server", "kowan:v1", env=["KOWAN_SECRET_KEY=s"], mounts=mounts)
        self.ops = FakeOps(self.d)
        self.adir = FakeDaemon()
        self.adir.add_image("ghcr.io/kowan-lab/kowan-agent:v1", "healthy", version="1.0")
        self.adir.add_image("ghcr.io/kowan-lab/kowan-agent:v2", "healthy", version="2.0")
        self.adir.add_container("kowan-agent", "ghcr.io/kowan-lab/kowan-agent:v1", mounts=mounts[:1])
        self.aops = FakeOps(self.adir)
        self.hub = _Hub()
        self.store.create_host(_host("h1"))
        self.svc = _Svc(self.dir, self.store, _Docker({"local": self.ops, "h1": self.aops}), self.hub)
        self.m = UpdateManager(self.svc, helper_wait=0, poll=0.01)
        self.env = mock.patch.dict(os.environ, {"KOWAN_SERVER_CONTAINER": "kowan-server"})
        self.env.start()
        self.digest = mock.patch("kowan.agentupdate._remote_digest",
                                 new=mock.AsyncMock(side_effect=RuntimeError("offline")))
        self.digest.start()

    async def asyncTearDown(self):
        self.env.stop()
        self.digest.stop()
        self.store.close()

    async def drive(self, uid):
        for t in list(self.m._tasks.values()):
            await t
        return self.store.get_update(uid)

    async def run_helper(self, daemon, ops, comp, uid, restore_db=None):
        """Execute the launched helper's plan with the fake engine (as Docker would)."""
        helper = daemon.by_name(f"kowan-{comp}-updater-{uid}")
        plan = plan_from_cmd(helper["Config"]["Cmd"])
        out = []
        clock = FakeClock()
        res = await Swap(FakeEngine(daemon), plan, out=out.append, sleep=clock.sleep,
                         monotonic=clock.monotonic,
                         restore_db=restore_db or (lambda db, snap, aside: {"moved": [], "keys": []})).run()
        out.append(result_line(res))
        ops.helper_logs[helper["Id"]] = "\n".join(out)
        helper["State"] = {"Status": "exited", "Running": False, "ExitCode": 0 if res["result"] != "failed" else 2}
        return res, plan


class TestReleaseImage(unittest.TestCase):
    def test_pinned_release_moves_to_the_newest(self):
        self.assertEqual(release_image("ghcr.io/kowan-lab/kowan:0.1.3", "0.1.4"), "ghcr.io/kowan-lab/kowan:0.1.4")
        self.assertEqual(release_image("ghcr.io/kowan-lab/kowan:v0.1.3", "0.1.4"), "ghcr.io/kowan-lab/kowan:0.1.4")
        self.assertEqual(release_image("localhost:5000/kowan:0.1.3@sha256:ab", "0.2.0"), "localhost:5000/kowan:0.2.0")

    def test_other_refs_update_in_place(self):
        for ref in ("ghcr.io/kowan-lab/kowan", "ghcr.io/kowan-lab/kowan:latest", "ghcr.io/kowan-lab/kowan:0.1",
                    "localhost:5000/kowan", "kowan:custom", "sha256:abc"):
            self.assertEqual(release_image(ref, "0.1.4"), ref)
        # Already on (or past) the newest release, or no release known.
        self.assertEqual(release_image("kowan:0.1.4", "0.1.4"), "kowan:0.1.4")
        self.assertEqual(release_image("kowan:0.2.0", "0.1.4"), "kowan:0.2.0")
        self.assertEqual(release_image("kowan:0.1.3", ""), "kowan:0.1.3")


class _Releases:
    def __init__(self, latest):
        self._latest = latest

    async def latest(self, refresh=False):
        return self._latest, ""

    async def status(self, refresh=False):
        return {"currentVersion": "0.1.3", "latestVersion": self._latest, "newVersion": True, "releaseError": ""}


class TestServerUpdate(_Base):
    async def test_update_without_image_takes_the_newest_release(self):
        # The server runs a pinned release; "update" with no image must not
        # pull 0.1.3 again but move to 0.1.4.
        self.d.add_image("kowan:0.1.3", "healthy", version="0.1.3")
        self.d.add_image("kowan:0.1.4", "healthy", version="0.1.4")
        self.d.containers.pop(self.server)
        mounts = [{"Type": "bind", "Source": "/var/run/docker.sock", "Destination": "/var/run/docker.sock", "RW": True},
                  {"Type": "volume", "Name": "kowan-data", "Destination": self.dir, "RW": True}]
        self.server = self.d.add_container("kowan-server", "kowan:0.1.3", env=["KOWAN_SECRET_KEY=s"], mounts=mounts)
        self.svc.releases = _Releases("0.1.4")
        st = await self.m.status("server")
        self.assertEqual((st["image"], st["updateImage"]), ("kowan:0.1.3", "kowan:0.1.4"))
        rec = await self.drive(await self.m.start("server", user="admin"))
        self.assertEqual((rec["toImage"], rec["toVersion"]), ("kowan:0.1.4", "0.1.4"), rec)

    async def test_status_shape(self):
        st = await self.m.status("server")
        self.assertTrue(st["supported"], st)
        self.assertEqual(st["container"], "kowan-server")
        self.assertEqual(st["version"], "1.0")
        self.assertFalse(st["canRollback"])
        self.assertIsNone(st["previous"])
        self.assertIn("offline", st["error"])
        for k in ("reason", "image", "running", "latest", "updateAvailable", "inProgress", "last"):
            self.assertIn(k, st)

    async def test_full_update_then_reconcile(self):
        uid = await self.m.start("server", image="kowan:v2", user="admin")
        rec = await self.drive(uid)
        phases = [p["phase"] for p in rec["phases"]]
        self.assertEqual(phases, ["lock", "resolve", "pull", "platform", "snapshot", "preflight", "launch"], rec)
        self.assertEqual(rec["status"], "running")
        self.assertEqual(rec["toVersion"], "2.0")
        self.assertEqual(rec["startedBy"], "admin")
        # preflight ran from the NEW image on a DB copy and was removed
        pf = [c for c in self.ops.created if c[0].startswith("kowan-server-preflight-")][0]
        self.assertEqual(pf[1].image, self.d.images["kowan:v2"]["Id"])
        self.assertEqual(pf[1].command[:2], ["preflight", "--db"])
        self.assertTrue(all(v.read_only for v in pf[1].volumes))
        self.assertIn(("KOWAN_SECRET_KEY", "s"), [(e.key, e.value) for e in pf[1].env])
        self.assertIn(pf[0], self.ops.removed)
        # helper from the CURRENT image id, socket + data mounts, restart no
        name, spec, start = [c for c in self.ops.created if c[0].startswith("kowan-server-updater-")][0]
        self.assertTrue(start)
        self.assertEqual(spec.image, self.d.containers[self.server]["Image"])
        self.assertEqual(spec.restart_policy, "no")
        self.assertEqual({v.target for v in spec.volumes}, {"/var/run/docker.sock", self.dir})
        plan = plan_from_cmd(spec.command)
        self.assertTrue(os.path.isfile(plan["db"]["rollbackRestore"]))
        self.assertEqual(plan["record"]["phases"][-1]["phase"], "launch")
        # lock held while running
        with self.assertRaises(UpdateError) as cm:
            await self.m.start("server", image="kowan:v2")
        self.assertEqual(cm.exception.status, 409)

        res, _ = await self.run_helper(self.d, self.ops, "server", uid)
        self.assertEqual(res["result"], "updated")
        busy = await Reconciler(self.svc, self.m).reconcile_once()
        self.assertFalse(busy)
        rec = self.store.get_update(uid)
        self.assertEqual(rec["status"], "updated")
        self.assertIn("commit", [p["phase"] for p in rec["phases"]])
        self.assertTrue(rec["finishedAt"])
        self.assertIsNone(self.d.by_name(f"kowan-server-updater-{uid}"), "helper removed after reconcile")
        st = await self.m.status("server")
        self.assertTrue(st["canRollback"], st)
        self.assertEqual(st["previous"]["version"], "1.0")
        self.assertEqual(st["version"], "2.0")
        self.assertEqual(st["last"]["id"], uid)

        # manual rollback: swaps back and restores the snapshot of THAT update
        rid = await self.m.start("server", mode="rollback", user="admin")
        rrec = await self.drive(rid)
        self.assertEqual([p["phase"] for p in rrec["phases"]], ["lock", "resolve", "snapshot", "launch"])
        restores = []
        res, plan = await self.run_helper(self.d, self.ops, "server", rid,
                                          restore_db=lambda db, s, a: restores.append(s) or {})
        self.assertEqual(plan["db"]["swapRestore"], plan["db"]["swapRestore"])
        self.assertTrue(plan["db"]["swapRestore"].endswith(f"pre-update-{uid}.db"))
        self.assertEqual(restores, [plan["db"]["swapRestore"]])
        await Reconciler(self.svc, self.m).reconcile_once()
        self.assertEqual(self.store.get_update(rid)["status"], "updated")
        self.assertEqual(self.d.by_name("kowan-server")["Id"], self.server)

    async def test_failed_preflight_aborts_untouched(self):
        self.ops.preflight_exit = 1
        self.ops.preflight_out = '{"ok":false,"checks":[{"name":"database","ok":false,"error":"no such column"}]}'
        uid = await self.m.start("server", image="kowan:v2")
        rec = await self.drive(uid)
        self.assertEqual(rec["status"], "aborted")
        self.assertIn("no such column", rec["error"])
        self.assertFalse(any(c[0].startswith("kowan-server-updater") for c in self.ops.created))
        self.assertTrue(self.d.by_name("kowan-server")["State"]["Running"])
        # lock released
        uid2 = await self.m.start("server", image="kowan:v2")
        self.assertNotEqual(uid, uid2)
        await self.drive(uid2)

    async def test_pull_failure_without_local_image_aborts(self):
        self.ops.pull_error = "manifest unknown"
        rec = await self.drive(await self.m.start("server", image="kowan:v9"))
        self.assertEqual(rec["status"], "aborted")
        self.assertIn("manifest unknown", rec["error"])

    async def test_pull_failure_with_local_image_continues(self):
        self.ops.pull_error = "registry down"
        rec = await self.drive(await self.m.start("server", image="kowan:v2"))
        self.assertEqual(rec["status"], "running")
        self.assertIn("registry down", rec["phases"][2]["detail"])

    async def test_arch_mismatch_aborts(self):
        self.d.images["kowan:v2"]["Architecture"] = "amd64"
        rec = await self.drive(await self.m.start("server", image="kowan:v2"))
        self.assertEqual(rec["status"], "aborted")
        self.assertIn("architecture", rec["error"])

    async def test_same_image_aborts(self):
        rec = await self.drive(await self.m.start("server"))
        self.assertEqual(rec["status"], "aborted")
        self.assertIn("already up to date", rec["error"])

    async def test_helper_create_failure_is_failed_nothing_touched(self):
        orig = self.ops.create_container

        async def create(name, spec, start):
            if name.startswith("kowan-server-updater"):
                raise RuntimeError("no space left")
            return await orig(name, spec, start)

        self.ops.create_container = create
        rec = await self.drive(await self.m.start("server", image="kowan:v2"))
        self.assertEqual(rec["status"], "failed")
        self.assertIn("nothing was changed", rec["error"])

    async def test_unsupported_reasons(self):
        self.d.containers[self.server]["Mounts"] = self.d.containers[self.server]["Mounts"][:1]
        st = await self.m.status("server")
        self.assertFalse(st["supported"])
        self.assertIn("not on a volume", st["reason"])
        with self.assertRaises(UpdateError):
            await self.m.start("server", image="kowan:v2")
        self.d.containers[self.server]["Mounts"] = []
        self.assertIn("Docker socket", (await self.m.status("server"))["reason"])

    async def test_rollback_without_previous_is_409(self):
        with self.assertRaises(UpdateError) as cm:
            await self.m.start("server", mode="rollback")
        self.assertEqual(cm.exception.status, 409)

    async def test_auto_rollback_recorded(self):
        self.d.add_image("kowan:v3", "crash", version="3.0")
        uid = await self.m.start("server", image="kowan:v3")
        await self.drive(uid)
        res, _ = await self.run_helper(self.d, self.ops, "server", uid)
        self.assertEqual(res["result"], "rolled_back")
        await Reconciler(self.svc, self.m).reconcile_once()
        rec = self.store.get_update(uid)
        self.assertEqual(rec["status"], "rolled_back")
        self.assertIn("Traceback: boom", rec["logs"])
        self.assertEqual(self.d.by_name("kowan-server")["Id"], self.server)


class TestReconcile(_Base):
    async def test_restored_db_record_is_rebuilt_from_plan(self):
        uid = await self.m.start("server", image="kowan:v2")
        await self.drive(uid)
        # simulate the DB snapshot restore: record as it was before the launch
        rec = self.store.get_update(uid)
        rec["phases"] = [p for p in rec["phases"] if p["phase"] in ("lock", "resolve", "pull")]
        rec["toVersion"] = ""
        self.store.save_update(rec)
        await self.run_helper(self.d, self.ops, "server", uid)
        await Reconciler(self.svc, self.m).reconcile_once()
        rec = self.store.get_update(uid)
        self.assertEqual(rec["status"], "updated")
        names = [p["phase"] for p in rec["phases"]]
        self.assertEqual(names[:7], ["lock", "resolve", "pull", "platform", "snapshot", "preflight", "launch"])
        self.assertEqual(rec["toVersion"], "2.0")

    async def test_missing_record_is_rebuilt_from_orphan_helper(self):
        uid = await self.m.start("server", image="kowan:v2", user="admin")
        await self.drive(uid)
        await self.run_helper(self.d, self.ops, "server", uid)
        self.store._execute("DELETE FROM updates WHERE id = ?", (uid,))  # DB restored to older snapshot
        await Reconciler(self.svc, self.m).reconcile_once()
        rec = self.store.get_update(uid)
        self.assertEqual(rec["status"], "updated")
        self.assertEqual(rec["startedBy"], "admin")
        self.assertEqual(rec["phases"][0]["phase"], "lock")
        self.assertIsNone(self.d.by_name(f"kowan-server-updater-{uid}"))

    async def test_leftover_preflight_and_final_helper_removed(self):
        from kowan.models.deployment import ContainerSpec

        await self.ops.create_container("kowan-server-preflight-zz", ContainerSpec(
            image="kowan:v2", labels={"kowan.role": "server-preflight", "kowan.update.id": "zz"}), True)
        self.store.save_update({"id": "done1", "component": "server", "status": "updated",
                                "mode": "update", "startedAt": "2026-01-01T00:00:00Z"})
        hid = await self.ops.create_container("kowan-server-updater-done1", ContainerSpec(
            image="kowan:v1", labels={"kowan.role": "server-updater", "kowan.update.id": "done1"}), True)
        self.d.containers[hid]["State"] = {"Status": "exited", "Running": False, "ExitCode": 0}
        await Reconciler(self.svc, self.m).reconcile_once()
        self.assertIsNone(self.d.by_name("kowan-server-preflight-zz"))
        self.assertIsNone(self.d.by_name("kowan-server-updater-done1"))
        self.assertEqual(self.store.get_update("done1")["status"], "updated")

    async def test_running_helper_waits(self):
        uid = await self.m.start("server", image="kowan:v2")
        await self.drive(uid)
        self.assertTrue(await Reconciler(self.svc, self.m).reconcile_once())
        self.assertEqual(self.store.get_update(uid)["status"], "running")

    async def test_not_launched_is_aborted_after_grace(self):
        rec = {"id": "x1", "component": "server", "status": "running", "mode": "update",
               "startedAt": "2020-01-01T00:00:00Z", "phases": [{"phase": "pull", "ok": True}]}
        self.store.save_update(rec)
        await Reconciler(self.svc, self.m).reconcile_once()
        self.assertEqual(self.store.get_update("x1")["status"], "aborted")

    async def test_live_record_is_skipped(self):
        rec = {"id": "x2", "component": "server", "status": "running", "mode": "update",
               "startedAt": "2020-01-01T00:00:00Z", "phases": []}
        self.store.save_update(rec)
        self.m._busy.add("x2")
        self.assertTrue(await Reconciler(self.svc, self.m).reconcile_once())
        self.assertEqual(self.store.get_update("x2")["status"], "running")

    async def test_unreachable_agent_becomes_unknown_then_recovers(self):
        rec = {"id": "a1", "component": "agent", "hostId": "h1", "status": "running", "mode": "update",
               "startedAt": "2020-01-01T00:00:00Z",
               "phases": [{"phase": "launch", "ok": True}]}
        self.store.save_update(rec)
        r = Reconciler(self.svc, self.m, clock=lambda: rc.parse_ts("2020-01-01T00:05:00Z"))
        self.assertTrue(await r.reconcile_once())
        r.clock = lambda: rc.parse_ts("2020-01-01T00:11:00Z")
        await r.reconcile_once()
        self.assertEqual(self.store.get_update("a1")["status"], "unknown")
        self.assertIn("did not reconnect", self.store.get_update("a1")["error"])

    def test_fold_without_result_is_failed(self):
        out = fold_result({"id": "z", "component": "server", "status": "running", "phases": []},
                          None, '{"ts":"t","id":"z","phase":"stop","ok":true}\nSegmentation fault', 139)
        self.assertEqual(out["status"], "failed")
        self.assertIn("phase 'stop'", out["error"])
        self.assertIn("Segmentation fault", out["logs"])

    def test_fold_failed_includes_operator_steps(self):
        out = fold_result({"id": "z", "component": "server", "status": "running", "phases": []}, None,
                          result_line({"result": "failed", "error": "e", "operator": ["docker rename a b"],
                                       "logs": ["x"]}), 2)
        self.assertEqual(out["logs"][0], "docker rename a b")


class TestAgentUpdate(_Base):
    async def test_agent_update_and_batch(self):
        self.hub.up.add("h1")
        h = self.store.get_host("h1")
        st = await self.m.status("agent", h)
        self.assertTrue(st["supported"], st)
        uid = await self.m.start("agent", h, image="ghcr.io/kowan-lab/kowan-agent:v2")
        rec = await self.drive(uid)
        self.assertEqual(rec["hostId"], "h1")
        self.assertNotIn("snapshot", [p["phase"] for p in rec["phases"]])
        name, spec, _ = [c for c in self.aops.created if c[0].startswith("kowan-agent-updater")][0]
        self.assertEqual([v.target for v in spec.volumes], ["/var/run/docker.sock"])
        self.assertEqual(spec.entrypoint, ["python", "-m", "kowan.agent"])
        await self.run_helper(self.adir, self.aops, "agent", uid)
        await Reconciler(self.svc, self.m).reconcile_once()
        self.assertEqual(self.store.get_update(uid)["status"], "updated")

    async def test_agent_found_by_token_among_several(self):
        self.hub.up.add("h1")
        # a second agent on the same daemon with another token, and ours renamed
        self.adir.add_container("other-agent", "ghcr.io/kowan-lab/kowan-agent:v1", env=["KOWAN_AGENT_TOKEN=nope"])
        ours = self.adir.by_name("kowan-agent")
        ours["Name"] = "/my-agent"
        ours["Config"]["Env"].append("KOWAN_AGENT_TOKEN=t")
        target, reason = await self.m.resolve_target("agent", self.store.get_host("h1"))
        self.assertIsNotNone(target, reason)
        self.assertEqual(target.name, "my-agent")

    async def test_aborted_update_drops_its_snapshot(self):
        self.ops.preflight_exit = 1
        uid = await self.m.start("server", image="kowan:v2")
        await self.drive(uid)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "backups", f"pre-update-{uid}.db")))

    async def test_disconnected_agent_is_unsupported_fast(self):
        st = await self.m.status("agent", self.store.get_host("h1"))
        self.assertFalse(st["supported"])
        self.assertIn("not connected", st["reason"])

    async def test_not_agent_host(self):
        st = await self.m.status("agent", self.store.get_host("local"))
        self.assertFalse(st["supported"])

    async def test_batch_stops_at_first_failure(self):
        self.hub.up.add("h1")
        self.store.create_host(_host("h2"))
        self.svc.docker.ops_by_host["h2"] = self.aops
        h1, h2 = self.store.get_host("h1"), self.store.get_host("h2")
        self.aops.preflight_exit = 1
        with mock.patch("kowan.updater.orchestrator.BATCH_STEP_TIMEOUT", 5):
            res = await self.m.start_batch([h1, h2], image="ghcr.io/kowan-lab/kowan-agent:v2", user="admin")
            self.assertEqual(len(res["ids"]), 2)
            self.assertEqual(self.store.get_update(res["ids"][1])["status"], "running")
            await self.m._tasks[res["batchId"]]
        a, b = (self.store.get_update(i) for i in res["ids"])
        self.assertEqual(a["status"], "aborted")
        self.assertEqual(b["status"], "aborted")
        self.assertIn("rolling update stopped", b["error"])
        self.assertEqual(a["batchId"], res["batchId"])

    async def test_batch_conflict(self):
        h1 = self.store.get_host("h1")
        self.hub.up.add("h1")
        await self.m.start("agent", h1, image="ghcr.io/kowan-lab/kowan-agent:v2")
        with self.assertRaises(UpdateError) as cm:
            await self.m.start_batch([h1])
        self.assertEqual(cm.exception.status, 409)
        await asyncio.gather(*self.m._tasks.values())


if __name__ == "__main__":
    unittest.main()
