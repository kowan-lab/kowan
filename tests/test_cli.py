"""kowanctl end to end: the real CLI (typer app + stdlib HTTP client) against a real
server (with the fake Docker Engine) running in a background thread. Every
command group runs once, with its output, its errors and the saved config."""

import asyncio
import importlib
import io
import json
import os
import socket
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from aiohttp import web

from kowan.cli import output
from kowan.cli.client import APIError, Client
from kowan.cli.httpclient import Reply, Session
from kowan.server import build_app
from tests.apicase import config
from tests.fakeengine import FakeEngine

# kowan.cli re-exports main() under the module's name, so import the module itself.
climain = importlib.import_module("kowan.cli.main")


class ThreadedServer:
    """A Kowan server on its own event loop in a daemon thread."""

    def start(self) -> str:
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        return asyncio.run_coroutine_threadsafe(self._start(), self.loop).result(30)

    async def _start(self) -> str:
        self.engine = FakeEngine()
        sock = await self.engine.start()
        self.engine.add_image("nginx:1.27")
        self.engine.add_container("web", "nginx:1.27", ports=[(80, 8080)])
        self.engine.add_volume("data")
        app, self.svc = await build_app(config(tempfile.mkdtemp(), sock))
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        await web.TCPSite(self.runner, "127.0.0.1", port).start()
        return f"http://127.0.0.1:{port}"

    def stop(self) -> None:
        async def _stop():
            await self.runner.cleanup()
            await self.engine.stop()
        asyncio.run_coroutine_threadsafe(_stop(), self.loop).result(30)
        self.loop.call_soon_threadsafe(self.loop.stop)


class CLICase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadedServer()
        cls.url = cls.server.start()
        r = Session().request("POST", cls.url + "/api/v1/auth/init", json={
            "username": "root", "password": "supersecret", "setupToken": cls.server.svc.setup_token})
        cls.token = r.json()["token"]

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self._env = mock.patch.dict(os.environ, {"HOME": self.home, "KOWAN_SERVER": "", "KOWAN_TOKEN": "",
                                                 "KOWAN_API_KEY": ""})
        self._env.start()
        self.addCleanup(self._env.stop)

    def run_cli(self, *args, auth=True, code=0):
        argv = ["kowanctl", "--server", self.url]
        if auth:
            argv += ["--token", self.token]
        out, err = io.StringIO(), io.StringIO()
        status = 0
        with mock.patch("sys.argv", argv + list(args)), redirect_stdout(out), redirect_stderr(err):
            try:
                climain.main()
            except SystemExit as e:
                status = e.code or 0
        self.assertEqual(status, code, f"{args}: {err.getvalue()} {out.getvalue()}")
        return out.getvalue(), err.getvalue()


class TestHostCommands(CLICase):
    def test_rename_by_id_or_name(self):
        out, _ = self.run_cli("hosts", "rename", "local", "nas")
        self.assertIn("Renamed host local to nas", out)
        out, _ = self.run_cli("hosts", "ls")
        self.assertIn("nas", out)
        out, _ = self.run_cli("hosts", "rename", "nas", "local")
        self.assertIn("to local", out)
        _, err = self.run_cli("hosts", "rename", "missing", "x", code=1)
        self.assertIn("no host with id or name", err)


class TestReadCommands(CLICase):
    def test_lists(self):
        out, _ = self.run_cli("hosts", "ls")
        self.assertIn("local", out)
        out, _ = self.run_cli("container", "ls", "--all")
        self.assertIn("web", out)
        self.assertIn("8080", out)
        out, _ = self.run_cli("image", "ls")
        self.assertIn("nginx", out)
        out, _ = self.run_cli("volume", "ls")
        self.assertIn("data", out)
        out, _ = self.run_cli("network", "ls")
        self.assertIn("bridge", out)
        out, _ = self.run_cli("user", "ls")
        self.assertIn("root", out)
        out, _ = self.run_cli("version")
        self.assertIn("kowanctl", out)

    def test_container_actions(self):
        cid = self.server.engine.container("web")["Id"]
        for action in ("stop", "start", "restart"):
            out, _ = self.run_cli("container", action, cid)
            self.assertIn(cid, out)
        _, err = self.run_cli("container", "stop", "missing", code=1)
        self.assertIn("error:", err)

    def test_container_logs_stream(self):
        eng = self.server.engine
        cid = eng.container("web")["Id"]
        eng.logs[cid] = [(1, "GET / 200"), (2, "warn: slow request")]
        out, _ = self.run_cli("container", "logs", cid)
        self.assertIn("GET / 200", out)
        self.assertIn("slow request", out)


