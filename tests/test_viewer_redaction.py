"""Viewers read deployments and workflows, but never the values that would let
them act or read secrets: webhook tokens and push secrets."""

import datetime
import tempfile
import unittest

from aiohttp.test_utils import TestClient, TestServer

from kowan.config import ServerConfig
from kowan.server import build_app


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


class ViewerTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        app, self.svc = await build_app(_cfg())
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        r = await self.client.post("/api/v1/auth/init", json={
            "username": "root", "password": "supersecret", "setupToken": self.svc.setup_token})
        self.admin = {"Authorization": "Bearer " + (await r.json())["token"]}
        self.h = {"admin": self.admin}
        for role in ("operator", "viewer"):
            await self.client.post("/api/v1/users", headers=self.admin, json={
                "username": role, "password": role + "-password", "role": role})
            r = await self.client.post("/api/v1/auth/login", json={"username": role, "password": role + "-password"})
            self.h[role] = {"Authorization": "Bearer " + (await r.json())["token"]}

    async def asyncTearDown(self):
        await self.client.close()

    async def get(self, role, path):
        r = await self.client.get("/api/v1" + path, headers=self.h[role])
        self.assertEqual(r.status, 200, await r.text())
        return await r.json()


class TestWebhookTokens(ViewerTestCase):
    async def test_workflow_run_token_hidden_from_viewers(self):
        r = await self.client.post("/api/v1/workflows", headers=self.admin, json={
            "name": "n", "steps": [{"name": "a", "type": "notify", "config": {"message": "hi"}}]})
        wid = (await r.json())["id"]
        r = await self.client.post(f"/api/v1/workflows/{wid}/webhook", headers=self.admin)
        token = (await r.json())["token"]

        self.assertEqual((await self.get("operator", f"/workflows/{wid}"))["runToken"], token)
        self.assertNotIn(token, str(await self.get("viewer", f"/workflows/{wid}")))
        self.assertNotIn(token, str(await self.get("viewer", "/workflows")))

    async def test_deployment_webhook_hidden_from_viewers(self):
        r = await self.client.post("/api/v1/deployments", headers=self.admin, json={
            "name": "web", "type": "compose", "strategy": "standalone", "targets": ["local"],
            "compose": "services:\n  web:\n    image: nginx:1.27\n"})
        self.assertIn(r.status, (200, 201), await r.text())
        did = (await r.json())["id"]
        r = await self.client.post(f"/api/v1/deployments/{did}/webhook", headers=self.admin)
        self.assertIn(r.status, (200, 201), await r.text())
        token = (await r.json())["token"]

        self.assertEqual((await self.get("operator", f"/deployments/{did}"))["redeployToken"], token)
        self.assertNotIn(token, str(await self.get("viewer", f"/deployments/{did}")))
        self.assertNotIn(token, str(await self.get("viewer", "/deployments")))


class TestEnvironmentValues(ViewerTestCase):
    COMPOSE = (
        "services:\n"
        "  db:\n"
        "    image: postgres:16\n"
        "    environment:\n"
        "      POSTGRES_PASSWORD: hunter2-db\n"
        "  api:\n"
        "    image: api:1\n"
        "    environment:\n"
        "      - API_KEY=hunter2-api\n"
    )

    async def test_compose_env_values_masked_for_viewers(self):
        r = await self.client.post("/api/v1/deployments", headers=self.admin, json={
            "name": "shop", "type": "compose", "strategy": "standalone", "targets": ["local"],
            "compose": self.COMPOSE})
        self.assertIn(r.status, (200, 201), await r.text())
        did = (await r.json())["id"]

        seen = (await self.get("viewer", f"/deployments/{did}"))["compose"]
        self.assertNotIn("hunter2", seen)
        self.assertIn("POSTGRES_PASSWORD", seen)
        self.assertIn("API_KEY: '********'", seen)
        self.assertIn("hunter2-db", (await self.get("operator", f"/deployments/{did}"))["compose"])

    def test_inspect_env_values_masked(self):
        from kowan.redact import redact_inspect

        info = {"Config": {"Env": ["PATH=/usr/bin", "TOKEN=abc=def", "FLAG"]}}
        self.assertEqual(redact_inspect(info)["Config"]["Env"], ["PATH=********", "TOKEN=********", "FLAG"])

    def test_compose_secrets_outside_environment_are_dropped(self):
        from kowan.redact import redact_compose

        text = (
            "x-env: &env {DB_PASSWORD: hunter2a}\n"
            "services:\n"
            "  db:\n"
            "    image: postgres:16\n"
            "    environment: *env\n"
            "    build: {context: ., args: {TOKEN: hunter2b}}\n"
            "    command: [\"--password=hunter2c\"]\n"
            "    healthcheck: {test: \"mysqladmin -phunter2d\"}\n"
            "    labels: [\"auth=hunter2e\"]\n"
            "secrets:\n"
            "  s: {content: hunter2f}\n"
        )
        out = redact_compose(text)
        self.assertNotIn("hunter2", out)
        self.assertIn("DB_PASSWORD", out)
        self.assertIn("postgres:16", out)

    def test_yaml_alias_bomb_is_refused(self):
        from kowan.redact import redact_compose

        lines, prev = ["a: &a [x,x,x,x,x,x,x,x,x]"], "a"
        for n in "bcdefghi":
            lines.append(f"{n}: &{n} [" + ",".join([f"*{prev}"] * 9) + "]")
            prev = n
        bomb = "\n".join(lines) + f"\nservices:\n  w:\n    image: x\n    ports: *{prev}\n"
        self.assertIn("could not be parsed", redact_compose(bomb))

    def test_git_credentials_stripped(self):
        from kowan.redact import strip_userinfo

        self.assertEqual(strip_userinfo("https://bob:tok@github.com/a/b.git"), "https://github.com/a/b.git")
        self.assertEqual(strip_userinfo("git@github.com:a/b.git"), "git@github.com:a/b.git")

    def test_unparsable_compose_is_hidden(self):
        from kowan.redact import redact_compose

        self.assertNotIn("hunter2", redact_compose("services: [\n  PASSWORD: hunter2"))


class TestRunHistory(ViewerTestCase):
    async def test_run_output_is_operator_only(self):
        r = await self.client.post("/api/v1/workflows", headers=self.admin, json={
            "name": "n", "steps": [{"name": "a", "type": "notify", "config": {"message": "hi"}}]})
        wid = (await r.json())["id"]
        r = await self.client.get(f"/api/v1/workflows/{wid}/runs", headers=self.h["viewer"])
        self.assertEqual(r.status, 403)
        r = await self.client.get(f"/api/v1/workflows/{wid}/runs/any", headers=self.h["viewer"])
        self.assertEqual(r.status, 403)
        r = await self.client.get(f"/api/v1/workflows/{wid}/runs", headers=self.h["operator"])
        self.assertEqual(r.status, 200)


if __name__ == "__main__":
    unittest.main()
