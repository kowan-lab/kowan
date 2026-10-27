"""Workflow script steps run bash on the control plane, so they are fenced in.

* the script never sees the server's own environment (its keys and config);
* a step cannot export variables that change how bash or the loader behave;
* only an admin may create, change, run or expose a workflow with a script;
* ``KOWAN_SERVER_SCRIPTS=off`` refuses script steps entirely (hosted instances).
"""

import asyncio
import datetime
import os
import tempfile
import unittest
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

from kowan import httpcap
from kowan.config import ServerConfig
from kowan.models import WorkflowPayload, WorkflowStep
from kowan.server import build_app
from kowan.workflow.engine import Deps, Engine, _parse_export_file


class _Store:
    def resolve_secrets(self, text):
        return text


def _engine(**kw):
    return Engine(Deps(store=_Store(), **kw))


class TestScriptEnvironment(unittest.TestCase):
    def test_server_environment_is_not_inherited(self):
        step = WorkflowStep(type="script", config={"script": "env"})
        with mock.patch.dict(os.environ, {"KOWAN_SECRET_KEY": "top-secret", "AWS_SECRET_ACCESS_KEY": "aws-secret-value-123"}):
            out = asyncio.run(_engine()._run_script(step, None, {"GREETING": "hi"}))
        self.assertNotIn("top-secret", out)
        self.assertNotIn("aws-secret-value-123", out)
        self.assertIn("GREETING=hi", out)

    def test_unsafe_exports_are_dropped(self):
        parsed = _parse_export_file(
            "RESULT=ok\nPATH=/tmp/evil\nBASH_ENV=/tmp/x\nLD_PRELOAD=/tmp/x.so\nKOWAN_OUTPUT=/etc/passwd\nbad name=1\n"
        )
        self.assertEqual(parsed, {"RESULT": "ok"})

    def test_scripts_can_be_disabled(self):
        step = WorkflowStep(type="script", config={"script": "echo hi"})
        engine = _engine(server_scripts=False)
        with self.assertRaises(ValueError):
            asyncio.run(engine._dispatch(step, None, {}))


class _SecretStore:
    def resolve_secrets(self, text):
        return text.replace("[[api-token]]", "TOKEN-123").replace("[[db-password]]", "DB-PASSWORD")


class TestHttpStepRendering(unittest.TestCase):
    def test_caller_values_cannot_reference_secrets(self):
        sent = {}

        async def handler(request):
            sent["url"] = str(request.url)
            sent["body"] = request.content.decode()
            sent["auth"] = request.headers.get("x-token")
            return httpcap.Reply(200, text="ok")

        step = WorkflowStep(type="http", config={
            "url": "https://relay.example/hook?q={{query.q}}",
            "body": '{"text": {{body}}, "who": "$KOWAN_WEBHOOK_HEADER_X_USER"}',
            "headers": "X-Token: [[api-token]]",
        })
        payload = WorkflowPayload(method="POST", body='"[[db-password]] $SECRET_VAR"',
                                  headers={"X-User": "[[db-password]]"}, query={"q": "[[db-password]]"})
        env = {"SECRET_VAR": "should-not-expand", "KOWAN_WEBHOOK_HEADER_X_USER": "[[db-password]]"}
        engine = Engine(Deps(store=_SecretStore()))
        with httpcap.mock(handler):
            asyncio.run(engine._run_http(step, payload, env))

        # The step's own reference resolves; the caller's stay literal text.
        self.assertEqual(sent["auth"], "TOKEN-123")
        self.assertNotIn("DB-PASSWORD", sent["body"] + sent["url"])
        self.assertNotIn("should-not-expand", sent["body"])
        self.assertIn("[[db-password]]", sent["body"])


def _cfg():
    return ServerConfig(
        addr="127.0.0.1:0",
        data_dir=tempfile.mkdtemp(),
        jwt_secret="x" * 48,
        monitor_interval=datetime.timedelta(seconds=3600),
        metrics_retention=datetime.timedelta(hours=1),
        secret_key="",
        external_url="",
    )


class TestScriptRights(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        app, self.svc = await build_app(_cfg())
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        r = await self.client.post("/api/v1/auth/init", json={
            "username": "root", "password": "supersecret", "setupToken": self.svc.setup_token})
        self.admin = {"Authorization": "Bearer " + (await r.json())["token"]}
        await self.client.post("/api/v1/users", headers=self.admin, json={
            "username": "ops", "password": "opspassword", "role": "operator"})
        r = await self.client.post("/api/v1/auth/login", json={"username": "ops", "password": "opspassword"})
        self.ops = {"Authorization": "Bearer " + (await r.json())["token"]}

    async def asyncTearDown(self):
        await self.client.close()

    async def test_only_admins_manage_script_workflows(self):
        script = {"name": "s", "steps": [{"name": "a", "type": "script", "config": {"script": "id"}}]}
        notify = {"name": "n", "steps": [{"name": "a", "type": "notify", "config": {"message": "hi"}}]}

        r = await self.client.post("/api/v1/workflows", headers=self.ops, json=script)
        self.assertEqual(r.status, 403)
        r = await self.client.post("/api/v1/workflows", headers=self.ops, json=notify)
        self.assertEqual(r.status, 201, await r.text())
        harmless = (await r.json())["id"]
        # An operator cannot turn an existing workflow into a script.
        r = await self.client.put(f"/api/v1/workflows/{harmless}", headers=self.ops, json=script)
        self.assertEqual(r.status, 403)

        r = await self.client.post("/api/v1/workflows", headers=self.admin, json=script)
        self.assertEqual(r.status, 201, await r.text())
        wid = (await r.json())["id"]
        for method, path in (("POST", f"/workflows/{wid}/run"), ("POST", f"/workflows/{wid}/webhook"),
                             ("PUT", f"/workflows/{wid}")):
            r = await self.client.request(method, "/api/v1" + path, headers=self.ops, json=notify)
            self.assertEqual(r.status, 403, path)


if __name__ == "__main__":
    unittest.main()
