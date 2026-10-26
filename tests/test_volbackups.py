"""Volume backups: restic env/retention/scripts, JSON parsing from fixtures,
volume selection, hooks, backup/restore flows with a fake helper, failure
alerts and the HTTP API."""

import asyncio
import datetime as dt
import os
import tempfile
import unittest

from aiohttp.test_utils import TestClient, TestServer

from kowan import errors
from kowan.auth import new_jwt
from kowan.config import ServerConfig
from kowan.models import Container, Host, Volume
from kowan.secrets import Cipher
from kowan.store import Store
from kowan.volbackup import restic
from kowan.volbackup import service as vb
from kowan.volbackup.executor import HelperResult
from kowan.web import Services, create_app

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "volbackup")


def fixture(name):
    with open(os.path.join(FIX, name)) as f:
        return f.read().splitlines()



def _ensure_user(store, role):
    """Tokens are checked against the users table: mint them for real users."""
    from kowan.models import User

    uid = "u-" + role
    try:
        store.get_user(uid)
    except Exception:  # noqa: BLE001
        store.create_user(User(id=uid, username=role, role=role, passwordHash="x",
                               createdAt="2026-01-01T00:00:00Z"))
    return uid

class TestResticHelpers(unittest.TestCase):
    def test_retention_flags(self):
        self.assertEqual(restic.retention_flags({"keepDaily": 7, "keepLast": 3, "keepWeekly": 0, "keepMonthly": "6"}),
                         ["--keep-last", "3", "--keep-daily", "7", "--keep-monthly", "6"])
        self.assertEqual(restic.retention_flags({}), [])
        self.assertEqual(restic.retention_flags({"keepDaily": "x"}), [])
        self.assertEqual(restic.normalize_retention({"keepDaily": "7", "keepLast": 0}), {"keepDaily": 7})
        for bad in ({"keepDaily": -1}, {"keepForever": 1}, {"keepDaily": "x"}, [1]):
            with self.assertRaises(ValueError):
                restic.normalize_retention(bad)

    def test_repo_runtime_types(self):
        s3 = restic.repo_runtime({"type": "s3", "config": {"endpoint": "http://minio:9000", "bucket": "b", "prefix": "/p/", "region": "eu", "network": "net"}},
                                 {"password": "pw", "accessKeyId": "ak", "secretAccessKey": "sk"})
        self.assertEqual(s3.env["RESTIC_REPOSITORY"], "s3:http://minio:9000/b/p")
        self.assertEqual((s3.env["AWS_ACCESS_KEY_ID"], s3.env["AWS_SECRET_ACCESS_KEY"], s3.env["AWS_DEFAULT_REGION"]), ("ak", "sk", "eu"))
        self.assertEqual(s3.network, "net")
        loc = restic.repo_runtime({"type": "local", "config": {"path": "/srv/backups"}}, {"password": "pw"})
        self.assertEqual((loc.env["RESTIC_REPOSITORY"], loc.binds), ("/repo", [("/srv/backups", "/repo", False)]))
        sftp = restic.repo_runtime({"type": "sftp", "config": {"host": "nas", "user": "bk", "path": "/r", "port": "2222"}}, {"password": "pw", "sshKey": "KEY"})
        self.assertEqual(sftp.env["RESTIC_REPOSITORY"], "sftp:bk@nas:/r")
        self.assertIn("-p 2222", sftp.env["KOWAN_SFTP_CMD"])
        self.assertIn("-i /tmp/id_port", sftp.env["KOWAN_SFTP_CMD"])
        rest = restic.repo_runtime({"type": "rest", "config": {"url": "https://rs:8000/repo", "username": "u@x"}}, {"password": "pw", "restPassword": "p:w"})
        self.assertEqual(rest.env["RESTIC_REPOSITORY"], "rest:https://u%40x:p%3Aw@rs:8000/repo")
        for repo, sec in (
            ({"type": "s3", "config": {}}, {"password": "p"}),
            ({"type": "local", "config": {"path": "rel"}}, {"password": "p"}),
            ({"type": "local", "config": {"path": "/x"}}, {}),
            ({"type": "rest", "config": {"url": "ftp://x"}}, {"password": "p"}),
            ({"type": "nfs", "config": {}}, {"password": "p"}),
        ):
            with self.assertRaises(restic.RepoConfigError):
                restic.repo_runtime(repo, sec)

    def test_backup_script(self):
        s = restic.backup_script(["a", "b"], job_id="j1", host_label="local", tags=["nightly", "bad,tag"], retention={"keepDaily": 7})
        self.assertIn("--tag kowan-volume=a", s)
        self.assertIn("--tag kowan-job=j1", s)
        self.assertIn("--tag nightly", s)
        self.assertNotIn("bad,tag", s)
        self.assertIn("/data/b", s)
        self.assertIn("forget --json --prune --tag kowan-job=j1 --group-by host,paths --keep-daily 7", s)
        self.assertNotIn("forget", restic.backup_script(["a"], job_id="j", host_label="h", tags=[], retention={}))
        with self.assertRaises(ValueError):
            restic.backup_script(["../etc"], job_id="j", host_label="h", tags=[], retention={})
        with self.assertRaises(ValueError):
            restic.restore_script("not-hex; rm -rf /", delete=False)
        self.assertIn("--delete", restic.restore_script("4fdd93e9", delete=True))

    def test_parse_backup_fixture(self):
        lines = restic.strip_timestamps(fixture("backup_ok.log"))
        parsed = restic.parse_markers(lines)
        self.assertTrue(parsed.initialized and parsed.done)
        self.assertEqual(parsed.section("backup", "app-data").rc, 0)
        self.assertEqual(parsed.section("backup", "db-data").rc, 3)
        summ, errs = restic.backup_summary(parsed.section("backup", "app-data").lines)
        st = restic.stats_from_summary(summ)
        self.assertEqual((st["filesNew"], st["bytesAdded"], st["filesTotal"]), (2, 1750, 2))
        self.assertTrue(st["snapshotId"].startswith("4fdd93e9"))
        out = vb.summarize_backup(parsed, ["app-data", "db-data"], 3)
        self.assertEqual(out["status"], "warning")
        self.assertEqual(out["bytesAdded"], 1750 + 4096)
        self.assertEqual(out["stats"]["filesTotal"], 18)
        self.assertEqual(len(out["snapshots"]), 2)
        self.assertIn("permission denied", out["error"])
        self.assertNotIn("percent_done", restic.clean_log(lines))

    def test_summarize_failures(self):
        lines = ["@@kowan phase open", "Fatal: wrong password or no key found", "@@kowan rc open repo 12"]
        out = vb.summarize_backup(restic.parse_markers(lines), ["a"], 21)
        self.assertEqual(out["status"], "failed")
        self.assertIn("wrong password", out["error"])
        lines = ["@@kowan phase open", "@@kowan phase backup a", "Fatal: unable to save snapshot", "@@kowan rc backup a 1",
                 "@@kowan phase forget", "@@kowan rc forget all 1"]
        out = vb.summarize_backup(restic.parse_markers(lines), ["a", "b"], 0)
        self.assertEqual(out["status"], "failed")
        self.assertIn("b: backup did not run", out["error"])
        lines = ["@@kowan phase backup a", '{"message_type":"summary","data_added":1,"snapshot_id":"ab"}', "@@kowan rc backup a 0",
                 "@@kowan phase forget", "@@kowan rc forget all 1"]
        self.assertEqual(vb.summarize_backup(restic.parse_markers(lines), ["a"], 0)["status"], "warning")

    def test_parse_snapshots_ls_restore(self):
        snaps = restic.parse_snapshots(fixture("snapshots.json"))
        self.assertEqual([s["shortId"] for s in snaps], ["bbbb93e9", "4fdd93e9"])
        self.assertEqual(snaps[1]["volume"], "kowan-f3-fixture")
        self.assertEqual(snaps[1]["jobId"], "j1")
        self.assertEqual(snaps[1]["tags"], ["nightly"])
        self.assertEqual(snaps[1]["sizeBytes"], 12)
        entries, trunc = restic.parse_ls(fixture("ls.ndjson"), "/data/kowan-f3-fixture")
        self.assertEqual([e["name"] for e in entries], ["sub", "a.txt"])
        self.assertFalse(trunc)
        self.assertEqual(entries[1]["size"], 6)
        _, trunc = restic.parse_ls(fixture("ls.ndjson"), "/data/kowan-f3-fixture", limit=1)
        self.assertTrue(trunc)
        parsed = restic.parse_markers(fixture("restore_ok.log"))
        self.assertEqual(parsed.section("restore").rc, 0)
        self.assertEqual(restic.restore_summary(parsed.section("restore").lines)["bytesRestored"], 12)

    def test_redact_and_hooks(self):
        self.assertEqual(restic.redact("pw=supersecret!", ["supersecret!"]), "pw=********")
        self.assertEqual(vb.parse_hook_output("dumped\n@@exit=0\n"), (True, "dumped"))
        ok, text = vb.parse_hook_output("boom\n@@exit=2")
        self.assertFalse(ok)
        self.assertFalse(vb.parse_hook_output("no marker")[0])
        self.assertEqual(vb.hook_command("pg_dump x")[:2], ["sh", "-c"])


