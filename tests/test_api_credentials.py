"""Credentials API: secrets, registries (against a fake registry), notification
channels (against a fake receiver), reference preview, masking and roles."""

import json
import unittest

from tests.apicase import ServerCase
from tests.fakeservices import FakeReceiver, FakeRegistry


class TestSecrets(ServerCase):
    async def test_crud_masking_reveal(self):
        await self.roles("POST", "/credentials", "admin", json={"kind": "secret", "name": "x", "fields": {"value": "v"}})
        _, s = await self.call("POST", "/credentials", "admin", json={
            "kind": "secret", "name": "db", "description": "prod db", "fields": {"user": "app", "password": "pw-123"}},
            expect=201)
        self.assertNotIn("pw-123", json.dumps(s))
        _, listing = await self.call("GET", "/credentials", "admin", expect=200)
        self.assertNotIn("pw-123", json.dumps(listing))
        self.assertIn("db", [c["name"] for c in listing])
        _, got = await self.call("GET", f"/credentials/{s['id']}", "admin", expect=200)
        self.assertNotIn("pw-123", json.dumps(got))
        _, revealed = await self.call("POST", f"/credentials/{s['id']}/reveal", "admin", expect=200)
        self.assertEqual(revealed["fields"]["password"], "pw-123")

        _, upd = await self.call("PUT", f"/credentials/{s['id']}", "admin", json={
            "name": "db", "fields": {"user": "app", "password": "pw-456"}}, expect=200)
        _, revealed = await self.call("POST", f"/credentials/{s['id']}/reveal", "admin", expect=200)
        self.assertEqual(revealed["fields"]["password"], "pw-456")

        _, prev = await self.call("POST", "/credentials/resolve", "admin",
                                  json={"text": "pass=[[db.password]] none=[[missing]]"}, expect=200)
        refs = {r["ref"] if "ref" in r else r.get("token", ""): r for r in prev["references"]}
        self.assertEqual(len(prev["references"]), 2)
        self.assertNotIn("pw-456", json.dumps(prev))

        await self.call("DELETE", f"/credentials/{s['id']}", "admin", expect=200)
        await self.call("GET", f"/credentials/{s['id']}", "admin", expect=404)

    async def test_validation(self):
        for bad, why in (({"kind": "nope", "name": "a"}, "kind"), ({"kind": "secret", "name": ""}, "name"),
                         ({"kind": "secret", "name": "a", "fields": {}}, "fields"),
                         ({"kind": "secret", "name": "a", "type": "bogus", "fields": {"v": "1"}}, "type"),
                         ({"kind": "registry", "name": "r"}, "url"),
                         ({"kind": "channel", "name": "c", "type": "pigeon"}, "channel type"),
                         ({"kind": "connection", "name": "c", "type": "nothing"}, "connection type")):
            status, _ = await self.call("POST", "/credentials", "admin", json=bad)
            self.assertEqual(status, 400, why)
        _, types = await self.call("GET", "/credentials/types", "admin", expect=200)
        self.assertIn("channel", types)