class TestAuthAndConfig(CLICase):
    def test_login_saves_token_then_commands_use_it(self):
        out, _ = self.run_cli("login", "-u", "root", "-p", "supersecret", auth=False)
        self.assertIn("token saved", out)
        path = os.path.join(self.home, ".kowan", "config.yaml")
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
        with mock.patch("sys.argv", ["kowanctl", "hosts", "ls"]), redirect_stdout(io.StringIO()) as out:
            climain.main()
        self.assertIn("local", out.getvalue())
        _, err = self.run_cli("login", "-u", "root", "-p", "wrong", auth=False, code=1)
        self.assertIn("401", err)

    def test_missing_server_or_token(self):
        with mock.patch("sys.argv", ["kowanctl", "--token", "t", "hosts", "ls"]), \
                redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit):
                climain.main()
        self.assertIn("no server configured", err.getvalue())
        _, err = self.run_cli("hosts", "ls", auth=False, code=1)
        self.assertIn("not authenticated", err)
        # A usage error (unknown command) is reported, not a traceback.
        _, err = self.run_cli("frobnicate", code=1)
        self.assertIn("error:", err)

    def test_api_keys(self):
        out, _ = self.run_cli("apikey", "create", "ci", "--role", "viewer")
        secret = next(w for w in out.split() if w.startswith("pk_"))
        out, _ = self.run_cli("apikey", "ls")
        self.assertIn("ci", out)
        # The key works on its own.
        with mock.patch("sys.argv", ["kowanctl", "--server", self.url, "--api-key", secret, "hosts", "ls"]), \
                redirect_stdout(io.StringIO()) as o:
            climain.main()
        self.assertIn("local", o.getvalue())
        key_id = Client(self.url, self.token).list_api_keys()[0]["id"]
        self.run_cli("apikey", "rm", key_id)
        self.run_cli("apikey", "create", "x", "--role", "god", code=1)


class TestDeploymentsAndBackup(CLICase):
    def test_deployment_lifecycle(self):
        f = os.path.join(self.home, "dep.json")
        with open(f, "w") as fh:
            json.dump({"name": "cli-web", "type": "container", "strategy": "standalone", "targets": ["local"],
                       "spec": {"image": "nginx:1.27"}}, fh)
        out, _ = self.run_cli("deployment", "create", "-f", f)
        dep_id = out.split("(")[1].split(")")[0]
        out, _ = self.run_cli("deployment", "ls")
        self.assertIn("cli-web", out)
        self.run_cli("deployment", "deploy", dep_id)
        c = Client(self.url, self.token)
        for _ in range(100):
            if c.get_deployment(dep_id)["status"] == "deployed":
                break
            threading.Event().wait(0.05)
        out, _ = self.run_cli("deployment", "status", dep_id)
        self.assertIn("running", out)
        for action in ("stop", "start", "redeploy", "destroy"):
            self.run_cli("deployment", action, dep_id)
            for _ in range(100):
                if c.get_deployment(dep_id)["status"] not in ("pending", "deploying"):
                    break
                threading.Event().wait(0.05)
        # History: an edit makes revision 2; diff shows it; rollback restores #1.
        dep = c.get_deployment(dep_id)
        dep["spec"]["image"] = "nginx:1.28"
        c.update_deployment(dep_id, dep)
        out, _ = self.run_cli("deployment", "history", dep_id)
        self.assertIn("#2", out)
        self.assertIn("edit", out)
        out, _ = self.run_cli("deployment", "diff", dep_id, "2")
        self.assertIn("+  image: nginx:1.28", out)
        out, _ = self.run_cli("deployment", "diff", dep_id, "1", "--against", "2")
        self.assertIn("--- #2", out)
        out, _ = self.run_cli("deployment", "rollback", dep_id, "1", "--no-deploy")
        self.assertIn("rolled back to #1", out)
        self.assertEqual(c.get_deployment(dep_id)["spec"]["image"], "nginx:1.27")
        self.run_cli("deployment", "rollback", dep_id, "99", code=1)
        self.run_cli("deployment", "rm", dep_id)
        self.run_cli("deployment", "create", code=1)
        bad = os.path.join(self.home, "bad.json")
        with open(bad, "w") as fh:
            fh.write("{nope")
        self.run_cli("deployment", "create", "-f", bad, code=1)
        self.run_cli("deployment", "create", "-f", os.path.join(self.home, "missing.json"), code=1)

    def test_previews(self):
        svc = self.server.svc
        real = svc.deployments.dispatch_deploy

        async def no_deploy(dep_id, *, redeploy=False):  # no network clone in tests
            return svc.store.get_deployment(dep_id)

        svc.deployments.dispatch_deploy = no_deploy
        self.addCleanup(setattr, svc.deployments, "dispatch_deploy", real)
        c = Client(self.url, self.token)
        dep = c.create_deployment({"name": "cli-git", "type": "compose", "strategy": "standalone", "targets": ["local"],
                                   "git": {"repoUrl": "https://example.com/o/r.git",
                                           "previews": {"enabled": True, "branches": "*"}}})
        out, _ = self.run_cli("deployment", "preview", dep["id"], "feature/x")
        self.assertIn('Preview "cli-git-feature-x"', out)
        out, _ = self.run_cli("deployment", "previews", dep["id"])
        self.assertIn("feature/x", out)
        self.run_cli("deployment", "preview", dep["id"], "main", code=1)
        self.run_cli("deployment", "rm", dep["id"])

    def test_promote(self):
        c = Client(self.url, self.token)
        mk = lambda n, img: c.create_deployment({"name": n, "type": "container", "strategy": "standalone",  # noqa: E731
                                                 "targets": ["local"], "spec": {"image": img}})
        s, p = mk("cli-stage", "nginx:1.28"), mk("cli-prod", "nginx:1.27")
        out, _ = self.run_cli("deployment", "promote", s["id"], "--to", p["id"], "--dry-run")
        self.assertIn("image: nginx:1.27 -> nginx:1.28", out)
        self.assertEqual(c.get_deployment(p["id"])["spec"]["image"], "nginx:1.27")
        out, _ = self.run_cli("deployment", "promote", s["id"], "--to", p["id"], "--no-deploy")
        self.assertIn('Promoted into "cli-prod"', out)
        out, _ = self.run_cli("deployment", "promote", s["id"], "--to", p["id"])
        self.assertIn("already runs the same thing", out)
        self.run_cli("deployment", "promote", s["id"], code=1)  # no target configured
        for d in (s, p):
            self.run_cli("deployment", "rm", d["id"])

    def test_backup_and_restore(self):
        dest = os.path.join(self.home, "b.tar.gz")
        out, _ = self.run_cli("backup", "-o", dest)
        self.assertIn(dest, out)
        self.assertGreater(os.path.getsize(dest), 100)
        out, _ = self.run_cli("restore", dest)
        self.assertIn("restart", out.lower())
        self.run_cli("restore", os.path.join(self.home, "missing.tar.gz"), code=1)

    def test_workflows(self):
        r = Session().request("POST", self.url + "/api/v1/workflows", headers={"Authorization": "Bearer " + self.token}, json={
            "name": "cli-wf", "enabled": True, "steps": [{"name": "b", "type": "backup", "config": {}}]})
        wf_id = r.json()["id"]
        out, _ = self.run_cli("workflow", "ls")
        self.assertIn("cli-wf", out)
        out, _ = self.run_cli("workflow", "run", wf_id)
        self.assertIn("Started run", out)
        out, _ = self.run_cli("workflow", "runs", wf_id)
        self.assertIn("manual", out)
        self.run_cli("workflow", "run", "missing", code=1)

    def test_interactive_login_and_backup_errors(self):
        with mock.patch("sys.stdin", io.StringIO("root\nsupersecret\n")):
            out, _ = self.run_cli("login", auth=False)
        self.assertIn("Logged in", out)
        with mock.patch("sys.stdin", io.StringIO("\n")):
            _, err = self.run_cli("login", auth=False, code=1)
        self.assertIn("username is required", err)
        with mock.patch("sys.stdin", io.StringIO("root\n\n")):
            _, err = self.run_cli("login", auth=False, code=1)
        self.assertIn("password is required", err)
        self.run_cli("backup", "-o", os.path.join(self.home, "no", "such", "dir", "b.tgz"), code=1)