class TestValidation(unittest.TestCase):
    def test_job(self):
        j = vb.validate_job({"name": "n", "hostId": "local", "repoId": "r", "selector": {"mode": "names", "volumes": ["b", "a", "a"]},
                             "schedule": "0 3 * * *", "timezone": "Europe/Rome", "retention": {"keepDaily": 7},
                             "hooks": {"stopContainers": True, "pre": [{"container": "/db", "command": "pg_dump"}]}, "tags": ["x"]})
        self.assertEqual(j["selector"]["volumes"], ["a", "b"])
        self.assertEqual(j["hooks"]["pre"][0]["container"], "db")
        bad = [
            {"name": "", "hostId": "h", "repoId": "r", "selector": {"mode": "names", "volumes": ["a"]}},
            {"name": "n", "hostId": "h", "repoId": "r", "selector": {"mode": "names", "volumes": []}},
            {"name": "n", "hostId": "h", "repoId": "r", "selector": {"mode": "deployment"}},
            {"name": "n", "hostId": "h", "repoId": "r", "selector": {"mode": "nope"}},
            {"name": "n", "hostId": "h", "repoId": "r", "selector": {"mode": "label"}, "schedule": "bad"},
            {"name": "n", "hostId": "h", "repoId": "r", "selector": {"mode": "label"}, "tags": ["kowan-job=x"]},
            {"name": "n", "hostId": "h", "repoId": "r", "selector": {"mode": "label"}, "hooks": {"pre": [{"container": "x"}]}},
            {"name": "n", "hostId": "h", "repoId": "r", "selector": {"mode": "label"}, "retention": {"keepDaily": -2}},
        ]
        base = {"name": "n", "hostId": "h", "repoId": "r", "selector": {"mode": "label"}}
        bad += [
            [1], {**base, "colour": "red"}, {**base, "repoId": ""}, {**base, "selector": "x"},
            {**base, "selector": {"mode": "names", "volumes": ["../x"]}},
            {**base, "selector": {"mode": "stack"}}, {**base, "selector": {"mode": "label", "exclude": "x"}},
            {**base, "timezone": "Mars/Base"}, {**base, "hooks": "x"}, {**base, "hooks": {"during": []}},
            {**base, "hooks": {"pre": "x"}}, {**base, "hooks": {"pre": ["x"]}},
            {**base, "hooks": {"pre": [{"container": "c", "command": "x" * 5000}]}},
            {**base, "tags": "x"}, {**base, "enabled": "yes"},
        ]
        for b in bad:
            with self.assertRaises(errors.Validation, msg=str(b)):
                vb.validate_job(b)
        ok = vb.validate_job({**base, "selector": {"mode": "stack", "stack": "shop", "exclude": ["b", "a", "a"]}})
        self.assertEqual(ok["selector"]["exclude"], ["a", "b"])
        ok = vb.validate_job({**base, "selector": {"mode": "deployment", "deploymentId": "d1"}})
        self.assertEqual(ok["selector"]["deploymentId"], "d1")

    def test_repo_refusals(self):
        base = {"name": "r", "type": "local", "hostId": "local", "config": {"path": "/x"}}
        for b in ([1], {**base, "colour": 1}, {**base, "name": ""}, {**base, "type": "ftp"},
                  {**base, "config": "x"}, {**base, "config": {"bucket": "b"}}, {**base, "hostId": ""},
                  {**base, "secrets": "x"}, {**base, "secrets": {"apiKey": "k"}}):
            with self.assertRaises(errors.Validation, msg=str(b)):
                vb.validate_repo(b)
        with self.assertRaises(errors.Validation):
            vb.validate_repo({"type": "s3"}, existing={**base, "type": "local"})

    def test_repo(self):
        repo, sec = vb.validate_repo({"name": "r", "type": "local", "hostId": "local", "config": {"path": "/x"}, "secrets": {"password": "p"}})
        self.assertEqual((repo["config"], sec), ({"path": "/x"}, {"password": "p"}))
        repo, _ = vb.validate_repo({"name": "r", "type": "s3", "hostId": "local", "config": {"bucket": "b"}})
        self.assertEqual(repo["hostId"], "")
        for b in ({"name": "r", "type": "ftp"}, {"name": "r", "type": "local", "config": {"path": "/x"}},
                  {"name": "r", "type": "s3", "config": {"path": "/x"}}, {"name": "r", "type": "s3", "secrets": {"sshKey": "k"}}):
            with self.assertRaises(errors.Validation):
                vb.validate_repo(b)
        with self.assertRaises(errors.Validation):
            vb.validate_repo({"type": "s3"}, {"name": "r", "type": "local", "config": {}})


