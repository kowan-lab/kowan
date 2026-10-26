"""Scheduled start/stop: cron next-run with timezones/DST, the missed-run
policy, label precedence, validation, execution and the HTTP API."""

import asyncio
import datetime as dt
import tempfile
import unittest
from datetime import timedelta, timezone

from aiohttp.test_utils import TestClient, TestServer

from kowan import errors
from kowan.auth import new_jwt
from kowan.config import ServerConfig
from kowan.models import Container, Host
from kowan.schedules import service as schedsvc
from kowan.schedules.cron import iso, next_run, next_runs
from kowan.schedules.runner import CronEntry, CronRunner
from kowan.store import Store
from kowan.web import Services, create_app

UTC = timezone.utc


def T(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))



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

class TestCronNextRun(unittest.TestCase):
    def test_basic_utc(self):
        self.assertEqual(iso(next_run("0 22 * * 1-5", T("2026-10-23T21:00:00Z"), "UTC")), "2026-10-23T22:00:00Z")
        # Friday 22:00 passed -> Monday.
        self.assertEqual(iso(next_run("0 22 * * 1-5", T("2026-10-23T22:00:00Z"), "UTC")), "2026-10-26T22:00:00Z")

    def test_timezone_offset(self):
        # 08:00 Europe/Rome in summer (CEST, +2) = 06:00Z.
        self.assertEqual(iso(next_run("0 8 * * *", T("2026-07-01T00:00:00Z"), "Europe/Rome")), "2026-07-01T06:00:00Z")
        # In winter (CET, +1) = 07:00Z.
        self.assertEqual(iso(next_run("0 8 * * *", T("2026-12-01T00:00:00Z"), "Europe/Rome")), "2026-12-01T07:00:00Z")
        # America/New_York.
        self.assertEqual(iso(next_run("30 9 * * *", T("2026-01-10T00:00:00Z"), "America/New_York")), "2026-01-10T14:30:00Z")

    def test_dst_spring_forward_gap_runs_once_after_gap(self):
        # Europe/Rome jumps 02:00 -> 03:00 on 2026-03-29. 02:30 does not exist.
        runs = next_runs("30 2 * * *", T("2026-03-28T12:00:00Z"), "Europe/Rome", 2)
        self.assertEqual(iso(runs[0]), "2026-03-29T01:00:00Z")  # 03:00 CEST
        self.assertEqual(iso(runs[1]), "2026-03-30T00:30:00Z")  # 02:30 CEST next day

    def test_dst_fall_back_runs_once(self):
        # Europe/Rome repeats 02:00-03:00 on 2026-10-25; 02:30 happens twice.
        runs = next_runs("30 2 * * *", T("2026-10-24T12:00:00Z"), "Europe/Rome", 2)
        self.assertEqual(iso(runs[0]), "2026-10-25T00:30:00Z")  # first occurrence (CEST)
        self.assertEqual(iso(runs[1]), "2026-10-26T01:30:00Z")  # next day (CET)

    def test_dom_dow_or_and_never(self):
        # dom=1 OR monday
        self.assertEqual(iso(next_run("0 0 1 * 1", T("2026-10-27T00:00:00Z"), "UTC")), "2026-11-01T00:00:00Z")
        self.assertIsNone(next_run("0 0 30 2 *", T("2026-01-01T00:00:00Z"), "UTC"))

    def test_macro_and_naive_input(self):
        self.assertEqual(iso(next_run("@hourly", dt.datetime(2026, 1, 1, 10, 15), "UTC")), "2026-01-01T11:00:00Z")

    def test_invalid(self):
        with self.assertRaises(ValueError):
            next_run("61 * * * *", T("2026-01-01T00:00:00Z"))
        with self.assertRaises(ValueError):
            next_run("* * * * *", T("2026-01-01T00:00:00Z"), "Mars/Base")


