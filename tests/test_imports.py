"""Imports: Portainer stacks through a fake Portainer API reached by a saved
connection (token and login, compose and git and swarm and kubernetes stacks,
errors), compose folders grouped from posted files, environments turned into
stack variables (secret ones into the deployment's own secret), and apply
creating deployments with the same checks as POST /deployments."""

import socket
import unittest

from aiohttp import web

from kowan.importer import compose_dir
from kowan.importer.portainer import Client, PortainerError, validate_url
from tests.apicase import ServerCase

TOKEN = "ptr_secret"


class FakePortainer:
    def __init__(self):
        self.stacks = [
            {"Id": 1, "Name": "shop", "Type": 2, "EndpointId": 1, "Status": 1,
             "Env": [{"name": "TAG", "value": "1.27"}, {"name": "DB_PASSWORD", "value": "s3cret"},
                     {"name": "DATABASE_URL", "value": "postgres://shop:s3cret@db/shop"}]},
            {"Id": 2, "Name": "blog", "Type": 2, "EndpointId": 2, "Status": 2, "Env": [{"name": "X", "value": "1"}],
             "GitConfig": {"URL": "https://github.com/acme/blog.git", "ReferenceName": "refs/heads/prod",
                           "ConfigFilePath": "deploy/compose.yml", "Authentication": {"Username": "bot"}}},
            {"Id": 3, "Name": "metrics", "Type": 1, "EndpointId": 1, "Status": 1},
            {"Id": 4, "Name": "k8s-app", "Type": 3, "EndpointId": 1},
        ]
        self.files = {1: "services:\n  web:\n    image: nginx:${TAG}\n    environment:\n"
                         "      - PASSWORD=${DB_PASSWORD}\n      - URL=${DATABASE_URL}\n", 3: "services:\n  prom:\n    image: prom/prometheus\n"}
        self.not_json = False

    def _authed(self, request):
        return request.headers.get("X-API-Key") == TOKEN or request.headers.get("Authorization") == "Bearer jwt-1"

    async def auth(self, request):
        body = await request.json()
        if body == {"Username": "admin", "Password": "pw"}:
            return web.json_response({"jwt": "jwt-1"})
        if body.get("Username") == "nojwt":
            return web.json_response({})
        if body.get("Username") == "boom":
            return web.json_response({}, status=500)
        return web.json_response({"message": "Invalid credentials"}, status=422)

    async def endpoints(self, request):
        if not self._authed(request):
            return web.json_response({}, status=401)
        if self.not_json:
            return web.Response(text="<html>login</html>")
        return web.json_response([{"Id": 1, "Name": "local"}, {"Id": 2, "Name": "edge-site"}])

    async def stacks_(self, request):
        if not self._authed(request):
            return web.json_response({}, status=401)
        return web.json_response(self.stacks)

    async def file(self, request):
        sid = int(request.match_info["id"])
        if sid not in self.files:
            return web.json_response({}, status=404)
        return web.json_response({"StackFileContent": self.files[sid]})

    async def start(self):
        app = web.Application()
        app.router.add_post("/api/auth", self.auth)
        app.router.add_get("/api/endpoints", self.endpoints)
        app.router.add_get("/api/stacks", self.stacks_)
        app.router.add_get("/api/stacks/{id}/file", self.file)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        await web.TCPSite(self.runner, "127.0.0.1", self.port).start()
        self.url = f"http://127.0.0.1:{self.port}/"
        return self

    async def stop(self):
        await self.runner.cleanup()


class TestComposeFolders(unittest.TestCase):
    def test_group(self):
        files = {
            "stacks/shop/compose.yaml": "services: {}",
            "stacks/shop/docker-compose.yml": "ignored: true",
            "stacks/shop/.env": "TAG=1",
            "stacks/blog/docker-compose.yml": "b",
            "stacks/notes/README.md": "no compose here",
            "stacks/../etc/compose.yml": "escaped",
            "compose.yml": "root",
            "win\\path\\compose.yml": "backslashes",
            "": "empty",
        }
        out = {g["folder"]: g for g in compose_dir.group(files)}
        self.assertEqual(sorted(out), ["", "stacks/blog", "stacks/shop", "win/path"])
        self.assertEqual((out["stacks/shop"]["name"], out["stacks/shop"]["file"], out["stacks/shop"]["env"]), ("shop", "compose.yaml", "TAG=1"))
        self.assertEqual(out["stacks/blog"]["env"], "")
        self.assertEqual(out[""]["name"], "stack")


