"""More of the credentials API: shared connections, per-kind updates (registry
password + re-validation, channel partial updates, secret renames/types), list
filters, and external secret backends (Vault KV v2, Infisical) resolved live
against a fake server, including at deploy time."""

import json
import unittest

from aiohttp import web
from aiohttp.test_utils import TestServer

from tests.apicase import ServerCase
from tests.fakecli import FakeDockerCLI
from tests.fakeservices import FakeRegistry


class FakeSecretServers:
    """Vault KV v2 and Infisical on one aiohttp server."""

    def __init__(self):
        self.vault = {"app/db": {"password": "vault-pw", "nested": {"a": 1}}}

    async def start(self) -> str:
        app = web.Application()
        app.router.add_get("/v1/{mount}/data/{path:.*}", self._vault)
        app.router.add_post("/api/v1/auth/universal-auth/login", self._inf_login)
        app.router.add_get("/api/v3/secrets/raw/{name}", self._inf_read)
        app.router.add_get("/v1/secrets/", self._phase)
        self.server = TestServer(app)
        await self.server.start_server()
        return str(self.server.make_url("")).rstrip("/")

    async def stop(self):
        await self.server.close()

    async def _vault(self, request):
        if request.headers.get("X-Vault-Token") != "root-token":
            return web.json_response({"errors": ["permission denied"]}, status=403)
        data = self.vault.get(request.match_info["path"])
        if data is None:
            return web.json_response({"errors": []}, status=404)
        return web.json_response({"data": {"data": data}})

    async def _inf_login(self, request):
        body = await request.json()
        if body.get("clientSecret") != "cs":
            return web.json_response({"message": "bad"}, status=401)
        return web.json_response({"accessToken": "inf-token"})

    async def _inf_read(self, request):
        if request.headers.get("Authorization") != "Bearer inf-token":
            return web.json_response({}, status=401)
        if request.query.get("environment") != "prod":
            return web.json_response({"message": "no env"}, status=404)
        return web.json_response({"secret": {"secretValue": "inf-" + request.match_info["name"]}})


    async def _phase(self, request):
        if request.headers.get("Authorization") != "Bearer pss":
            return web.json_response({"error": "unauthorized"}, status=401)
        key = request.query.get("key")
        return web.json_response([{"key": "OTHER", "value": "x"}] + (
            [{"key": key, "value": "phase-" + key}] if key != "MISSING" else []))


class TestConnectionsAndUpdates(ServerCase):
    async def test_connection_crud(self):
        _, conn = await self.call("POST", "/credentials", "admin", json={
            "kind": "connection", "type": "github", "name": "gh", "config": {"token": "ghp_secret"}}, expect=201)
        self.assertNotIn("ghp_secret", json.dumps(conn))
        _, upd = await self.call("PUT", f"/credentials/{conn['id']}", "admin", json={
            "name": "gh2", "meta": {"note": "x"}, "config": {"token": "ghp_new"}}, expect=200)
        self.assertEqual(upd["name"], "gh2")
        _, conns = await self.call("GET", "/credentials", "admin", params={"kind": "connection"}, expect=200)
        self.assertEqual([c["name"] for c in conns], ["gh2"])
        await self.call("GET", "/credentials", "admin", params={"kind": "nope"}, expect=400)
        status, _ = await self.call("POST", "/credentials", "admin", json={"kind": "connection", "type": "", "name": "x"})
        self.assertEqual(status, 400)
        await self.call("DELETE", f"/credentials/{conn['id']}", "admin", expect=200)
        await self.call("DELETE", f"/credentials/{conn['id']}", "admin", expect=404)
        await self.call("PUT", f"/credentials/{conn['id']}", "admin", json={}, expect=404)

    async def test_channel_partial_update(self):
        _, ch = await self.call("POST", "/credentials", "admin", json={
            "kind": "channel", "name": "c", "type": "webhook", "config": {"url": "https://h.example"}}, expect=201)
        path = f"/credentials/{ch['id']}"
        _, upd = await self.call("PUT", path, "admin", json={"enabled": False, "minLevel": "critical"}, expect=200)
        self.assertEqual(upd["meta"]["minLevel"], "critical")
        await self.call("PUT", path, "admin", json={"name": ""}, expect=400)
        await self.call("PUT", path, "admin", json={"type": "slack"}, expect=400)
        await self.call("PUT", path, "admin", json={"minLevel": "loud"}, expect=400)
        await self.call("PUT", path, "admin", json=[1], expect=400)
        await self.call("POST", f"{path}/reveal", "admin", expect=400)

    async def test_secret_update_rules(self):
        _, s = await self.call("POST", "/credentials", "admin", json={
            "kind": "secret", "name": "s", "fields": {"value": "1"}}, expect=201)
        path = f"/credentials/{s['id']}"
        await self.call("PUT", path, "admin", json={"name": "", "fields": {"value": "2"}}, expect=400)
        await self.call("PUT", path, "admin", json={"name": "s", "type": "bogus", "fields": {"value": "2"}}, expect=400)
        await self.call("PUT", path, "admin", json={"name": "s2", "description": "d", "fields": {"value": "3"}},
                        expect=200)
        _, rev = await self.call("POST", f"{path}/reveal", "admin", expect=200)
        self.assertEqual(rev["fields"], {"value": "3"})
        await self.call("POST", "/credentials/missing/reveal", "admin", expect=404)

    async def test_registry_update_revalidates(self):
        reg = FakeRegistry()
        addr = await reg.start()
        try:
            _, r = await self.call("POST", "/credentials", "admin", json={
                "kind": "registry", "name": "priv", "url": "http://" + addr, "username": "bob",
                "password": "wrong"}, expect=201)
            self.assertFalse(r["meta"].get("verified"))
            _, upd = await self.call("PUT", f"/credentials/{r['id']}", "admin", json={
                "name": "priv", "url": "http://" + addr, "username": "bob", "password": "pw"}, expect=200)
            self.assertTrue(upd["meta"]["verified"], upd)
            await self.call("PUT", f"/credentials/{r['id']}", "admin", json={"name": "priv", "url": ""}, expect=400)
            await self.call("GET", f"/credentials/{r['id']}/tags", "admin", expect=400)
            await self.call("GET", "/credentials/missing/tags", "admin", params={"repo": "x"}, expect=404)
            await self.call("GET", "/credentials/missing/catalog", "admin", expect=404)
        finally:
            await reg.stop()