class FakeOps:
    def __init__(self):
        self.volumes = [Volume(name="app-data", labels={"kowan.backup": "true"}),
                        Volume(name="shop_db", labels={"com.docker.compose.project": "shop"}),
                        Volume(name="other")]
        self.containers = [
            Container(id="c1", names=["/web"], state="running", labels={"kowan.deployment.id": "d1"}),
            Container(id="c2", names=["/db"], state="running", labels={"com.docker.compose.project": "shop", "kowan.backup": "yes"}),
            Container(id="c3", names=["/idle"], state="exited", labels={}),
        ]
        self.mounts = {"c1": ["web-static", "app-data"], "c2": ["shop_db"], "c3": ["other"]}
        self.calls = []
        self.exec_out = "ok\n@@exit=0\n"
        self.created = []

    async def list_volumes(self):
        return self.volumes

    async def list_containers(self, all):
        return [c for c in self.containers if all or c.state == "running"]

    async def inspect_container(self, id):
        return {"Mounts": [{"Type": "volume", "Name": n} for n in self.mounts[id]] + [{"Type": "bind", "Source": "/x"}]}

    async def stop_container(self, id):
        self.calls.append(("stop", id))

    async def start_container(self, id):
        self.calls.append(("start", id))

    async def exec(self, id, cmd):
        self.calls.append(("exec", id, cmd[2].splitlines()[0]))
        return self.exec_out

    async def inspect_volume(self, name):
        if name in [v.name for v in self.volumes]:
            return {"Name": name}
        raise errors.NotFound("no such volume")

    async def create_volume(self, opts):
        self.volumes.append(Volume(name=opts.name))
        self.created.append(opts.name)
        return {"Name": opts.name}


