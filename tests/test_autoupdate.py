"""Auto-update: policy parsing, windows, min-age, label precedence, the safe
switch (gate / verify / rollback) against a fake Docker client, and the
background runner's modes. No docker, no network."""

import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from kowan.autoupdate import engine, policy
from kowan.autoupdate.runner import AutoUpdater, stagger_offset, tracked_ref
from kowan.models import Container, Host
from kowan.store import Store


class TestModesAndDurations(unittest.TestCase):
    def test_modes(self):
        self.assertEqual(policy.parse_mode("APPLY"), "apply")
        self.assertEqual(policy.parse_mode(""), "off")
        with self.assertRaises(ValueError):
            policy.parse_mode("true")
        with self.assertRaises(ValueError):
            policy.parse_mode("sometimes")

    def test_durations(self):
        self.assertEqual(policy.parse_duration("2d"), timedelta(days=2))
        self.assertEqual(policy.parse_duration("1d12h"), timedelta(hours=36))
        self.assertEqual(policy.parse_duration("90m"), timedelta(minutes=90))
        self.assertEqual(policy.parse_duration("1w"), timedelta(days=7))
        self.assertEqual(policy.parse_duration("3"), timedelta(days=3))
        self.assertIsNone(policy.parse_duration(""))
        for bad in ("2x", "d2", "2d junk", "-1d"):
            with self.assertRaises(ValueError, msg=bad):
                policy.parse_duration(bad)
        self.assertEqual(policy.format_duration(timedelta(hours=36)), "1d12h")

    def test_flags(self):
        self.assertTrue(policy.notify_disabled({"kowan.notify": "off"}))
        self.assertTrue(policy.notify_disabled({"kowan.notify": "False"}))
        self.assertFalse(policy.notify_disabled({"kowan.notify": "on"}))
        self.assertFalse(policy.notify_disabled({}))
        self.assertTrue(policy.is_hidden({"kowan.hidden": "true"}))
        self.assertFalse(policy.is_hidden({"kowan.hidden": "no"}))


class TestWindows(unittest.TestCase):
    # 2026-10-24 is a Saturday, 2026-10-26 a Monday.
    def test_daily_range(self):
        w = policy.parse_window("02:00-05:00")
        self.assertTrue(w.is_open(datetime(2026, 10, 26, 2, 0)))
        self.assertTrue(w.is_open(datetime(2026, 10, 26, 4, 59)))
        self.assertFalse(w.is_open(datetime(2026, 10, 26, 5, 0)))
        self.assertEqual(w.next_open(datetime(2026, 10, 26, 6, 0)), datetime(2026, 10, 27, 2, 0))

    def test_weekdays(self):
        w = policy.parse_window("sat,sun 02:00-05:00")
        self.assertTrue(w.is_open(datetime(2026, 10, 24, 3, 0)))
        self.assertFalse(w.is_open(datetime(2026, 10, 26, 3, 0)))
        w = policy.parse_window("02:00-03:00 mon-fri")
        self.assertEqual(w.days, {0, 1, 2, 3, 4})
        w = policy.parse_window("02:00-03:00 fri-mon")
        self.assertEqual(w.days, {4, 5, 6, 0})

    def test_overnight_belongs_to_start_day(self):
        w = policy.parse_window("22:00-02:00 fri")
        self.assertTrue(w.is_open(datetime(2026, 10, 23, 23, 0)))   # Fri 23:00
        self.assertTrue(w.is_open(datetime(2026, 10, 24, 1, 30)))   # Sat 01:30 (Fri night)
        self.assertFalse(w.is_open(datetime(2026, 10, 25, 1, 30)))  # Sun 01:30
        self.assertFalse(w.is_open(datetime(2026, 10, 23, 21, 59)))

    def test_cron(self):
        w = policy.parse_window("cron 0 3 * * 6 2h")
        self.assertTrue(w.is_open(datetime(2026, 10, 24, 3, 0)))
        self.assertTrue(w.is_open(datetime(2026, 10, 24, 4, 59)))
        self.assertFalse(w.is_open(datetime(2026, 10, 24, 5, 0)))
        self.assertFalse(w.is_open(datetime(2026, 10, 25, 3, 30)))
        self.assertEqual(w.next_open(datetime(2026, 10, 25, 10, 0)), datetime(2026, 10, 31, 3, 0))
        w = policy.parse_window("0 4 * * *")
        self.assertEqual(w.duration, timedelta(hours=1))
        w = policy.parse_window("@daily 30m")
        self.assertTrue(w.is_open(datetime(2026, 10, 26, 0, 29)))
        self.assertFalse(w.is_open(datetime(2026, 10, 26, 0, 30)))

    def test_invalid(self):
        self.assertIsNone(policy.parse_window(""))
        for bad in ("whenever", "25:00-26:00", "02:00-03:00 funday", "cron 0 3 * *", "cron 0 3 * * 6 2d",
                    "01:00-02:00 03:00-04:00"):
            with self.assertRaises(ValueError, msg=bad):
                policy.parse_window(bad)


