"""Volume backups end to end on the fake Engine: the restic helper container is
"run" by the fake (its output comes from the recorded fixtures), so the REST API,
the service, the executor (create/start/poll/logs/remove) and the parsers all run
for real: repository test, a job run, a one-off volume backup, snapshots, ls and
a restore into a new volume."""

import asyncio
import os
import unittest

from kowan.volbackup.restic import RESTIC_IMAGE, strip_timestamps
from tests.apicase import ServerCase

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "volbackup")


def fixture(name):
    # The recorded logs carry Docker's timestamps; the fake Engine adds its own.
    with open(os.path.join(FIX, name)) as f:
        return strip_timestamps(f.read().splitlines())


def restic(c):
    """What the restic helper prints, by the script it was given."""
    script = " ".join(c["Config"].get("Cmd") or [])
    if "r backup" in script:
        return 0, fixture("backup_ok.log")
    if "r restore" in script:
        return 0, fixture("restore_ok.log")
    if "r ls" in script:
        return 0, fixture("ls.ndjson") + ["@@kowan rc ls list 0"]
    if "r snapshots" in script:
        return 0, ["\n".join(fixture("snapshots.json")), "@@kowan rc snapshots list 0"]
    return 0, ["@@kowan phase open", "@@kowan initialized", "@@kowan done"]