class FakeClock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class TestRunnerPolicy(unittest.IsolatedAsyncioTestCase):
    async def test_fires_on_time_and_skips_when_late(self):
        clock = FakeClock(T("2026-10-25T09:59:30Z"))
        fired, skipped = [], []

        async def fire(e, due):
            fired.append(iso(due))

        entries = [CronEntry(key="a", spec="0 10 * * *", tz="UTC", anchor=T("2026-10-25T09:00:00Z"))]
        r = CronRunner("t", lambda: entries, fire, lambda e, due, why: skipped.append((iso(due), why)), clock=clock)
        r.reload()
        self.assertEqual(iso(r.next_due("a")), "2026-10-25T10:00:00Z")
        self.assertEqual(r.tick(), [])
        clock.t = T("2026-10-25T10:03:00Z")  # 3 min late: within grace
        self.assertEqual(r.tick(), ["a"])
        await r.drain()
        self.assertEqual(fired, ["2026-10-25T10:00:00Z"])
        self.assertEqual(iso(r.next_due("a")), "2026-10-26T10:00:00Z")
        clock.t = T("2026-10-26T10:11:00Z")  # 11 min late: skipped
        self.assertEqual(r.tick(), [])
        self.assertEqual(len(skipped), 1)
        self.assertIn("missed", skipped[0][1])
        self.assertEqual(iso(r.next_due("a")), "2026-10-27T10:00:00Z")

    async def test_restart_detects_missed_slot_once(self):
        # Last run yesterday 10:00, server comes back today at 15:00: the
        # missed 10:00 slot is reported once, then the next is tomorrow.
        clock = FakeClock(T("2026-10-25T15:00:00Z"))
        skipped = []

        async def fire(e, due):
            raise AssertionError("must not fire late")

        entries = [CronEntry(key="a", spec="0 10 * * *", tz="UTC", last_run=T("2026-10-24T10:00:00Z"))]
        r = CronRunner("t", lambda: entries, fire, lambda e, due, why: skipped.append(iso(due)), clock=clock)
        r.reload()
        r.tick()
        self.assertEqual(skipped, ["2026-10-25T10:00:00Z"])
        self.assertEqual(iso(r.next_due("a")), "2026-10-26T10:00:00Z")

    async def test_overlap_skipped(self):
        clock = FakeClock(T("2026-10-25T10:00:00Z"))
        gate = asyncio.Event()
        skipped = []

        async def fire(e, due):
            await gate.wait()

        entries = [CronEntry(key="a", spec="* * * * *", tz="UTC", last_run=T("2026-10-25T09:59:00Z"))]
        r = CronRunner("t", lambda: entries, fire, lambda e, due, why: skipped.append(why), clock=clock)
        r.reload()
        self.assertEqual(r.tick(), ["a"])
        await asyncio.sleep(0)
        clock.t = T("2026-10-25T10:01:00Z")
        self.assertEqual(r.tick(), [])
        self.assertIn("still in progress", skipped[0])
        gate.set()
        await r.drain()

    async def test_reload_keeps_unchanged_and_recomputes_edited(self):
        clock = FakeClock(T("2026-10-25T09:00:00Z"))
        entries = [CronEntry(key="a", spec="0 10 * * *", tz="UTC")]

        async def fire(e, due):
            pass

        r = CronRunner("t", lambda: entries, fire, clock=clock)
        r.reload()
        self.assertEqual(iso(r.next_due("a")), "2026-10-25T10:00:00Z")
        entries[0] = CronEntry(key="a", spec="0 11 * * *", tz="UTC")
        r.reload()
        self.assertEqual(iso(r.next_due("a")), "2026-10-25T11:00:00Z")
        entries.append(CronEntry(key="bad", spec="nope", tz="UTC"))
        r.reload()
        self.assertIsNone(r.next_due("bad"))


class TestLabelsAndValidation(unittest.TestCase):
    def test_labels(self):
        out = schedsvc.schedules_from_labels("h1", "web", {
            "kowan.schedule.stop": "0 22 * * 1-5", "kowan.schedule.start": "0 8 * * 1-5",
            "kowan.schedule.timezone": "Europe/Rome", "other": "x",
        })
        self.assertEqual(sorted(s["action"] for s in out), ["start", "stop"])
        self.assertTrue(all(s["source"] == "label" and s["timezone"] == "Europe/Rome" for s in out))
        self.assertEqual(out[0]["id"], "label:h1:web:start")
        bad = schedsvc.schedules_from_labels("h1", "web", {"kowan.schedule.restart": "bogus"})
        self.assertFalse(bad[0]["enabled"])
        self.assertTrue(bad[0]["error"])

    def test_precedence_stored_wins_even_disabled(self):
        labels = schedsvc.schedules_from_labels("h1", "web", {"kowan.schedule.stop": "0 22 * * *", "kowan.schedule.start": "0 8 * * *"})
        manual = [{"targetType": "container", "hostId": "h1", "targetId": "web", "action": "stop", "enabled": False}]
        kept = schedsvc.apply_precedence(manual, labels)
        self.assertEqual([k["action"] for k in kept], ["start"])
        other_host = [{"targetType": "container", "hostId": "h2", "targetId": "web", "action": "stop"}]
        self.assertEqual(len(schedsvc.apply_precedence(other_host, labels)), 2)

    def test_validate(self):
        ok = schedsvc.validate_schedule({"targetType": "container", "targetId": "/web", "hostId": "local",
                                         "action": "stop", "cron": "0 22 * * *", "timezone": "Europe/Rome",
                                         "onlyIf": {"noExecSession": True}})
        self.assertEqual(ok["targetId"], "web")
        self.assertEqual(ok["name"], "stop web")
        cases = [
            {"targetType": "vm", "targetId": "x", "hostId": "h", "action": "stop", "cron": "* * * * *"},
            {"targetType": "container", "targetId": "x", "hostId": "h", "action": "deploy", "cron": "* * * * *"},
            {"targetType": "container", "targetId": "x", "hostId": "", "action": "stop", "cron": "* * * * *"},
            {"targetType": "container", "targetId": "x", "hostId": "h", "action": "stop", "cron": "bad"},
            {"targetType": "container", "targetId": "x", "hostId": "h", "action": "stop", "cron": "* * * * *", "timezone": "X/Y"},
            {"targetType": "container", "targetId": "x", "hostId": "h", "action": "start", "cron": "* * * * *", "onlyIf": {"noExecSession": True}},
            {"targetType": "container", "targetId": "x", "hostId": "h", "action": "stop", "cron": "* * * * *", "bogus": 1},
            [1],
            {"targetType": "container", "targetId": " ", "hostId": "h", "action": "stop", "cron": "* * * * *"},
            {"targetType": "container", "targetId": "x", "hostId": "h", "action": "stop", "cron": "* * * * *", "enabled": "y"},
            {"targetType": "container", "targetId": "x", "hostId": "h", "action": "stop", "cron": "* * * * *", "onlyIf": "x"},
        ]
        for c in cases:
            with self.assertRaises(errors.Validation, msg=str(c)):
                schedsvc.validate_schedule(c)
        dep = schedsvc.validate_schedule({"targetType": "deployment", "targetId": "d1", "action": "deploy", "cron": "@daily"})
        self.assertEqual(dep["hostId"], "")


