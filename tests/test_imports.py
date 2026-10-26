"""Imports: Portainer stacks through a fake Portainer API (token and login,
compose and git and swarm and kubernetes stacks, errors), compose folders
grouped from posted files, and apply creating deployments with the same
checks as POST /deployments."""

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

    async def test_portainer_scan(self):
        await self.roles("POST", "/import/portainer/scan", "admin", json={"url": self.p.url, "apiKey": TOKEN})
        await self.call("POST", "/deployments", json={"name": "metrics", "type": "compose", "strategy": "standalone",
                                                       "targets": ["local"], "compose": "services: {}"}, expect=201)
        _, items = await self.call("POST", "/import/portainer/scan", json={"url": self.p.url, "apiKey": TOKEN}, expect=200)
        by = {i["name"]: i for i in items}
        shop = by["shop"]
        self.assertEqual((shop["source"], shop["host"], shop["strategy"], shop["envCount"]), ("portainer", "local", "standalone", 3))
        # The compose stays a template; every variable is offered as env.
        self.assertIn("nginx:${TAG}", shop["compose"])
        self.assertNotIn("s3cret", shop["compose"])
        self.assertEqual(shop["env"], {"secret": "shop-env", "inImage": ["TAG"], "values": {
            "TAG": "1.27", "DB_PASSWORD": "s3cret", "DATABASE_URL": "postgres://shop:s3cret@db/shop"}})
        self.assertEqual(shop["warnings"], [])
        self.assertNotIn("env", by["metrics"])
        blog = by["blog"]
        self.assertEqual(blog["git"]["ref"], "prod")
        self.assertEqual(len(blog["warnings"]), 2)
        self.assertEqual(blog["host"], "local")  # no host called edge-site: the first one
        self.assertEqual(by["metrics"]["strategy"], "swarm")
        self.assertIn("exists already", by["metrics"]["warnings"][0])
        self.assertFalse(by["k8s-app"]["importable"])
        for body, msg in (({"url": "nope", "apiKey": "x"}, "http"), ({"url": self.p.url, "apiKey": "bad"}, "refused"),
                          ({"url": self.p.url, "apiKey": TOKEN, "x": 1}, "")):
            _, err = await self.call("POST", "/import/portainer/scan", json=body, expect=400)
            self.assertIn(msg, err["error"])
        await self.call("POST", "/import/portainer/scan", data=b"[1]", expect=400)

    async def test_portainer_variables_become_a_secret(self):
        _, items = await self.call("POST", "/import/portainer/scan", json={"url": self.p.url, "apiKey": TOKEN}, expect=200)
        shop = next(i for i in items if i["name"] == "shop")
        env = {**shop["env"], "inline": ["TAG"]}
        item = {"name": "shop", "host": "local", "compose": shop["compose"], "env": env}
        # Secrets are admin-only: an operator's import that needs one is refused...
        _, res = await self.call("POST", "/import/apply", "operator", json={"items": [item]}, expect=200)
        self.assertIn("only an admin", res[0]["error"])
        # ...but with every variable inlined there is no secret to store.
        _, res = await self.call("POST", "/import/apply", "operator", json={"items": [
            {**item, "name": "shop-all", "env": {**env, "inline": list(env["values"])}}]}, expect=200)
        self.assertTrue(res[0]["ok"], res)
        self.assertIn("PASSWORD=s3cret", self.svc.store.get_deployment(res[0]["id"]).compose)
        self.assertEqual(self.svc.store.list_credentials("secret"), [])

        _, res = await self.call("POST", "/import/apply", json={"items": [
            item,
            {**item, "name": "shop2"},  # the secret exists by now
            {**item, "name": "bad", "env": {**env, "secret": "a.b"}},
            {**item, "name": "bad2", "env": {**env, "values": {"1X": "1"}}},
            {**item, "name": "bad3", "env": {**env, "x": 1}},
            {**item, "name": "bad4", "env": {**env, "inline": ["NOPE"]}},
        ]}, expect=200)
        self.assertEqual([r["ok"] for r in res], [True, False, False, False, False, False])
        self.assertIn('secret named "shop-env" exists', res[1]["error"])
        self.assertIn("env.secret", res[2]["error"])
        self.assertIn("env.values", res[3]["error"])
        self.assertIn("env must be", res[4]["error"])
        self.assertIn("env.inline", res[5]["error"])
        dep = self.svc.store.get_deployment(res[0]["id"])
        self.assertIn("image: nginx:1.27", dep.compose)  # inlined
        self.assertIn("PASSWORD=[[shop-env.DB_PASSWORD]]", dep.compose)
        self.assertNotIn("s3cret", dep.compose)
        resolved = self.svc.store.resolve_secrets(dep.compose)
        self.assertIn("PASSWORD=s3cret", resolved)
        self.assertIn("URL=postgres://shop:s3cret@db/shop", resolved)
        secret = self.svc.store.list_credentials("secret")[0]
        self.assertEqual(sorted(self.svc.store.credential_config(secret.id)), ["DATABASE_URL", "DB_PASSWORD"])
        # A rescan numbers the next secret; a refused deployment leaves no secret behind.
        _, items = await self.call("POST", "/import/portainer/scan", json={"url": self.p.url, "apiKey": TOKEN}, expect=200)
        again = next(i for i in items if i["name"] == "shop")
        self.assertEqual(again["env"]["secret"], "shop-env-2")
        _, res = await self.call("POST", "/import/apply", json={"items": [
            {"name": "shop", "host": "local", "compose": again["compose"], "env": again["env"]}]}, expect=200)
        self.assertIn("already exists", res[0]["error"])
        self.assertEqual([c.name for c in self.svc.store.list_credentials("secret")], ["shop-env"])

        # Later, chosen references are written into the compose.
        path = f"/deployments/{dep.id}/inline-secrets"
        await self.roles("POST", path, "admin", json={"refs": ["shop-env.DB_PASSWORD"]})
        for bad in ({}, {"refs": []}, {"refs": [1]}, {"refs": ["x"], "y": 1}, {"refs": ["shop-env.NOPE"]}):
            await self.call("POST", path, json=bad, expect=400)
        _, out = await self.call("POST", path, json={"refs": ["shop-env.DB_PASSWORD"]}, expect=200)
        self.assertIn("PASSWORD=s3cret", out["compose"])
        self.assertIn("URL=[[shop-env.DATABASE_URL]]", out["compose"])
        _, revs = await self.call("GET", f"/deployments/{dep.id}/revisions", expect=200)
        self.assertEqual((revs[0]["source"], revs[0]["note"]), ("edit", "wrote shop-env.DB_PASSWORD into the compose"))
        await self.call("POST", "/deployments/missing/inline-secrets", json={"refs": ["a"]}, expect=404)
        # A reference whose secret is gone does not resolve.
        def set_compose(text):
            d = self.svc.store.get_deployment(dep.id)
            d.compose = text
            self.svc.store.update_deployment(d)
        set_compose("services:\n  web:\n    image: nginx\n    environment: [A=[[gone.X]]]\n")
        _, err = await self.call("POST", path, json={"refs": ["gone.X"]}, expect=400)
        self.assertIn("does not resolve", err["error"])
        # A value that breaks the YAML is refused, and the compose is kept.
        self.svc.store.create_credential(secret.__class__(id="c2", kind="secret", type="generic", name="yaml",
                                                          createdAt="t", updatedAt="t"), {"value": "a: [b"})
        set_compose("services:\n  web:\n    image: nginx\n    command: [[yaml]]\n")
        await self.call("POST", path, json={"refs": ["yaml"]}, expect=400)
        self.assertIn("[[yaml]]", self.svc.store.get_deployment(dep.id).compose)

    async def test_compose_scan_and_apply(self):
        await self.roles("POST", "/import/compose/scan", "operator", json={"files": {"a/compose.yml": "services: {}"}})
        files = {"stacks/shop/compose.yml": "services:\n  web:\n    image: nginx:${TAG}\n", "stacks/shop/.env": "# c\nTAG=1.28\n",
                 "stacks/blog/docker-compose.yml": "services:\n  ghost:\n    image: ghost:5\n"}
        _, items = await self.call("POST", "/import/compose/scan", "operator", json={"files": files}, expect=200)
        by = {i["name"]: i for i in items}
        self.assertEqual(sorted(by), ["blog", "shop"])
        self.assertIn("nginx:1.28", by["shop"]["compose"])
        self.assertEqual((by["shop"]["envCount"], by["shop"]["endpoint"]), (1, "stacks/shop/compose.yml"))
        for body in ({"files": {}}, {"files": []}, {"files": {"a": 1}}, {"files": {"a": "x" * (1 << 20 + 1)}},
                     {"files": {str(i): "" for i in range(501)}}, {"x": 1}):
            await self.call("POST", "/import/compose/scan", json=body, expect=400)

        await self.roles("POST", "/import/apply", "operator", json={"items": [{"name": "x"}]})
        _, res = await self.call("POST", "/import/apply", "operator", json={"items": [
            {"name": "shop", "host": "local", "compose": by["shop"]["compose"]},
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
        self.assertIn("nginx:1.28", shop.compose)
        site = self.svc.store.get_deployment(res[2]["id"])
        self.assertEqual((site.git.ref, site.git.path, site.git.auto_deploy), ("prod", "c.yml", False))
        _, revs = await self.call("GET", f"/deployments/{shop.id}/revisions", expect=200)
        self.assertEqual((revs[0]["source"], revs[0]["author"]), ("import", "operator"))
        for body in ({"items": []}, {"items": "x"}, {"items": [{}] * 101}, {"items": [{}], "x": 1}):
            await self.call("POST", "/import/apply", json=body, expect=400)


if __name__ == "__main__":
    unittest.main()
