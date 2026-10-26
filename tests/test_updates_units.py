"""Pure pieces of the self-update system: plan encoding, journal parsing,
verify rules and config cloning."""

import json
import unittest

from kowan.updater import clone, verify
from kowan.updater.plan import (
    PlanError, decode_plan, encode_plan, journal_line, parse_journal, plan_from_cmd, result_line,
)

BASE = {"id": "abc", "component": "server", "mode": "update", "target": "kowan-server", "image": "x:2"}


class TestPlan(unittest.TestCase):
    def test_roundtrip_and_defaults(self):
        tok = encode_plan(BASE)
        self.assertNotIn("=", tok)
        p = decode_plan(tok)
        self.assertEqual(p["target"], "kowan-server")
        self.assertEqual(p["previous"], "kowan-server-previous")
        self.assertEqual(p["stopTimeout"], 20)
        self.assertEqual(p["verifyTimeout"], 120.0)
        self.assertEqual(p["stableSeconds"], 10.0)
        self.assertEqual(p["v"], 1)

    def test_nested_db_and_record_survive(self):
        p = dict(BASE, db={"path": "/data/kowan.db", "rollbackRestore": "/s"},
                 record={"phases": [{"phase": "pull", "ok": True}], "startedBy": "admin"})
        out = decode_plan(encode_plan(p))
        self.assertEqual(out["db"]["rollbackRestore"], "/s")
        self.assertEqual(out["record"]["startedBy"], "admin")

    def test_invalid_plans(self):
        for bad in ({}, dict(BASE, component="db"), dict(BASE, mode="x"), dict(BASE, image=""),
                    dict(BASE, stopTimeout="soon"), dict(BASE, db={"x": 1}), dict(BASE, v=9),
                    dict(BASE, verifyTimeout=-1)):
            with self.assertRaises(PlanError, msg=bad):
                encode_plan(bad)
        for tok in ("", "%%%", "bm90IGpzb24"):
            with self.assertRaises(PlanError):
                decode_plan(tok)

    def test_rollback_needs_no_image(self):
        p = decode_plan(encode_plan(dict(BASE, mode="rollback", image="")))
        self.assertEqual(p["mode"], "rollback")

    def test_plan_from_cmd(self):
        tok = encode_plan(BASE)
        self.assertEqual(plan_from_cmd(["self-update", "--plan", tok])["id"], "abc")
        self.assertEqual(plan_from_cmd(["self-update", f"--plan={tok}"])["id"], "abc")
        self.assertIsNone(plan_from_cmd(["self-update", "--plan", "junk!"]))
        self.assertIsNone(plan_from_cmd(None))


class TestJournal(unittest.TestCase):
    def test_parse_with_docker_timestamps_and_noise(self):
        lines = [
            "2026-10-25T10:00:00.123456789Z " + journal_line("abc", "wait", True, "3s", ts="t1"),
            journal_line("abc", "stop", False, "boom", ts="t2"),
            "2026-10-25T10:00:01Z INFO something on stderr",
            "Traceback (most recent call last):",
            result_line({"result": "rolled_back", "error": "x"}),
            result_line({"result": "failed", "error": "later wins"}),
            "",
        ]
        parsed = parse_journal("\n".join(lines))
        self.assertEqual([p["phase"] for p in parsed["phases"]], ["wait", "stop"])
        self.assertEqual(parsed["phases"][0], {"phase": "wait", "ok": True, "at": "t1", "detail": "3s"})
        self.assertFalse(parsed["phases"][1]["ok"])
        self.assertEqual(parsed["result"]["error"], "later wins")
        self.assertIn("Traceback (most recent call last):", parsed["other"])
        self.assertIn("INFO something on stderr", parsed["other"])

    def test_empty(self):
        self.assertEqual(parse_journal(""), {"phases": [], "result": None, "other": []})


def _insp(status="running", running=True, restarts=0, health=None, hc=True, **st):
    state = {"Status": status, "Running": running, "ExitCode": st.pop("code", 0)}
    state.update(st)
    if health:
        state["Health"] = {"Status": health, "Log": [{"Output": "refused"}]}
    cfg = {"Healthcheck": {"Test": ["CMD", "x"]}} if hc else {}
    return {"State": state, "RestartCount": restarts, "Config": cfg}