class TestMinAge(unittest.TestCase):
    NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)

    def test_no_minimum(self):
        self.assertEqual(policy.age_ok(None, None, None, self.NOW), (True, None))

    def test_first_seen(self):
        ok, age = policy.age_ok(timedelta(days=2), self.NOW - timedelta(days=1), None, self.NOW)
        self.assertFalse(ok)
        self.assertEqual(age, timedelta(days=1))
        self.assertTrue(policy.age_ok(timedelta(days=2), self.NOW - timedelta(days=3), None, self.NOW)[0])

    def test_created_counts_when_plausible(self):
        self.assertTrue(policy.age_ok(timedelta(days=2), self.NOW, self.NOW - timedelta(days=5), self.NOW)[0])
        # reproducible builds stamp 1970: ignored
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        self.assertFalse(policy.age_ok(timedelta(days=2), self.NOW, epoch, self.NOW)[0])

    def test_unknown_age_waits(self):
        self.assertFalse(policy.age_ok(timedelta(hours=1), None, None, self.NOW)[0])


class TestPrecedence(unittest.TestCase):
    def test_label_wins(self):
        eff = policy.resolve_policy(
            {"kowan.update": "notify", "kowan.update.min-age": "3d"},
            {"mode": "apply", "minAge": "1d", "window": "02:00-03:00", "scanBlock": "high"},
            {"scanBlock": "critical"},
        )
        self.assertEqual((eff.mode, eff.min_age, eff.window, eff.scan_block), ("notify", "3d", "02:00-03:00", "high"))
        self.assertEqual(eff.sources, {"mode": "label", "window": "policy", "minAge": "label", "scanBlock": "policy"})

    def test_defaults(self):
        eff = policy.resolve_policy({}, None, {"scanBlock": "critical"})
        self.assertEqual(eff.mode, "off")
        self.assertEqual(eff.scan_block, "critical")
        self.assertEqual(eff.sources["scanBlock"], "default")
        self.assertEqual(policy.resolve_policy({}, None, None).scan_block, "never")

    def test_invalid_label_reported(self):
        eff = policy.resolve_policy({"kowan.update": "yolo", "kowan.scan.block": "x"}, {"mode": "apply", "scanBlock": "high"})
        self.assertEqual(eff.mode, "off")  # never fall back to a more aggressive mode
        self.assertEqual(eff.scan_block, "high")
        self.assertEqual(len(eff.errors), 2)

    def test_notify_flag(self):
        self.assertFalse(policy.resolve_policy({"kowan.notify": "off"}).notify)

    def test_validate_body(self):
        self.assertEqual(policy.validate_policy_body({"mode": "apply", "window": "02:00-03:00", "minAge": "2d",
                                                      "scanBlock": "worse"})["scanBlock"], "worse")
        with self.assertRaises(ValueError):
            policy.validate_policy_body({"mode": "apply", "window": "nope"})

    def test_tracked_ref(self):
        st = {"trackedRef": "nginx:latest", "rollbackRef": "nginx@sha256:old"}
        self.assertEqual(tracked_ref("nginx@sha256:old", st), "nginx:latest")
        self.assertEqual(tracked_ref("nginx:1.27", st), "nginx:1.27")
        self.assertEqual(tracked_ref("nginx:latest", {}), "nginx:latest")

    def test_stagger(self):
        a = stagger_offset("host-a", 6 * 3600)
        self.assertEqual(a, stagger_offset("host-a", 6 * 3600))
        self.assertTrue(0 <= a < 600)
        self.assertTrue(0 <= stagger_offset("x", 60) < 60)