class VolBackupCase(ServerCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.engine.add_image(RESTIC_IMAGE)
        self.engine.add_volume("app-data")
        self.engine.add_volume("db-data")
        self.engine.runners.append(("restic/restic", restic))
        _, self.repo = await self.call("POST", "/volbackups/repos", "admin", json={
            "name": "disk", "type": "local", "hostId": "local", "config": {"path": "/srv/backups"},
            "secrets": {"password": "pw-123"}}, expect=201)

    async def wait_run(self, run_id):
        for _ in range(200):
            _, run = await self.call("GET", f"/volbackups/runs/{run_id}", "viewer", expect=200)
            if run["status"] not in ("running", "queued", "pending"):
                return run
            await asyncio.sleep(0.05)
        self.fail("backup run did not finish")


class TestFlows(VolBackupCase):
    async def test_repo_test_job_run_and_restore(self):
        _, res = await self.call("POST", f"/volbackups/repos/{self.repo['id']}/test", "operator", json={}, expect=200)
        self.assertTrue(res["ok"], res)
        self.assertNotIn("pw-123", res["log"])

        _, job = await self.call("POST", "/volbackups/jobs", "operator", json={
            "name": "nightly", "hostId": "local", "repoId": self.repo["id"],
            "selector": {"mode": "names", "volumes": ["app-data", "db-data"]},
            "schedule": "0 3 * * *", "timezone": "UTC", "retention": {"keepDaily": 7}}, expect=201)
        _, run = await self.call("POST", f"/volbackups/jobs/{job['id']}/run", "operator", expect=202)
        done = await self.wait_run(run["id"])
        self.assertEqual(done["status"], "warning", done)  # db-data had a permission error in the fixture
        self.assertEqual(done["bytesAdded"], 1750 + 4096)
        # The helper container never outlives the run.
        self.assertFalse([c for c in self.engine.containers.values() if c["Config"]["Image"] == RESTIC_IMAGE])
        _, got = await self.call("GET", f"/volbackups/jobs/{job['id']}", "viewer", expect=200)
        self.assertEqual(got["lastRun"]["id"], run["id"])
        _, runs = await self.call("GET", "/volbackups/runs", "viewer", params={"jobId": job["id"]}, expect=200)
        self.assertEqual(len(runs), 1)
        _, jobs = await self.call("GET", "/volbackups/jobs", "viewer", params={"volume": "app-data"}, expect=200)
        self.assertEqual(len(jobs), 1)
        _, upd = await self.call("PUT", f"/volbackups/jobs/{job['id']}", "operator", json={
            "name": "nightly", "hostId": "local", "repoId": self.repo["id"],
            "selector": {"mode": "names", "volumes": ["app-data"]}, "schedule": "0 4 * * *",
            "timezone": "UTC", "enabled": False}, expect=200)
        self.assertFalse(upd["enabled"])

        _, snaps = await self.call("GET", f"/volbackups/repos/{self.repo['id']}/snapshots", "operator", expect=200)
        self.assertEqual([s["shortId"] for s in snaps], ["bbbb93e9", "4fdd93e9"])
        _, listing = await self.call("GET", f"/volbackups/repos/{self.repo['id']}/snapshots/4fdd93e9/ls", "operator",
                                     params={"path": "/data/kowan-f3-fixture"}, expect=200)
        self.assertEqual([e["name"] for e in listing["entries"]], ["sub", "a.txt"])
        _, vs = await self.call("GET", "/volbackups/volumes/local/app-data/snapshots", "operator", expect=200)
        self.assertTrue(vs)

        _, rr = await self.call("POST", "/volbackups/restore", "operator", json={
            "hostId": "local", "repoId": self.repo["id"], "snapshotId": "4fdd93e9", "target": "restored",
            "newVolume": True}, expect=202)
        restored = await self.wait_run(rr["id"])
        self.assertEqual(restored["status"], "success", restored)
        self.assertIn("restored", self.engine.volumes)

    async def test_one_off_volume_backup_and_validation(self):
        _, run = await self.call("POST", "/volbackups/volumes/local/app-data/backup", "operator",
                                 json={"repoId": self.repo["id"], "tags": ["manual"]}, expect=202)
        await self.wait_run(run["id"])
        await self.call("POST", "/volbackups/volumes/local/app-data/backup", "operator", json={}, expect=400)
        await self.call("GET", "/volbackups/jobs/missing", "viewer", expect=404)
        await self.call("GET", "/volbackups/runs/missing", "viewer", expect=404)
        await self.call("POST", "/volbackups/jobs/missing/run", "operator", expect=404)
        await self.call("GET", "/volbackups/runs", "viewer", params={"limit": "x"}, expect=400)
        await self.call("POST", "/volbackups/restore", "operator", json={"hostId": "local", "bogus": 1}, expect=400)
        _, info = await self.call("GET", "/volbackups/info", "viewer", expect=200)
        self.assertTrue(info)


class TestHooksAndFailures(VolBackupCase):
    async def test_stack_job_with_hooks_and_failed_restore(self):
        db = self.engine.add_container("shop-db-1", "postgres:16", binds=["app-data:/var/lib/postgresql/data"],
                                       labels={"com.docker.compose.project": "shop"})
        ran = []

        def hook_exec(cid, argv):
            if "pg_dump" in " ".join(argv):
                ran.append(argv)
                return 0, b"dumped\n@@exit=0\n"  # what the wrapping `sh -c` prints
            return None

        self.engine.exec_handler = hook_exec
        _, job = await self.call("POST", "/volbackups/jobs", "operator", json={
            "name": "shop", "hostId": "local", "repoId": self.repo["id"],
            "selector": {"mode": "stack", "stack": "shop"}, "schedule": "0 3 * * *", "timezone": "UTC",
            "hooks": {"stopContainers": True, "pre": [{"container": "shop-db-1", "command": "pg_dump -f /tmp/x"}],
                      "post": [{"container": "missing", "command": "true"}]}}, expect=201)
        _, pv = await self.call("POST", "/volbackups/jobs/preview", "operator", json={
            "hostId": "local", "selector": {"mode": "stack", "stack": "shop"}}, expect=200)
        self.assertEqual(pv["volumes"], ["app-data"])
        _, run = await self.call("POST", f"/volbackups/jobs/{job['id']}/run", "operator", expect=202)
        await self.call("POST", f"/volbackups/jobs/{job['id']}/run", "operator", expect=409)  # one at a time
        done = await self.wait_run(run["id"])
        self.assertTrue(ran, done)
        self.assertEqual(done["status"], "warning", done)  # the post hook's container is missing
        self.assertTrue(self.engine.containers[db]["State"]["Running"])  # restarted after the backup

        def failing(c):
            script = " ".join(c["Config"].get("Cmd") or [])
            if "r restore" in script or "r snapshots --no-lock --json 4fdd" in script:
                return 1, ["@@kowan phase lookup", "Fatal: no matching ID found", "@@kowan rc lookup snapshot 1"]
            return restic(c)

        self.engine.runners[:] = [("restic/restic", failing)]
        _, rr = await self.call("POST", "/volbackups/restore", "operator", json={
            "hostId": "local", "repoId": self.repo["id"], "snapshotId": "4fdd93e9", "target": "app-data"},
            expect=202)
        failed = await self.wait_run(rr["id"])
        self.assertEqual(failed["status"], "failed", failed)
        self.assertIn("no matching ID", failed["error"])
        await self.call("POST", "/volbackups/restore", "operator", json={
            "hostId": "local", "repoId": self.repo["id"], "snapshotId": "zz", "target": "app-data"}, expect=400)
        await self.call("POST", "/volbackups/restore", "operator", json={
            "hostId": "local", "repoId": self.repo["id"], "snapshotId": "4fdd93e9", "target": "../x"}, expect=400)

    async def test_local_repo_is_bound_to_its_host(self):
        _, created = await self.call("POST", "/hosts", "admin", json={"name": "far", "mode": "edge"}, expect=201)
        far = created["host"]["id"]
        status, body = await self.call("POST", "/volbackups/jobs", "operator", json={
            "name": "x", "hostId": far, "repoId": self.repo["id"], "selector": {"mode": "label"}})
        self.assertEqual(status, 400, body)
        await self.call("POST", f"/volbackups/volumes/{far}/data/backup", "operator",
                        json={"repoId": self.repo["id"]}, expect=400)
        _, snaps = await self.call("GET", "/volbackups/volumes/local/nothing/snapshots", "operator", expect=200)
        self.assertIsInstance(snaps, (list, dict))


if __name__ == "__main__":
    unittest.main()