class TestVerifyRules(unittest.TestCase):
    def ev(self, insp, since=None, now=100.0, stable=10, base=0):
        return verify.evaluate(insp, base, since, now, stable)

    def test_healthy_and_stable_is_ok(self):
        v, _, _ = self.ev(_insp(health="healthy"), since=80.0)
        self.assertEqual(v, "ok")

    def test_healthy_but_not_stable_waits(self):
        v, why, since = self.ev(_insp(health="healthy"), since=None)
        self.assertEqual(v, "wait")
        self.assertEqual(since, 100.0)

    def test_starting_waits(self):
        self.assertEqual(self.ev(_insp(health="starting"), since=0)[0], "wait")

    def test_unhealthy_fails_with_output(self):
        v, why, _ = self.ev(_insp(health="unhealthy"), since=0)
        self.assertEqual(v, "fail")
        self.assertIn("refused", why)

    def test_exit_restart_oom_fail(self):
        self.assertEqual(self.ev(_insp("exited", False, code=3))[0], "fail")
        self.assertEqual(self.ev(_insp("dead", False))[0], "fail")
        self.assertEqual(self.ev(_insp(restarts=2), base=1)[0], "fail")
        self.assertEqual(self.ev(_insp(Restarting=True))[0], "fail")
        self.assertEqual(self.ev(_insp(OOMKilled=True))[0], "fail")

    def test_created_waits(self):
        self.assertEqual(self.ev(_insp("created", False))[0], "wait")

    def test_no_healthcheck_only_needs_stability(self):
        self.assertEqual(self.ev(_insp(hc=False), since=85.0)[0], "ok")
        self.assertEqual(self.ev(_insp(hc=False), since=95.0)[0], "wait")
        self.assertFalse(verify.has_healthcheck({"Config": {"Healthcheck": {"Test": ["NONE"]}}, "State": {}}))


class TestWaitVerified(unittest.IsolatedAsyncioTestCase):
    async def test_timeout(self):
        t = {"now": 0.0}

        async def sleep(s):
            t["now"] += s

        async def insp():
            return _insp(health="starting")

        ok, why = await verify.wait_verified(insp, timeout=10, stable_seconds=2, sleep=sleep,
                                             monotonic=lambda: t["now"])
        self.assertFalse(ok)
        self.assertIn("not healthy after 10s", why)

    async def test_inspect_errors_eventually_fail(self):
        async def insp():
            raise RuntimeError("socket gone")

        async def sleep(s):
            pass

        ok, why = await verify.wait_verified(insp, timeout=1000, stable_seconds=1, sleep=sleep,
                                             monotonic=lambda: 0.0)
        self.assertFalse(ok)
        self.assertIn("socket gone", why)