# ---------------------------------------------------------------------------
# Fake docker for the engine
# ---------------------------------------------------------------------------

class FakeDocker:
    """Containers/images in memory. ``behaviour[image_id]`` decides how a
    container created from that image behaves: "ok", "crash", "unhealthy",
    "restart", "slowhealthy"."""

    def __init__(self, behaviour=None):
        self.behaviour = behaviour or {}
        self.images = {
            "sha256:old": {"Id": "sha256:old", "RepoTags": [], "RepoDigests": ["nginx@sha256:d-old"],
                           "Config": {}, "Created": "2026-10-01T00:00:00Z"},
            "sha256:new": {"Id": "sha256:new", "RepoTags": ["nginx:latest"], "RepoDigests": ["nginx@sha256:d-new"],
                           "Config": {}, "Created": "2026-10-20T00:00:00Z"},
        }
        self.tags = {"nginx:latest": "sha256:new", "nginx@sha256:d-old": "sha256:old", "nginx@sha256:d-new": "sha256:new"}
        self.containers = {}
        self.polls = {}
        self.log = []
        self.n = 0
        self._add("c0", "web", "nginx:latest", "sha256:old")

    def _add(self, cid, name, ref, image_id):
        self.containers[cid] = {"Id": cid, "Name": "/" + name, "Image": image_id, "Config": {"Image": ref, "Labels": {}},
                                "State": {"Running": True, "Status": "running"}, "RestartCount": 0}
        self.polls[cid] = 0

    def _resolve(self, ref):
        if ref in self.images:
            return ref
        return self.tags.get(ref)

    async def inspect_image(self, ref):
        iid = self._resolve(ref)
        if iid is None:
            raise RuntimeError("no such image " + ref)
        return dict(self.images[iid])

    async def inspect_container(self, cid):
        c = self.containers[await self._by_name(cid)]
        cid = c["Id"]
        self.polls[cid] += 1
        n = self.polls[cid]
        b = self.behaviour.get(c["Image"], "ok")
        st = {"Running": True, "Status": "running"}
        rc = 0
        if b == "crash" and n > 1:
            st = {"Running": False, "Status": "exited", "ExitCode": 1}
        elif b == "unhealthy":
            st["Health"] = {"Status": "starting" if n < 3 else "unhealthy"}
        elif b == "restart" and n > 2:
            rc = 2
        elif b == "slowhealthy":
            st["Health"] = {"Status": "starting" if n < 4 else "healthy"}
        c["State"], c["RestartCount"] = st, rc
        return {k: (dict(v) if isinstance(v, dict) else v) for k, v in c.items()}

    async def clone_recreate(self, cid, image_ref, start=True):
        old = self.containers.pop(cid)
        self.n += 1
        new = f"c{self.n}"
        self._add(new, old["Name"].lstrip("/"), image_ref, self._resolve(image_ref))
        self.log.append(("recreate", cid, image_ref, new))
        if start and self.behaviour.get(self._resolve(image_ref)) == "nostart":
            # like the local raw clone: the clone cannot start, so it is removed
            # and the original is put back under its name
            del self.containers[new]
            self.containers[cid] = old
            raise RuntimeError("OCI runtime create failed: exec: sleep: not found")
        if not start:
            self.containers[new]["State"] = {"Running": False, "Status": "created"}
        return new

    async def _by_name(self, ref):
        for c in self.containers.values():
            if c["Id"] == ref or c["Name"].lstrip("/") == ref:
                return c["Id"]
        raise RuntimeError("no such container " + ref)

    async def stop_container(self, cid):
        self.log.append(("stop", cid))


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    async def sleep(self, d):
        self.t += d


def _run_engine(docker, gate="never", scan=None, verify=engine.VerifyOptions(timeout=30, stable=4, poll=1)):
    clk = _Clock()
    return asyncio.run(engine.apply_container_update(
        docker, "c0", image_ref="nginx:latest", scan=scan, gate_policy=gate, verify=verify,
        clock=clk, sleep=clk.sleep,
    ))