class TestSelection(unittest.IsolatedAsyncioTestCase):
    async def test_modes(self):
        ops = FakeOps()
        self.assertEqual(await vb.resolve_volumes(ops, {"mode": "names", "volumes": ["x", "a"], "exclude": ["x"]}), ["a"])
        self.assertEqual(await vb.resolve_volumes(ops, {"mode": "deployment", "deploymentId": "d1"}), ["app-data", "web-static"])
        self.assertEqual(await vb.resolve_volumes(ops, {"mode": "deployment", "deploymentId": "d9"}, deployment_project="shop"), ["shop_db"])
        self.assertEqual(await vb.resolve_volumes(ops, {"mode": "stack", "stack": "shop"}), ["shop_db"])
        self.assertEqual(await vb.resolve_volumes(ops, {"mode": "label"}), ["app-data", "shop_db"])
        self.assertEqual(await vb.containers_using(ops, ["app-data"]), [("c1", "web")])
        self.assertEqual(await vb.containers_using(ops, ["other"]), [])  # c3 not running


class FakeProvider:
    def __init__(self, ops):
        self._ops = ops

    def ops(self, host):
        return self._ops


class FakeBus:
    def __init__(self):
        self.events = []

    def publish(self, e):
        self.events.append(e)


def _cfg():
    return ServerConfig(addr="127.0.0.1:0", data_dir=tempfile.mkdtemp(), jwt_secret="x" * 48,
                        monitor_interval=dt.timedelta(seconds=3600), metrics_retention=dt.timedelta(hours=1),
                        secret_key="", external_url="")