class FakeOps:
    def __init__(self, containers, exec_ids=None):
        self.containers = containers
        self.calls = []
        self.exec_ids = exec_ids or {}

    async def list_containers(self, all):
        return self.containers

    async def inspect_container(self, id):
        return {"ExecIDs": self.exec_ids.get(id)}

    async def start_container(self, id):
        self.calls.append(("start", id))

    async def stop_container(self, id):
        self.calls.append(("stop", id))

    async def restart_container(self, id):
        self.calls.append(("restart", id))


class FakeProvider:
    def __init__(self, ops):
        self._ops = ops

    def ops(self, host):
        return self._ops


class FakeDeployments:
    def __init__(self):
        self.calls = []

    async def start(self, i):
        self.calls.append(("start", i))

    async def stop(self, i):
        self.calls.append(("stop", i))

    async def redeploy(self, i):
        self.calls.append(("deploy", i))


def _cfg():
    return ServerConfig(addr="127.0.0.1:0", data_dir=tempfile.mkdtemp(), jwt_secret="x" * 48,
                        monitor_interval=dt.timedelta(seconds=3600), metrics_retention=dt.timedelta(hours=1),
                        secret_key="", external_url="")


def _store():
    st = Store(":memory:")
    st.create_host(Host(id="local", name="local", type="local", mode="agent", address="", token="t",
                        lastSeen="", createdAt="2026-01-01T00:00:00Z"))
    return st