class TestExternalSecrets(ServerCase):
    async def asyncSetUp(self):
        self.cli = FakeDockerCLI()
        await super().asyncSetUp()
        self.backends = FakeSecretServers()
        self.url = await self.backends.start()

    async def asyncTearDown(self):
        await self.backends.stop()
        await super().asyncTearDown()
        self.cli.close()

    async def test_vault_and_infisical(self):
        _, v = await self.call("POST", "/credentials", "admin", json={
            "kind": "secret", "name": "db", "type": "vault",
            "fields": {"address": self.url, "token": "root-token", "path": "app/db", "key": "password"}}, expect=201)
        _, rev = await self.call("POST", f"/credentials/{v['id']}/reveal", "admin", expect=200)
        self.assertEqual(rev["fields"]["token"], "********")
        self.assertEqual(rev["fields"]["mount"], "secret")  # default applied
        _, inf = await self.call("POST", "/credentials", "admin", json={
            "kind": "secret", "name": "api", "type": "infisical",
            "fields": {"address": self.url, "clientId": "ci", "clientSecret": "cs", "projectId": "p",
                       "environment": "prod", "key": "API_KEY"}}, expect=201)
        _, prev = await self.call("POST", "/credentials/resolve", "admin",
                                  json={"text": "a=[[db]] b=[[api]] c=[[db.nested]]"}, expect=200)
        self.assertNotIn("vault-pw", json.dumps(prev))
        self.assertEqual([r["status"] for r in prev["references"]], ["resolved", "resolved", "error"], prev)
        self.assertIn("non-scalar", prev["references"][2]["message"])
        _, ph = await self.call("POST", "/credentials", "admin", json={
            "kind": "secret", "name": "ph", "type": "phase",
            "fields": {"address": self.url, "token": "pss", "appId": "a", "environment": "prod", "key": "DB"}},
            expect=201)
        _, prev = await self.call("POST", "/credentials/resolve", "admin",
                                  json={"text": "[[ph]] [[ph.MISSING]]"}, expect=200)
        self.assertEqual([r["status"] for r in prev["references"]], ["resolved", "error"], prev)
        self.assertEqual(prev["references"][0]["length"], len("phase-DB"))
        # A standalone external secret must carry its required fields.
        status, body = await self.call("POST", "/credentials", "admin", json={
            "kind": "secret", "name": "bad", "type": "vault", "fields": {"address": self.url}})
        self.assertEqual(status, 400, body)

        # Resolved live, on the wire only, when deploying.
        self.engine.add_image("nginx:1.27")
        _, dep = await self.call("POST", "/deployments", "operator", json={
            "name": "ext", "type": "compose", "strategy": "standalone", "targets": ["local"],
            "compose": "services:\n  app:\n    image: nginx:1.27\n    environment:\n      DB: '[[db]]'\n      K: '[[api]]'\n"})
        await self.call("POST", f"/deployments/{dep['id']}/deploy", "operator")
        for _ in range(200):
            _, d = await self.call("GET", f"/deployments/{dep['id']}", "operator")
            if d["status"] not in ("pending", "deploying"):
                break
            import asyncio
            await asyncio.sleep(0.05)
        self.assertEqual(d["status"], "deployed", d)
        sent = next(iter([c for c in self.cli.calls() if "up" in c["argv"]][-1]["files"].values()))
        self.assertIn("vault-pw", sent)
        self.assertIn("inf-API_KEY", sent)

    async def test_backend_errors_surface(self):
        _, v = await self.call("POST", "/credentials", "admin", json={
            "kind": "secret", "name": "denied", "type": "vault",
            "fields": {"address": self.url, "token": "wrong", "path": "app/db", "key": "password"}}, expect=201)
        _, prev = await self.call("POST", "/credentials/resolve", "admin", json={"text": "[[denied]]"}, expect=200)
        self.assertIn("403", prev["references"][0]["message"])
        _, inf = await self.call("POST", "/credentials", "admin", json={
            "kind": "secret", "name": "infbad", "type": "infisical",
            "fields": {"address": self.url, "clientId": "ci", "clientSecret": "nope", "projectId": "p",
                       "environment": "prod", "key": "K"}}, expect=201)
        _, prev = await self.call("POST", "/credentials/resolve", "admin", json={"text": "[[infbad]]"}, expect=200)
        self.assertEqual(prev["references"][0]["status"], "error")