def _store():
    st = Store(":memory:")
    st.use_cipher(Cipher(b"k" * 32))
    for hid in ("local", "h2"):
        st.create_host(Host(id=hid, name=hid, type="local", mode="agent", address="", token=hid,
                            lastSeen="", createdAt="2026-01-01T00:00:00Z"))
    return st


class FakeHelper:
    def __init__(self, lines, exit_code=0):
        self.lines = lines
        self.exit_code = exit_code
        self.calls = []

    async def __call__(self, ops, script, **kw):
        self.calls.append((script, kw))
        return HelperResult(exit_code=self.exit_code, lines=list(self.lines))


class TestServiceFlows(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = _store()
        self.ops = FakeOps()
        self.bus = FakeBus()
        self.svc = Services(config=_cfg(), store=self.store, docker=FakeProvider(self.ops), bus=self.bus)
        self.repo = self.store.save_backup_repo({"name": "r", "type": "local", "hostId": "local", "config": {"path": "/srv/b"}},
                                                {"password": "sup3rsecret"})
        self.job = self.store.save_backup_job(vb.validate_job({
            "name": "nightly", "hostId": "local", "repoId": self.repo["id"],
            "selector": {"mode": "names", "volumes": ["app-data", "db-data"]}, "retention": {"keepDaily": 7},
            "hooks": {"stopContainers": True, "pre": [{"container": "db", "command": "pg_dump > /x"}], "post": [{"container": "db", "command": "rm /x"}]},
        }))

    def service(self, helper):
        return vb.VolumeBackupService(self.svc, helper=helper)

    async def test_backup_success_with_hooks(self):
        helper = FakeHelper(restic.strip_timestamps(fixture("backup_ok.log")))
        s = self.service(helper)
        run = await s.run_backup(self.job)
        self.assertEqual(run["status"], "warning")  # db-data exit 3 in the fixture
        self.assertEqual(run["bytesAdded"], 5846)
        script, kw = helper.calls[0]
        self.assertIn(("app-data", "/data/app-data", True), kw["binds"])
        self.assertIn(("/srv/b", "/repo", False), kw["binds"])
        self.assertEqual(kw["env"]["RESTIC_PASSWORD"], "sup3rsecret")
        kinds = [c[0] for c in self.ops.calls]
        self.assertEqual(kinds, ["exec", "stop", "start", "exec"])  # pre, stop web, start web, post
        self.assertTrue(self.store.get_backup_repo(self.repo["id"])["initialized"])
        saved = self.store.get_backup_run(run["id"])
        self.assertNotIn("sup3rsecret", saved["log"])
        self.assertIn("[pre-hook db] ok", saved["log"])
        self.assertEqual(self.bus.events, [])

    async def test_failure_raises_alert_then_success_resolves(self):
        s = self.service(FakeHelper(["@@kowan phase open", "Fatal: wrong password sup3rsecret", "@@kowan rc open repo 12"], 21))
        run = await s.run_backup(self.job)
        self.assertEqual(run["status"], "failed")
        self.assertNotIn("sup3rsecret", run["error"])
        self.assertEqual(len(self.bus.events), 1)
        ev = self.bus.events[0]
        self.assertEqual((ev.type, ev.payload["level"]), ("alert", "critical"))
        self.assertIn("nightly", ev.payload["title"])
        job = self.store.get_backup_job(self.job["id"])
        self.assertTrue(job["alertId"])
        alerts = self.store.list_alerts(10)
        self.assertFalse(alerts[0].resolved)
        # Restart must have happened even though restic failed.
        self.assertIn(("start", "c1"), self.ops.calls)
        s2 = self.service(FakeHelper(["@@kowan phase open", "@@kowan phase backup app-data", '{"message_type":"summary","snapshot_id":"ab"}',
                                      "@@kowan rc backup app-data 0", "@@kowan phase backup db-data", "@@kowan rc backup db-data 0",
                                      "@@kowan phase forget", "@@kowan rc forget all 0", "@@kowan done"]))
        run = await s2.run_backup(self.job)
        self.assertEqual(run["status"], "success")
        self.assertTrue(self.store.list_alerts(10)[0].resolved)
        self.assertEqual(self.store.get_backup_job(self.job["id"])["alertId"], "")

    async def test_pre_hook_failure_aborts(self):
        self.ops.exec_out = "pg_dump: error\n@@exit=1\n"
        helper = FakeHelper([])
        run = await self.service(helper).run_backup(self.job)
        self.assertEqual(run["status"], "failed")
        self.assertEqual(helper.calls, [])
        self.assertIn("pre hook", run["error"])

    async def test_no_volumes_and_wrong_host_repo(self):
        job = dict(self.job, selector={"mode": "stack", "stack": "nothing"})
        run = await self.service(FakeHelper([])).run_backup(job)
        self.assertIn("matched no volumes", run["error"])
        job = dict(self.job, hostId="h2")
        run = await self.service(FakeHelper([])).run_backup(job)
        self.assertIn("local path on host", run["error"])

    async def test_skip_records_run(self):
        from kowan.schedules.runner import CronEntry

        s = self.service(FakeHelper([]))
        s._on_skip(CronEntry(key=self.job["id"], spec="@daily", payload=self.job["id"]),
                   dt.datetime(2026, 10, 25, tzinfo=dt.timezone.utc), "missed: 15 min late")
        runs = self.store.list_backup_runs(job_id=self.job["id"])
        self.assertEqual((runs[0]["status"], runs[0]["error"]), ("skipped", "missed: 15 min late"))

    async def test_restore_in_place_and_new(self):
        helper = FakeHelper(fixture("restore_ok.log"))
        s = self.service(helper)
        run = s.start_restore(host_id="local", repo_id=self.repo["id"], snapshot_id="4fdd93e9", target="app-data", new_volume=False)
        await s.drain()
        run = self.store.get_backup_run(run["id"])
        self.assertEqual((run["kind"], run["status"]), ("restore", "success"))
        self.assertEqual(run["stats"]["filesRestored"], 3)
        self.assertIn("--delete", helper.calls[0][0])
        self.assertIn(("app-data", "/target", False), helper.calls[0][1]["binds"])
        self.assertEqual([c for c in self.ops.calls if c[0] in ("stop", "start")], [("stop", "c1"), ("start", "c1")])
        run = s.start_restore(host_id="local", repo_id=self.repo["id"], snapshot_id="4fdd93e9", target="app-copy", new_volume=True)
        await s.drain()
        self.assertEqual(self.store.get_backup_run(run["id"])["status"], "success")
        self.assertEqual(self.ops.created, ["app-copy"])
        self.assertNotIn("--delete", helper.calls[1][0])
        run = s.start_restore(host_id="local", repo_id=self.repo["id"], snapshot_id="4fdd93e9", target="app-copy", new_volume=True)
        await s.drain()
        self.assertIn("already exists", self.store.get_backup_run(run["id"])["error"])

    async def test_snapshots_cache_and_uninitialised(self):
        helper = FakeHelper(fixture("snapshots.json") + ["@@kowan rc snapshots list 0"])
        s = self.service(helper)
        snaps = await s.list_snapshots(self.repo["id"], "", "kowan-f3-fixture")
        self.assertEqual(len(snaps), 2)
        await s.list_snapshots(self.repo["id"], "", "kowan-f3-fixture")
        self.assertEqual(len(helper.calls), 1)  # cached
        s2 = self.service(FakeHelper(["Fatal: repository does not exist", "@@kowan rc snapshots list 10"]))
        self.assertEqual(await s2.list_snapshots(self.repo["id"]), [])
        s3 = self.service(FakeHelper(["Fatal: wrong password", "@@kowan rc snapshots list 12"]))
        with self.assertRaises(errors.BadGateway):
            await s3.list_snapshots(self.repo["id"])
        out = await s.browse(self.repo["id"], "4fdd93e9", "/data/kowan-f3-fixture")
        self.assertEqual(out["path"], "/data/kowan-f3-fixture")


class TestAPI(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from kowan.api import volbackups as api

        self.store = _store()
        self.jwt = new_jwt("x" * 48)
        self.svc = Services(config=_cfg(), store=self.store, jwt=self.jwt, docker=FakeProvider(FakeOps()))
        app = create_app(self.svc, registrars=[api.register])
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def req(self, method, path, role="admin", **kw):
        h = {"Authorization": "Bearer " + self.jwt.issue(_ensure_user(self.store, role), role, role)}
        r = await self.client.request(method, "/api/v1" + path, headers=h, **kw)
        return r.status, await r.json()

    async def test_repo_and_job_crud(self):
        body = {"name": "minio", "type": "s3", "config": {"endpoint": "http://m:9000", "bucket": "b"},
                "secrets": {"password": "pw", "accessKeyId": "ak", "secretAccessKey": "sk"}}
        st, _ = await self.req("POST", "/volbackups/repos", role="operator", json=body)
        self.assertEqual(st, 403)
        st, repo = await self.req("POST", "/volbackups/repos", json=dict(body, secrets={}))
        self.assertEqual(st, 400)
        st, repo = await self.req("POST", "/volbackups/repos", json=body)
        self.assertEqual(st, 201, repo)
        self.assertEqual(repo["secrets"], {"password": "********", "accessKeyId": "********", "secretAccessKey": "********"})
        st, _ = await self.req("POST", "/volbackups/repos", json=body)
        self.assertEqual(st, 409)
        # Update with masked secrets keeps them.
        st, upd = await self.req("PUT", f"/volbackups/repos/{repo['id']}", json={"config": {"endpoint": "http://m:9000", "bucket": "c"}, "secrets": {"password": "********"}})
        self.assertEqual(st, 200, upd)
        self.assertEqual(self.store.repo_secrets(repo["id"])["password"], "pw")
        job = {"name": "j", "hostId": "local", "repoId": repo["id"], "selector": {"mode": "label"}, "schedule": "0 3 * * *",
               "timezone": "UTC", "retention": {"keepDaily": 7, "keepWeekly": 4}}
        st, j = await self.req("POST", "/volbackups/jobs", role="operator", json=job)
        self.assertEqual(st, 201, j)
        self.assertTrue(j["nextRun"].endswith("03:00:00Z"))
        self.assertIsNone(j["lastRun"])
        st, pv = await self.req("POST", "/volbackups/jobs/preview", role="operator", json={"hostId": "local", "selector": {"mode": "label"}})
        self.assertEqual(pv["volumes"], ["app-data", "shop_db"])
        st, _ = await self.req("POST", "/volbackups/jobs", json=dict(job, repoId="missing"))
        self.assertEqual(st, 404)
        st, _ = await self.req("DELETE", f"/volbackups/repos/{repo['id']}")
        self.assertEqual(st, 409)
        st, lst = await self.req("GET", "/volbackups/jobs", role="viewer")
        self.assertEqual((st, len(lst)), (200, 1))
        st, _ = await self.req("DELETE", f"/volbackups/jobs/{j['id']}", role="operator")
        self.assertEqual(st, 200)
        st, _ = await self.req("DELETE", f"/volbackups/repos/{repo['id']}")
        self.assertEqual(st, 200)
        st, _ = await self.req("POST", "/volbackups/restore", json={"hostId": "local"})
        self.assertEqual(st, 400)
        st, runs = await self.req("GET", "/volbackups/runs", role="viewer")
        self.assertEqual((st, runs), (200, []))


if __name__ == "__main__":
    unittest.main()