class TestExecution(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = _store()
        self.ops = FakeOps([
            Container(id="c1", names=["/web"], state="running", labels={"com.docker.compose.project": "shop"}),
            Container(id="c2", names=["/db"], state="exited", labels={"com.docker.compose.project": "shop"}),
            Container(id="c3", names=["/busy"], state="running", labels={"kowan.schedule.stop": "0 22 * * *"}),
        ], exec_ids={"c3": ["e1"]})
        self.svc = Services(config=_cfg(), store=self.store, docker=FakeProvider(self.ops))
        self.deps = FakeDeployments()
        self.svc.attach("deployments", self.deps)
        self.s = schedsvc.ScheduleService(self.svc)

    async def test_container_stop_and_noop(self):
        s = schedsvc.validate_schedule({"targetType": "container", "targetId": "web", "hostId": "local", "action": "stop", "cron": "@daily"})
        s = self.store.save_schedule(s)
        run = await self.s.execute(s, trigger="manual")
        self.assertEqual(run["status"], "success")
        self.assertEqual(self.ops.calls, [("stop", "c1")])
        s2 = self.store.save_schedule(schedsvc.validate_schedule({"targetType": "container", "targetId": "db", "hostId": "local", "action": "stop", "cron": "@daily"}))
        run = await self.s.execute(s2, trigger="manual")
        self.assertIn("nothing to stop", run["message"])

    async def test_stack_start_and_missing(self):
        s = self.store.save_schedule(schedsvc.validate_schedule({"targetType": "stack", "targetId": "shop", "hostId": "local", "action": "start", "cron": "@daily"}))
        run = await self.s.execute(s, trigger="manual")
        self.assertEqual(self.ops.calls, [("start", "c2")])
        s = self.store.save_schedule(schedsvc.validate_schedule({"targetType": "container", "targetId": "ghost", "hostId": "local", "action": "start", "cron": "@daily"}))
        run = await self.s.execute(s, trigger="manual")
        self.assertEqual(run["status"], "failed")
        self.assertEqual(len(self.store.list_schedule_runs(s["id"])), 1)

    async def test_only_if_no_exec(self):
        s = self.store.save_schedule(schedsvc.validate_schedule({"targetType": "container", "targetId": "busy", "hostId": "local", "action": "stop", "cron": "@daily", "onlyIf": {"noExecSession": True}}))
        run = await self.s.execute(s, trigger="schedule")
        self.assertEqual(run["status"], "skipped")
        self.assertEqual(self.ops.calls, [])
        self.assertEqual(self.store.get_schedule(s["id"])["lastStatus"], "skipped")

    async def test_deployment_actions(self):
        for action in ("start", "stop", "restart", "deploy"):
            s = schedsvc.validate_schedule({"targetType": "deployment", "targetId": "d1", "action": action, "cron": "@daily"})
            s["id"] = "x-" + action
            await self.s.execute(s, trigger="manual")
        self.assertEqual(self.deps.calls, [("start", "d1"), ("stop", "d1"), ("stop", "d1"), ("start", "d1"), ("deploy", "d1")])

    async def test_label_scan_and_listing(self):
        await self.s.scan_labels()
        all_ = self.s.all_schedules()
        ids = [s["id"] for s in all_]
        self.assertIn("label:local:busy:stop", ids)
        self.assertTrue(all(s["nextRun"] for s in all_))
        # A stored schedule for the same container/action hides the label.
        self.store.save_schedule(schedsvc.validate_schedule({"targetType": "container", "targetId": "busy", "hostId": "local", "action": "stop", "cron": "0 23 * * *", "enabled": False}))
        ids = [s["id"] for s in self.s.all_schedules()]
        self.assertNotIn("label:local:busy:stop", ids)


class TestAPI(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from kowan.api import schedules as api

        self.store = _store()
        self.jwt = new_jwt("x" * 48)
        self.ops = FakeOps([Container(id="c1", names=["/web"], state="running")])
        self.svc = Services(config=_cfg(), store=self.store, jwt=self.jwt, docker=FakeProvider(self.ops))
        app = create_app(self.svc, registrars=[api.register])
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def req(self, method, path, role="admin", **kw):
        h = {"Authorization": "Bearer " + self.jwt.issue(_ensure_user(self.store, role), role, role)}
        r = await self.client.request(method, "/api/v1" + path, headers=h, **kw)
        return r.status, await r.json()

    async def test_crud_preview_run(self):
        body = {"targetType": "container", "targetId": "web", "hostId": "local", "action": "stop", "cron": "0 22 * * 1-5", "timezone": "Europe/Rome"}
        st, _ = await self.req("POST", "/schedules", role="viewer", json=body)
        self.assertEqual(st, 403)
        st, s = await self.req("POST", "/schedules", json=body)
        self.assertEqual(st, 201, s)
        self.assertTrue(s["nextRun"].endswith("Z"))
        st, lst = await self.req("GET", "/schedules?targetType=container&targetId=web", role="viewer")
        self.assertEqual((st, len(lst)), (200, 1))
        st, upd = await self.req("PUT", f"/schedules/{s['id']}", json={"enabled": False})
        self.assertEqual((st, upd["enabled"], upd["nextRun"]), (200, False, ""))
        st, run = await self.req("POST", f"/schedules/{s['id']}/run")
        self.assertEqual((st, run["status"]), (200, "success"))
        st, runs = await self.req("GET", f"/schedules/runs?scheduleId={s['id']}")
        self.assertEqual(len(runs), 1)
        st, pv = await self.req("POST", "/schedules/preview", json={"cron": "0 8 * * *", "timezone": "UTC", "count": 3})
        self.assertEqual((st, len(pv["nextRuns"])), (200, 3))
        st, err = await self.req("POST", "/schedules/preview", json={"cron": "nope"})
        self.assertEqual(st, 400)
        st, _ = await self.req("PUT", "/schedules/label:local:web:stop", json={})
        self.assertEqual(st, 400)
        st, _ = await self.req("DELETE", f"/schedules/{s['id']}")
        self.assertEqual(st, 200)
        st, _ = await self.req("GET", f"/schedules/{s['id']}")
        self.assertEqual(st, 404)


if __name__ == "__main__":
    unittest.main()