class TestEngine(unittest.TestCase):
    def test_update_ok(self):
        d = FakeDocker()
        out = _run_engine(d)
        self.assertEqual(out.outcome, "updated", out.message)
        self.assertEqual(d.log, [("recreate", "c0", "nginx:latest", "c1")])
        self.assertEqual((out.from_digest, out.to_digest), ("sha256:d-old", "sha256:d-new"))
        self.assertEqual(out.rollback_ref, "nginx@sha256:d-old")

    def test_already_current(self):
        d = FakeDocker()
        d.containers["c0"]["Image"] = "sha256:new"
        self.assertEqual(_run_engine(d).outcome, "current")
        self.assertEqual(d.log, [])

    def test_crash_rolls_back_to_old_digest(self):
        d = FakeDocker({"sha256:new": "crash"})
        out = _run_engine(d)
        self.assertEqual(out.outcome, "rolled_back")
        self.assertIn("exit code 1", out.message)
        self.assertEqual([e[2] for e in d.log], ["nginx:latest", "nginx@sha256:d-old"])
        live = list(d.containers.values())
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0]["Image"], "sha256:old")
        self.assertEqual(out.container_id, live[0]["Id"])

    def test_failed_start_recovers_previous_image(self):
        d = FakeDocker({"sha256:new": "nostart"})
        out = _run_engine(d)
        self.assertEqual(out.outcome, "rolled_back", out.message)
        self.assertIn("switch failed", out.message)
        live = list(d.containers.values())
        self.assertEqual(len(live), 1)
        # The raw clone put the original container itself back, untouched.
        self.assertEqual(live[0]["Image"], "sha256:old")
        self.assertEqual(live[0]["Config"]["Image"], "nginx:latest")
        self.assertIn("previous container restored", out.message)

    def test_unhealthy_rolls_back(self):
        out = _run_engine(FakeDocker({"sha256:new": "unhealthy"}))
        self.assertEqual(out.outcome, "rolled_back")
        self.assertIn("unhealthy", out.message)

    def test_restart_loop_rolls_back(self):
        out = _run_engine(FakeDocker({"sha256:new": "restart"}))
        self.assertEqual(out.outcome, "rolled_back")
        self.assertIn("restarted", out.message)

    def test_waits_for_healthcheck(self):
        out = _run_engine(FakeDocker({"sha256:new": "slowhealthy"}))
        self.assertEqual(out.outcome, "updated", out.message)
        self.assertIn("healthy", out.message)

    def test_deadline(self):
        d = FakeDocker({"sha256:new": "unhealthy"})
        d.behaviour["sha256:new"] = "slowhealthy"
        out = _run_engine(d, verify=engine.VerifyOptions(timeout=2, stable=1, poll=1))
        self.assertEqual(out.outcome, "rolled_back")
        self.assertIn("starting", out.message)

    def test_gate_blocks_and_leaves_container(self):
        async def scan(img):
            return {"status": "ok", "counts": {"critical": 2 if img["Id"] == "sha256:new" else 0, "high": 0}}
        d = FakeDocker()
        out = _run_engine(d, gate="critical", scan=scan)
        self.assertEqual(out.outcome, "blocked")
        self.assertIn("critical", out.message)
        self.assertEqual(d.log, [])
        self.assertIn("c0", d.containers)

    def test_gate_worse_than_current(self):
        async def scan(img):
            return {"status": "ok", "counts": {"critical": 1, "high": 4 if img["Id"] == "sha256:new" else 2}}
        out = _run_engine(FakeDocker(), gate="worse", scan=scan)
        self.assertEqual(out.outcome, "blocked")
        self.assertEqual(out.current_scan["high"], 2)

        async def better(img):
            return {"status": "ok", "counts": {"critical": 0, "high": 1 if img["Id"] == "sha256:new" else 5}}
        self.assertEqual(_run_engine(FakeDocker(), gate="worse", scan=better).outcome, "updated")

    def test_gate_fails_closed_on_scan_error(self):
        async def broken(img):
            raise RuntimeError("trivy unavailable")
        out = _run_engine(FakeDocker(), gate="high", scan=broken)
        self.assertEqual(out.outcome, "blocked")
        self.assertIn("trivy unavailable", out.message)

    def test_stopped_container_not_verified(self):
        d = FakeDocker({"sha256:new": "crash"})
        d.containers["c0"]["State"] = {"Running": False, "Status": "exited"}
        clk = _Clock()
        insp = {**d.containers["c0"], "State": {"Running": False, "Status": "exited"}}
        out = asyncio.run(engine.apply_container_update(d, "c0", image_ref="nginx:latest", insp=insp,
                                                        clock=clk, sleep=clk.sleep))
        self.assertEqual(out.outcome, "updated")
        self.assertIn("not verified", out.message)