class TestClone(unittest.TestCase):
    def setUp(self):
        self.img = {"Env": ["PATH=/bin", "PYTHONUNBUFFERED=1"], "Cmd": ["serve"],
                    "Entrypoint": ["python", "-m", "kowan.server"],
                    "Labels": {"org.opencontainers.image.version": "1.0"},
                    "Healthcheck": {"Test": ["CMD", "old"]}, "ExposedPorts": {"8080/tcp": {}},
                    "Volumes": {"/data": {}}, "WorkingDir": "/app"}
        cid = "a" * 64
        self.insp = {
            "Id": cid, "Name": "/kowan-server",
            "Config": {"Hostname": cid[:12], "Image": "kowan:1", "Env": self.img["Env"] + ["KOWAN_X=1"],
                       "Cmd": ["serve"], "Entrypoint": ["python", "-m", "kowan.server"],
                       "Labels": {"org.opencontainers.image.version": "1.0",
                                  "com.docker.compose.service": "server"},
                       "Healthcheck": {"Test": ["CMD", "old"]}, "ExposedPorts": {"8080/tcp": {}, "9/tcp": {}},
                       "Volumes": {"/data": {}}, "WorkingDir": "/app", "MacAddress": "x"},
            "HostConfig": {"Binds": ["/var/run/docker.sock:/var/run/docker.sock"],
                           "RestartPolicy": {"Name": "unless-stopped"}, "NetworkMode": "kowan_default",
                           "PortBindings": {"8080/tcp": [{"HostPort": "8080"}]}},
            "Mounts": [
                {"Type": "bind", "Source": "/var/run/docker.sock", "Destination": "/var/run/docker.sock", "RW": True},
                {"Type": "volume", "Name": "f00dcafe", "Destination": "/data", "RW": True},
            ],
            "NetworkSettings": {"Networks": {
                "kowan_default": {"Aliases": ["server", cid[:12]], "IPAMConfig": None},
                "extra": {"Aliases": ["srv"]},
            }},
        }

    def test_image_defaults_are_dropped_user_overrides_kept(self):
        body, _ = clone.clone_body(self.insp, self.img, "kowan:2", {"kowan.update.id": "u"})
        self.assertEqual(body["Image"], "kowan:2")
        self.assertEqual(body["Env"], ["KOWAN_X=1"])
        for k in ("Cmd", "Entrypoint", "Healthcheck", "WorkingDir", "Hostname", "MacAddress", "Volumes"):
            self.assertNotIn(k, body, k)
        self.assertEqual(body["ExposedPorts"], {"9/tcp": {}})
        self.assertEqual(body["Labels"], {"com.docker.compose.service": "server", "kowan.update.id": "u"})
        self.assertEqual(body["HostConfig"]["PortBindings"], {"8080/tcp": [{"HostPort": "8080"}]})
        self.assertEqual(body["HostConfig"]["RestartPolicy"], {"Name": "unless-stopped"})

    def test_anonymous_volume_is_kept_by_name(self):
        body, _ = clone.clone_body(self.insp, self.img, "kowan:2")
        self.assertIn("f00dcafe:/data", body["HostConfig"]["Binds"])
        self.assertIn("/var/run/docker.sock:/var/run/docker.sock", body["HostConfig"]["Binds"])

    def test_declared_volume_not_duplicated(self):
        self.insp["HostConfig"]["Binds"].append("kowan-data:/data")
        body, _ = clone.clone_body(self.insp, self.img, "kowan:2")
        self.assertEqual([b for b in body["HostConfig"]["Binds"] if b.endswith(":/data")], ["kowan-data:/data"])

    def test_aliases_and_extra_networks(self):
        body, extras = clone.clone_body(self.insp, self.img, "kowan:2")
        self.assertEqual(body["NetworkingConfig"]["EndpointsConfig"]["kowan_default"]["Aliases"], ["server"])
        self.assertEqual(extras, [("extra", {"Aliases": ["srv"]})])

    def test_custom_hostname_and_entrypoint_override_kept(self):
        self.insp["Config"]["Hostname"] = "myhost"
        self.insp["Config"]["Entrypoint"] = ["/bin/custom"]
        body, _ = clone.clone_body(self.insp, self.img, "kowan:2")
        self.assertEqual(body["Hostname"], "myhost")
        self.assertEqual(body["Entrypoint"], ["/bin/custom"])
        self.assertEqual(body["Cmd"], ["serve"], "Cmd kept: Engine won't inherit it with a custom entrypoint")

    def test_default_bridge_has_no_extras(self):
        self.insp["HostConfig"]["NetworkMode"] = "bridge"
        self.insp["NetworkSettings"]["Networks"] = {"bridge": {"Aliases": None}}
        body, extras = clone.clone_body(self.insp, self.img, "kowan:2")
        self.assertNotIn("NetworkingConfig", body)
        self.assertEqual(extras, [])

    def test_restart_policy(self):
        self.assertEqual(clone.restart_policy(self.insp), {"Name": "unless-stopped"})
        self.assertEqual(clone.restart_policy({"HostConfig": {"RestartPolicy": {"Name": "on-failure", "MaximumRetryCount": 3}}}),
                         {"Name": "on-failure", "MaximumRetryCount": 3})
        self.assertEqual(clone.restart_policy({}), {"Name": "no"})


if __name__ == "__main__":
    unittest.main()