if __name__ == "__main__":
    unittest.main()


class TestStoreFailuresAndKinds(ServerCase):
    """When the store fails the API answers 500 with a generic message (the
    detail is only logged), and per-kind catalog / test dispatch."""

    async def test_types_per_kind_and_test_dispatch(self):
        for kind in ("connection", "registry", "secret", "channel"):
            _, types = await self.call("GET", "/credentials/types", "admin", params={"kind": kind}, expect=200)
            self.assertTrue(types, kind)
        await self.call("GET", "/credentials/types", "admin", params={"kind": "potion"}, expect=400)
        _, s = await self.call("POST", "/credentials", "admin", json={
            "kind": "secret", "name": "s", "fields": {"value": "x"}}, expect=201)
        await self.call("POST", f"/credentials/{s['id']}/test", "admin", expect=400)
        await self.call("POST", "/credentials/missing/test", "admin", expect=404)
        _, ch = await self.call("POST", "/credentials", "admin", json={
            "kind": "channel", "name": "broken", "type": "smtp", "config": {"host": "127.0.0.1", "port": "9",
                                                                          "from": "a@b.io", "to": "c@d.io"}},
            expect=201)
        _, res = await self.call("POST", f"/credentials/{ch['id']}/test", "admin", expect=200)
        self.assertFalse(res["ok"])
        # A secret delegating to a shared connection; detach it again with an empty ref.
        _, conn = await self.call("POST", "/credentials", "admin", json={
            "kind": "connection", "type": "vault", "name": "v", "config": {"address": "https://v", "token": "t"}},
            expect=201)
        _, ext = await self.call("POST", "/credentials", "admin", json={
            "kind": "secret", "name": "ext", "type": "vault", "connectionRef": conn["id"],
            "fields": {"path": "a", "key": "b"}}, expect=201)
        _, upd = await self.call("PUT", f"/credentials/{ext['id']}", "admin", json={
            "name": "ext", "type": "vault", "fields": {"path": "a", "key": "c"}}, expect=200)
        await self.call("PUT", f"/credentials/{ext['id']}", "admin", json={
            "name": "ext", "type": "vault", "connectionRef": "", "fields": {"path": "a"}}, expect=400)

    async def test_store_errors_become_500(self):
        from unittest import mock

        _, s = await self.call("POST", "/credentials", "admin", json={
            "kind": "secret", "name": "s", "fields": {"value": "x"}}, expect=201)
        store = self.svc.store
        cases = [
            ("list_credentials", "GET", "/credentials", None),
            ("create_credential", "POST", "/credentials", {"kind": "secret", "name": "n", "fields": {"v": "1"}}),
            ("create_credential", "POST", "/credentials", {"kind": "connection", "type": "github", "name": "g",
                                                           "config": {"token": "t"}}),
            ("create_credential", "POST", "/credentials", {"kind": "channel", "type": "webhook", "name": "w",
                                                           "config": {"url": "https://x"}}),
            ("update_credential", "PUT", f"/credentials/{s['id']}", {"name": "s", "fields": {"value": "y"}}),
            ("delete_credential", "DELETE", f"/credentials/{s['id']}", None),
            ("credential_config", "POST", f"/credentials/{s['id']}/reveal", None),
            ("preview_references", "POST", "/credentials/resolve", {"text": "[[s]]"}),
        ]
        for method_name, http, path, body in cases:
            with mock.patch.object(store, method_name, side_effect=RuntimeError("disk on fire")):
                status, resp = await self.call(http, path, "admin", json=body)
            self.assertEqual(status, 500, (method_name, path, resp))
            self.assertNotIn("disk on fire", str(resp))