class TestRegistries(ServerCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.registry = FakeRegistry()
        self.addr = await self.registry.start()

    async def asyncTearDown(self):
        await self.registry.stop()
        await super().asyncTearDown()

    async def test_validate_browse_and_mask(self):
        # Without a scheme the registry is assumed HTTPS: a plain-HTTP one says so.
        _, plain = await self.call("POST", "/credentials", "admin", json={
            "kind": "registry", "name": "noscheme", "url": self.addr, "username": "bob", "password": "pw"}, expect=201)
        _, res = await self.call("POST", f"/credentials/{plain['id']}/test", "admin", expect=200)
        self.assertFalse(res["verified"])
        self.assertIn(f"http://{self.addr}", res["error"])

        _, reg = await self.call("POST", "/credentials", "admin", json={
            "kind": "registry", "name": "private", "url": "http://" + self.addr, "username": "bob", "password": "pw"},
            expect=201)
        self.assertNotIn('"pw"', json.dumps(reg))
        _, res = await self.call("POST", f"/credentials/{reg['id']}/test", "admin", expect=200)
        self.assertTrue(res["verified"], res)
        self.assertTrue(res["insecure"])  # plain HTTP registry
        _, cat = await self.call("GET", f"/credentials/{reg['id']}/catalog", "admin", expect=200)
        self.assertIn("app/web", json.dumps(cat))
        _, tags = await self.call("GET", f"/credentials/{reg['id']}/tags", "admin", params={"repo": "app/web"}, expect=200)
        self.assertEqual(tags["tags"], ["1.0", "1.1", "latest"])

    async def test_browse_endpoints_for_selectors(self):
        _, reg = await self.call("POST", "/credentials", "admin", json={
            "kind": "registry", "name": "sel", "url": "http://" + self.addr, "username": "bob", "password": "pw"},
            expect=201)
        base = f"/credentials/{reg['id']}/registry"
        await self.roles("GET", base + "/repositories", "operator")
        _, repos = await self.call("GET", base + "/repositories", "operator", expect=200)
        self.assertEqual(repos["repositories"], ["app/web", "tools"])
        _, tags = await self.call("GET", base + "/tags", "operator", params={"repository": "tools"}, expect=200)
        self.assertEqual(tags["tags"], ["v2"])
        await self.call("GET", base + "/tags", "operator", expect=400)
        # Best effort: a failing registry gives an empty list, not an error.
        _, tags = await self.call("GET", base + "/tags", "operator", params={"repository": "nope"}, expect=200)
        self.assertEqual(tags["tags"], [])
        _, s = await self.call("POST", "/credentials", "admin", json={
            "kind": "secret", "name": "notreg", "fields": {"value": "x"}}, expect=201)
        _, repos = await self.call("GET", f"/credentials/{s['id']}/registry/repositories", "operator", expect=200)
        self.assertEqual(repos["repositories"], [])
        await self.call("GET", "/credentials/missing/registry/repositories", "operator", expect=404)
        await self.call("GET", "/credentials/missing/registry/tags", "operator", params={"repository": "x"},
                        expect=404)

    async def test_wrong_password_is_reported(self):
        _, reg = await self.call("POST", "/credentials", "admin", json={
            "kind": "registry", "name": "bad", "url": "http://" + self.addr, "username": "bob", "password": "wrong"}, expect=201)
        _, res = await self.call("POST", f"/credentials/{reg['id']}/test", "admin", expect=200)
        self.assertFalse(res["verified"])
        self.assertIn("rejected the username or password", res["error"])

    async def test_bearer_registry_checks_credentials_at_the_token_endpoint(self):
        self.registry.bearer = True
        for password, ok in (("pw", True), ("wrong", False)):
            _, reg = await self.call("POST", "/credentials", "admin", json={
                "kind": "registry", "name": "hub-" + password, "url": "http://" + self.addr,
                "username": "bob", "password": password}, expect=201)
            _, res = await self.call("POST", f"/credentials/{reg['id']}/test", "admin", expect=200)
            self.assertEqual(res["verified"], ok, res)


class TestChannels(ServerCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.receiver = FakeReceiver()
        self.url = await self.receiver.start()

    async def asyncTearDown(self):
        await self.receiver.stop()
        await super().asyncTearDown()

    async def test_webhook_channel_test_send(self):
        _, ch = await self.call("POST", "/credentials", "admin", json={
            "kind": "channel", "name": "ops", "type": "webhook", "enabled": True, "minLevel": "warning",
            "config": {"url": self.url + "/hook"}}, expect=201)
        _, res = await self.call("POST", f"/credentials/{ch['id']}/test", "admin", expect=200)
        self.assertTrue(res["ok"], res)
        self.assertEqual(len(self.receiver.received), 1)
        self.assertIn("Kowan test", json.dumps(self.receiver.received[0]["body"]))

    async def test_failing_receiver_is_reported(self):
        self.receiver.status = 500
        _, ch = await self.call("POST", "/credentials", "admin", json={
            "kind": "channel", "name": "down", "type": "webhook", "config": {"url": self.url + "/hook"}}, expect=201)
        _, res = await self.call("POST", f"/credentials/{ch['id']}/test", "admin", expect=200)
        self.assertFalse(res["ok"])
        status, _ = await self.call("POST", "/credentials", "admin", json={
            "kind": "channel", "name": "lvl", "type": "webhook", "minLevel": "loud", "config": {"url": self.url}})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