class TestPortainerClient(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.p = await FakePortainer().start()

    async def asyncTearDown(self):
        await self.p.stop()

    async def test_token_and_login(self):
        stacks = await Client(self.p.url, api_key=TOKEN).stacks()
        self.assertEqual([s.name for s in stacks], ["shop", "blog", "metrics", "k8s-app"])
        shop, blog, metrics, k8s = stacks
        self.assertEqual((shop.kind, shop.endpoint, shop.env["TAG"], shop.active), ("compose", "local", "1.27", True))
        self.assertIn("nginx:${TAG}", shop.compose)
        self.assertEqual(blog.git, {"repoUrl": "https://github.com/acme/blog.git", "ref": "prod", "path": "deploy/compose.yml"})
        self.assertTrue(blog.git_auth)
        self.assertFalse(blog.active)
        self.assertEqual((metrics.kind, k8s.kind, k8s.compose), ("swarm", "kubernetes", ""))
        self.assertEqual(len(await Client(self.p.url, username="admin", password="pw").stacks()), 4)

    async def test_errors(self):
        for kw, msg in (({"api_key": "wrong"}, "refused the credentials"), ({}, "access token"),
                        ({"username": "admin", "password": "bad"}, "refused the username"),
                        ({"username": "nojwt", "password": "x"}, "no token"),
                        ({"username": "boom", "password": "x"}, "answered 500")):
            with self.assertRaises(PortainerError) as cm:
                await Client(self.p.url, **kw).stacks()
            self.assertIn(msg, str(cm.exception))
        self.p.not_json = True
        with self.assertRaisesRegex(PortainerError, "not JSON"):
            await Client(self.p.url, api_key=TOKEN).stacks()
        self.p.not_json = False
        self.p.stacks.append({"Id": 9, "Name": "gone", "Type": 2, "EndpointId": 1})
        with self.assertRaisesRegex(PortainerError, "answered 404"):
            await Client(self.p.url, api_key=TOKEN).stacks()
        await self.p.stop()
        with self.assertRaisesRegex(PortainerError, "cannot reach Portainer"):
            await Client(self.p.url, api_key=TOKEN).stacks()
        self.p = await FakePortainer().start()  # for tearDown
        with self.assertRaises(ValueError):
            validate_url("ftp://x")
        self.assertEqual(validate_url(" https://p.example.com/ "), "https://p.example.com")


class TestImportAPI(ServerCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.p = await FakePortainer().start()

    async def asyncTearDown(self):
        await self.p.stop()
        await super().asyncTearDown()

    async def connection(self, **config) -> str:
        _, c = await self.call("POST", "/credentials", json={
            "kind": "connection", "type": config.pop("type", "portainer"), "name": config.pop("name", "portainer"),
            "config": config}, expect=201)
        return c["id"]

    async def scan(self, conn: str, **kw):
        return await self.call("POST", "/import/portainer/scan", json={"connectionId": conn}, **kw)

    async def test_portainer_scan(self):
        conn = await self.connection(url=self.p.url, apiKey=TOKEN)
        await self.roles("POST", "/import/portainer/scan", "admin", json={"connectionId": conn})
        await self.call("POST", "/deployments", json={"name": "metrics", "type": "compose", "strategy": "standalone",
                                                       "targets": ["local"], "compose": "services: {}"}, expect=201)
        _, items = await self.scan(conn, expect=200)
        by = {i["name"]: i for i in items}
        shop = by["shop"]
        self.assertEqual((shop["source"], shop["host"], shop["strategy"], shop["envCount"]), ("portainer", "local", "standalone", 3))
        # The compose stays as written; the environment becomes stack variables,
        # the sensitive-looking ones proposed as secrets.
        self.assertIn("nginx:${TAG}", shop["compose"])
        self.assertEqual(shop["variables"], [
            {"key": "TAG", "value": "1.27", "secret": False},
            {"key": "DB_PASSWORD", "value": "s3cret", "secret": True},
            {"key": "DATABASE_URL", "value": "postgres://shop:s3cret@db/shop", "secret": True},
        ])
        self.assertEqual(shop["warnings"], [])
        blog = by["blog"]
        self.assertEqual(blog["git"]["ref"], "prod")
        self.assertEqual(len(blog["warnings"]), 2)
        self.assertNotIn("variables", blog)
        self.assertEqual(blog["host"], "local")  # no host called edge-site: the first one
        self.assertEqual(by["metrics"]["strategy"], "swarm")
        self.assertIn("exists already", by["metrics"]["warnings"][0])
        self.assertEqual(by["metrics"]["variables"], [])
        self.assertFalse(by["k8s-app"]["importable"])

        # Login instead of a token, and a self-signed setting that parses.
        login = await self.connection(name="by-login", url=self.p.url, username="admin", password="pw", insecure="true")
        _, items = await self.scan(login, expect=200)
        self.assertEqual(len(items), 4)

        other = await self.connection(type="basic-auth", name="smb", username="u", password="p")
        bad_token = await self.connection(name="bad", url=self.p.url, apiKey="bad")
        bad_url = await self.connection(name="nope", url="nope", apiKey="x")
        for conn_id, status, msg in (("", 400, "connectionId"), ("missing", 404, "not found"),
                                     (other, 400, "not a Portainer"), (bad_token, 400, "refused"), (bad_url, 400, "http")):
            _, err = await self.scan(conn_id, expect=status)
            self.assertIn(msg, err["error"])
        await self.call("POST", "/import/portainer/scan", json={"connectionId": conn, "url": "x"}, expect=400)
        await self.call("POST", "/import/portainer/scan", data=b"[1]", expect=400)

    async def test_variables_become_stack_variables_and_secrets(self):
        conn = await self.connection(url=self.p.url, apiKey=TOKEN)
        _, items = await self.scan(conn, expect=200)
        shop = next(i for i in items if i["name"] == "shop")
        item = {"name": "shop", "host": "local", "compose": shop["compose"], "variables": shop["variables"]}
        # An operator may import secrets: they go to the deployment's own secret.
        _, res = await self.call("POST", "/import/apply", "operator", json={"items": [
            item,
            {**item, "name": "bad", "variables": "x"},
            {**item, "name": "bad2", "variables": [{"key": "1X", "value": "1"}]},
        ]}, expect=200)
        self.assertEqual([r["ok"] for r in res], [True, False, False])
        self.assertIn("variables must be a list", res[1]["error"])
        self.assertIn("1X", res[2]["error"])
        dep = self.svc.store.get_deployment(res[0]["id"])
        self.assertEqual(dep.compose, shop["compose"].strip())
        self.assertEqual([(v.key, v.value, v.secret) for v in dep.variables], [
            ("TAG", "1.27", False),
            ("DB_PASSWORD", "[[shop-env.DB_PASSWORD]]", True),
            ("DATABASE_URL", "[[shop-env.DATABASE_URL]]", True),
        ])
        secret = self.svc.store.list_credentials("secret")[0]
        self.assertEqual((secret.name, secret.meta["deploymentId"]), ("shop-env", dep.id))
        self.assertEqual(self.svc.store.credential_config(secret.id),
                         {"DB_PASSWORD": "s3cret", "DATABASE_URL": "postgres://shop:s3cret@db/shop"})
        # A refused deployment leaves no secret behind.
        _, res = await self.call("POST", "/import/apply", json={"items": [{**item, "host": "nowhere"}]}, expect=200)
        self.assertFalse(res[0]["ok"])
        self.assertEqual(len(self.svc.store.list_credentials("secret")), 1)

    async def test_compose_scan_and_apply(self):
        await self.roles("POST", "/import/compose/scan", "operator", json={"files": {"a/compose.yml": "services: {}"}})
        files = {"stacks/shop/compose.yml": "services:\n  web:\n    image: nginx:${TAG}\n", "stacks/shop/.env": "# c\nTAG=1.28\n",
                 "stacks/blog/docker-compose.yml": "services:\n  ghost:\n    image: ghost:5\n"}
        _, items = await self.call("POST", "/import/compose/scan", "operator", json={"files": files}, expect=200)
        by = {i["name"]: i for i in items}
        self.assertEqual(sorted(by), ["blog", "shop"])
        # The .env becomes stack variables; the compose stays as written.
        self.assertIn("nginx:${TAG}", by["shop"]["compose"])
        self.assertEqual(by["shop"]["variables"], [{"key": "TAG", "value": "1.28", "secret": False}])
        self.assertEqual((by["shop"]["envCount"], by["shop"]["endpoint"]), (1, "stacks/shop/compose.yml"))
        for body in ({"files": {}}, {"files": []}, {"files": {"a": 1}}, {"files": {"a": "x" * (1 << 20 + 1)}},
                     {"files": {str(i): "" for i in range(501)}}, {"x": 1}):
            await self.call("POST", "/import/compose/scan", json=body, expect=400)

        await self.roles("POST", "/import/apply", "operator", json={"items": [{"name": "x"}]})
        _, res = await self.call("POST", "/import/apply", "operator", json={"items": [
            {"name": "shop", "host": "local", "compose": by["shop"]["compose"], "variables": by["shop"]["variables"]},
            {"name": "blog", "host": "local", "strategy": "standalone", "compose": by["blog"]["compose"]},
            {"name": "site", "host": "local", "git": {"repoUrl": "https://github.com/acme/site.git", "ref": "prod", "path": "c.yml"}},
            {"name": "shop", "host": "local", "compose": "services: {}"},
            {"name": "ghost", "host": "nowhere", "compose": "services: {}"},
            {"name": "broken", "host": "local", "compose": "services: ["},
            "not-an-item",
        ]}, expect=200)
        self.assertEqual([r["ok"] for r in res], [True, True, True, False, False, False, False])
        self.assertIn("already exists", res[3]["error"])
        self.assertIn("unknown host", res[4]["error"])
        self.assertIn("compose", res[5]["error"])
        self.assertEqual(res[6]["error"], "invalid item")
        shop = self.svc.store.get_deployment(res[0]["id"])
        self.assertEqual((shop.status, shop.type), ("stopped", "compose"))
        self.assertEqual([(v.key, v.value) for v in shop.variables], [("TAG", "1.28")])
        site = self.svc.store.get_deployment(res[2]["id"])
        self.assertEqual((site.git.ref, site.git.path, site.git.auto_deploy), ("prod", "c.yml", False))
        _, revs = await self.call("GET", f"/deployments/{shop.id}/revisions", expect=200)
        self.assertEqual((revs[0]["source"], revs[0]["author"]), ("import", "operator"))
        for body in ({"items": []}, {"items": "x"}, {"items": [{}] * 101}, {"items": [{}], "x": 1}):
            await self.call("POST", "/import/apply", json=body, expect=400)


if __name__ == "__main__":
    unittest.main()