# ---------------------------------------------------------------------------
# Runner modes
# ---------------------------------------------------------------------------

class _Bus:
    def __init__(self):
        self.events = []

    def publish(self, e):
        self.events.append(e)


class _Provider:
    def __init__(self, ops):
        self._ops = ops

    def ops(self, host):
        return self._ops


class RunnerOps(FakeDocker):
    def __init__(self, labels, behaviour=None):
        super().__init__(behaviour)
        self.containers["c0"]["Config"]["Labels"] = labels
        self.pulls = []

    async def list_containers(self, all):
        return [Container(id=c["Id"], names=[c["Name"]], image=c["Config"]["Image"], state="running",
                          labels=c["Config"]["Labels"]) for c in self.containers.values()]

    async def pull_image(self, ref, auth):
        self.pulls.append(ref)

        class S:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration
        return S()

    async def clone_recreate(self, cid, image_ref, start=True):
        labels = self.containers[cid]["Config"]["Labels"]
        new = await super().clone_recreate(cid, image_ref, start=start)
        self.containers[new]["Config"]["Labels"] = labels
        return new


class _Svc:
    def __init__(self, store, ops):
        self.store = store
        self.docker = _Provider(ops)
        self.bus = _Bus()


class TestRunner(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "t.db"))
        self.store.create_host(Host(id="h1", name="local", type="local"))
        self.host = self.store.get_host("h1")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_verify_options_take_a_zero_stable_window(self):
        au = AutoUpdater(_Svc(self.store, None))
        self.assertEqual((au.verify_options().timeout, au.verify_options().stable), (120.0, 10.0))
        with mock.patch("kowan.autoupdate.runner.load_settings", return_value={"stableSeconds": 0, "verifyTimeoutSeconds": 30}):
            opts = au.verify_options()
        self.assertEqual((opts.timeout, opts.stable), (30.0, 0.0))

    def _run(self, ops, **kw):
        svc = _Svc(self.store, ops)
        au = AutoUpdater(svc)
        au.verify_options = lambda: engine.VerifyOptions(timeout=5, stable=0.02, poll=0.01)

        async def remote(ref, refresh):
            return "sha256:d-new", ""

        async def go():
            with mock.patch.object(au, "_remote_digest", remote), \
                    mock.patch("kowan.registry.docker_auth_for", mock.AsyncMock(return_value="")):
                return await au.process_host(self.host, check_registry=True, **kw)
        return asyncio.run(go()), svc

    def test_forgets_state_of_removed_containers(self):
        now = "2026-10-25T16:27:31Z"
        gone = {"lastResult": "failed", "lastMessage": "network not found"}
        self.store.put_autoupdate_state("container", "h1", "fast-db-1", gone, now)
        self.store.put_autoupdate_state("container", "other", "elsewhere", gone, now)
        self.store.put_autoupdate_state("deployment", "", "deleted-dep", gone, now)
        self._run(RunnerOps({}))
        left = {(s["kind"], s["hostId"], s["target"]) for s in self.store.list_autoupdate_states()}
        # Only the row of another host survives; this host's pass cannot see it.
        self.assertEqual(left, {("container", "other", "elsewhere")})

    def test_notify_once_per_digest(self):
        ops = RunnerOps({"kowan.update": "notify"})
        res, svc = self._run(ops)
        self.assertEqual(res[0]["result"], "available")
        self.assertEqual([e.payload["title"] for e in svc.bus.events], ["Update available"])
        self.assertEqual(self.store.list_autoupdate_history()["items"][0]["outcome"], "notified")
        res, svc = self._run(ops)
        self.assertEqual(svc.bus.events, [])  # same digest: no second alert
        self.assertEqual(ops.log, [])  # never switched

    def test_notify_off_label_silences(self):
        ops = RunnerOps({"kowan.update": "notify", "kowan.notify": "off"})
        _, svc = self._run(ops)
        self.assertEqual(svc.bus.events, [])
        self.assertEqual(self.store.list_autoupdate_history()["total"], 1)

    def test_pull_mode(self):
        ops = RunnerOps({"kowan.update": "pull"})
        res, _ = self._run(ops)
        self.assertEqual(res[0]["result"], "pulled")
        self.assertEqual(ops.pulls, ["nginx:latest"])
        self.assertEqual(ops.log, [])
        self._run(ops)
        self.assertEqual(ops.pulls, ["nginx:latest"])  # once per digest

    def test_apply_mode_updates(self):
        ops = RunnerOps({"kowan.update": "apply"})
        res, _ = self._run(ops)
        self.assertEqual(res[0]["result"], "updated", res)
        st = self.store.get_autoupdate_state("container", "h1", "web")
        self.assertFalse(st["available"])
        self.assertEqual(self.store.list_autoupdate_history()["items"][0]["outcome"], "updated")

    def test_apply_waits_for_window_and_min_age(self):
        ops = RunnerOps({"kowan.update": "apply", "kowan.update.min-age": "2d"})
        ops.images["sha256:new"]["Created"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        res, _ = self._run(ops)
        self.assertEqual(res[0]["result"], "waiting")
        st = self.store.get_autoupdate_state("container", "h1", "web")
        self.assertTrue(st["pending"])
        self.assertIn("age", st["lastMessage"])
        self.assertEqual(ops.log, [])
        ops2 = RunnerOps({"kowan.update": "apply"})
        self.store.upsert_autoupdate_policy({"kind": "container", "hostId": "h1", "target": "web", "mode": "apply",
                                             "window": "cron 0 0 1 1 * 1m"})
        res, _ = self._run(ops2)
        self.assertEqual(res[0]["result"], "waiting")
        self.assertIn("window", self.store.get_autoupdate_state("container", "h1", "web")["lastMessage"])

    def test_rollback_records_and_skips_bad_digest(self):
        ops = RunnerOps({"kowan.update": "apply"}, {"sha256:new": "crash"})
        res, svc = self._run(ops)
        self.assertEqual(res[0]["result"], "rolled_back")
        self.assertEqual([e.payload["title"] for e in svc.bus.events], ["Update rolled back"])
        st = self.store.get_autoupdate_state("container", "h1", "web")
        self.assertEqual(st["badDigest"], "sha256:d-new")
        self.assertEqual(st["trackedRef"], "nginx:latest")
        self.assertEqual(st["rollbackRef"], "nginx@sha256:d-old")
        live = list(ops.containers.values())[0]
        self.assertEqual(live["Config"]["Image"], "nginx@sha256:d-old")
        # Next pass follows the original tag but skips the known-bad digest.
        res, _ = self._run(ops)
        self.assertEqual(res[0]["result"], "skipped")
        self.assertEqual(len(ops.log), 2)

    def test_blocked_by_label_policy(self):
        ops = RunnerOps({"kowan.update": "apply", "kowan.scan.block": "critical"})
        svc_holder = {}

        class Scans:
            async def scan(self, host_id, image, ops=None, image_insp=None):
                return {"status": "ok", "counts": {"critical": 1, "high": 0}}

        orig = _Svc.__init__

        def init(self, store, o):
            orig(self, store, o)
            self.scans = Scans()
            svc_holder["svc"] = self
        with mock.patch.object(_Svc, "__init__", init):
            res, svc = self._run(ops)
        self.assertEqual(res[0]["result"], "blocked")
        self.assertEqual(ops.log, [])
        self.assertEqual(svc.bus.events[0].payload["title"], "Update blocked")

    def test_off_and_support_ignored(self):
        ops = RunnerOps({})
        res, _ = self._run(ops)
        self.assertEqual(res, [])
        ops = RunnerOps({"kowan.update": "apply", "kowan.support": "true"})
        res, _ = self._run(ops)
        self.assertEqual(res, [])


class TestMonitorNotifyLabel(unittest.TestCase):
    def test_rule_skips_opted_out_container(self):
        from kowan.models import AlertRule
        from kowan.monitor.monitor import rule_matches_container

        r = AlertRule(id="r", name="down", type="container_down")
        self.assertTrue(rule_matches_container(r, Container(id="a", names=["/a"], labels={})))
        self.assertFalse(rule_matches_container(r, Container(id="a", names=["/a"], labels={"kowan.notify": "off"})))


if __name__ == "__main__":
    unittest.main()