class TestClientAndOutput(unittest.TestCase):
    def test_client_errors_and_urls(self):
        c = Client("https://k.example/ ", "t", "pk_x")
        self.assertEqual(c.server, "https://k.example")
        self.assertEqual(c._auth_headers()["X-API-Key"], "pk_x")
        self.assertTrue(c.container_logs_ws_url("local", "abc").startswith("wss://k.example/"))
        self.assertTrue(Client("http://k").container_logs_ws_url("local", "a").startswith("ws://"))
        def resp(reply):
            return Session(handler=lambda r: reply).request("GET", "http://k")

        self.assertIn("plain failure", str(c._status_error("GET", "/x", resp(Reply(500, content=b"plain failure")))))
        self.assertEqual(str(c._status_error("GET", "/x", resp(Reply(404, json={"error": "gone"})))),
                         "GET /x: gone (404)")
        self.assertIn("418", str(c._status_error("GET", "/x", resp(Reply(418)))))
        self.assertTrue(issubclass(APIError, Exception))
        c.close()

    def test_output_helpers(self):
        self.assertEqual(len(output.truncate("abcdefghij", 5)), 5)
        self.assertEqual(output.truncate("abc", 5), "abc")
        self.assertEqual(output.short_id("sha256:" + "a" * 64), "a" * 12)
        self.assertEqual(output.first_name(["/web"]), "web")
        self.assertEqual(output.first_name([]), "<none>")
        self.assertEqual(output.dash(""), "-")
        self.assertEqual(output.dash("x"), "x")
        self.assertNotEqual(output.human_bytes(2048), output.human_bytes(2))
        self.assertNotEqual(output.bool_str(True), output.bool_str(False))
        self.assertTrue(output.unix_time(0))

if __name__ == "__main__":
    unittest.main()
