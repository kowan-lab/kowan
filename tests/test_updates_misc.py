"""preflight / healthcheck sub-commands, the digest helpers, the helper CLI and
the LocalClient primitives the helper uses."""

import io
import json
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

from kowan import agentupdate
from kowan.docker.local import LocalClient
from kowan.store import Store
from kowan.updater import helper, preflight
from kowan.updater.plan import encode_plan


def _last_json(out: str):
    return json.loads(out.strip().splitlines()[-1])


class TestPreflight(unittest.TestCase):
    def test_server_preflight_migrates_a_copy_only(self):
        d = tempfile.mkdtemp()
        db = os.path.join(d, "kowan.db")
        Store(db).close()
        c = sqlite3.connect(db)
        c.execute("DROP TABLE updates")  # an "older" database
        c.commit()
        c.close()
        before = os.path.getmtime(db)
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"KOWAN_DATA_DIR": d}), redirect_stdout(buf):
            rc = preflight.server_preflight(["--db", db])
        doc = _last_json(buf.getvalue())
        self.assertEqual(rc, 0, doc)
        self.assertEqual({c["name"] for c in doc["checks"]}, {"imports", "config", "helper", "database"})
        c = sqlite3.connect(db)
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        c.close()
        self.assertNotIn("updates", tables, "the given db must not be migrated in place")
        self.assertEqual(os.path.getmtime(db), before)

    def test_server_preflight_fails_on_bad_db(self):
        d = tempfile.mkdtemp()
        bad = os.path.join(d, "bad.db")
        with open(bad, "wb") as fh:
            fh.write(b"garbage" * 1000)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = preflight.server_preflight(["--db", bad])
        self.assertEqual(rc, 1)
        self.assertFalse(_last_json(buf.getvalue())["ok"])

    def test_agent_preflight_config_checks(self):
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"KOWAN_AGENT_MODE": "edge", "KOWAN_SERVER_URL": ""}), redirect_stdout(buf):
            self.assertEqual(preflight.agent_preflight([]), 1)
        with mock.patch.dict(os.environ, {"KOWAN_AGENT_MODE": "edge", "KOWAN_SERVER_URL": "http://s"}), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(preflight.agent_preflight([]), 0)


class TestHealthchecks(unittest.TestCase):
    def test_agent_heartbeat(self):
        path = os.path.join(tempfile.mkdtemp(), "hb")
        with mock.patch.dict(os.environ, {preflight.HEARTBEAT_ENV: path}), redirect_stdout(io.StringIO()):
            self.assertEqual(preflight.agent_healthcheck([]), 1)
            preflight.touch_heartbeat()
            self.assertEqual(preflight.agent_healthcheck([]), 0)
            self.assertEqual(preflight.agent_healthcheck([], now=time.time() + 120), 1)
            preflight.clear_heartbeat()
            self.assertEqual(preflight.agent_healthcheck([]), 1)

    def test_server_healthcheck_against_local_server(self):
        import http.server
        import threading

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200 if self.path == "/health" else 404)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            port = srv.server_address[1]
            with mock.patch.dict(os.environ, {"KOWAN_ADDR": f":{port}", "KOWAN_TLS_CERT": ""}), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(preflight.server_healthcheck([]), 0)
        finally:
            srv.shutdown()
            srv.server_close()
        with mock.patch.dict(os.environ, {"KOWAN_ADDR": ":1", "KOWAN_TLS_CERT": ""}), redirect_stdout(io.StringIO()):
            self.assertEqual(preflight.server_healthcheck([]), 1)

    def test_dispatch(self):
        self.assertIsNone(preflight.dispatch("server", []))
        self.assertIsNone(preflight.dispatch("server", ["serve"]))


class TestHelperCli(unittest.TestCase):
    def test_invalid_plan_reports_aborted(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(helper.main(["--plan", "!!"]), 0)
        self.assertEqual(_last_json(buf.getvalue())["result"], "aborted")

    def test_runs_plan_and_prints_result(self):
        tok = encode_plan({"id": "x", "component": "agent", "mode": "update", "target": "t", "image": "i",
                           "waitSeconds": 0})

        async def fake_run(plan, socket_path="/var/run/docker.sock", engine=None):
            print(json.dumps({"result": "failed"}))
            return 2

        buf = io.StringIO()
        with mock.patch("kowan.updater.helper.run_plan", new=fake_run), redirect_stdout(buf):
            self.assertEqual(helper.main(["--plan", tok]), 2)


class TestDigestHelpers(unittest.TestCase):
    def test_split_ref(self):
        self.assertEqual(agentupdate._split_ref("kowan-lab/kowan:1.2"), ("", "kowan-lab/kowan", "1.2"))
        self.assertEqual(agentupdate._split_ref("reg.io:5000/a/b@sha256:x"), ("reg.io:5000", "a/b", "latest"))

    def test_running_digest(self):
        img = {"RepoDigests": ["other/x@sha256:o", "reg.io/ghcr.io/kowan-lab/kowan@sha256:mine"]}
        self.assertEqual(agentupdate._running_digest(img, "ghcr.io/kowan-lab/kowan"), "sha256:mine")
        self.assertEqual(agentupdate._running_digest({}, "x"), "")


class TestLocalClientPrimitives(unittest.IsolatedAsyncioTestCase):
    async def test_raw_calls(self):
        c = LocalClient(socket_path="/nonexistent.sock")
        req = mock.AsyncMock(return_value=(204, b""))
        rj = mock.AsyncMock(return_value={"Id": "new"})
        with mock.patch.object(LocalClient, "_request", new=req), \
                mock.patch.object(LocalClient, "_request_json", new=rj):
            await c.rename_container("a", "a-previous")
            await c.update_container_config("a", {"RestartPolicy": {"Name": "no"}})
            await c.stop_container_timeout("a", 7)
            await c.connect_container_endpoint("net", "a", {"Aliases": ["s"]})
            cid = await c.create_container_raw("n", {"Image": "x"})
        self.assertEqual(cid, "new")
        calls = [(x.args[0], x.args[1], x.kwargs.get("params"), x.kwargs.get("json_body")) for x in req.await_args_list]
        self.assertEqual(calls[0], ("POST", "/containers/a/rename", {"name": "a-previous"}, None))
        self.assertEqual(calls[1], ("POST", "/containers/a/update", None, {"RestartPolicy": {"Name": "no"}}))
        self.assertEqual(calls[2], ("POST", "/containers/a/stop", {"t": "7"}, None))
        self.assertEqual(calls[3], ("POST", "/networks/net/connect", None,
                                    {"Container": "a", "EndpointConfig": {"Aliases": ["s"]}}))
        self.assertEqual(rj.await_args.kwargs["params"], {"name": "n"})


if __name__ == "__main__":
    unittest.main()
