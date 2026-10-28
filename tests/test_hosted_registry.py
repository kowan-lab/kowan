"""Deploying an image registry from Kowan: bcrypt htpasswd, the compose file
behind the host's Traefik, the login volume and the registry list entry."""

import unittest
from unittest import mock

from kowan.hostedregistry import bcrypt
from kowan.hostedregistry import service as hosted
from tests.apicase import ServerCase
from tests.fakecli import FakeDockerCLI
from tests.test_api_exposure import ExposureCase

# Made with Apache's `htpasswd -nbBC 4 alice 's3cret pass'`.
APACHE = "$2y$04$zPg/NJ.69mO3BRJnvz9RlOU67GJbp0.wOXqhR7skS.SN1.Ixj6GBG"


class TestBcrypt(unittest.TestCase):
    def test_matches_apache_htpasswd(self):
        self.assertTrue(bcrypt.check_password("s3cret pass", APACHE))
        self.assertFalse(bcrypt.check_password("s3cret pas", APACHE))

    def test_round_trip_and_format(self):
        h = bcrypt.hash_password("hunter2!", cost=4)
        self.assertRegex(h, r"^\$2y\$04\$[./A-Za-z0-9]{53}$")
        self.assertTrue(bcrypt.check_password("hunter2!", h))
        self.assertFalse(bcrypt.check_password("hunter2", h))
        self.assertNotEqual(h, bcrypt.hash_password("hunter2!", cost=4))  # random salt
        self.assertFalse(bcrypt.check_password("x", "not a hash"))
        with self.assertRaises(ValueError):
            bcrypt.hash_password("x", cost=3)
        self.assertTrue(bcrypt.htpasswd_line("u", "p", cost=4).startswith("u:$2y$04$"))


class RegistryCase(ExposureCase):
    """A host with a running Traefik (from the exposure tests)."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        patcher = mock.patch.object(hosted, "_verify_when_ready", mock.AsyncMock())
        patcher.start()
        self.addCleanup(patcher.stop)

    def registries(self):
        return {c.name: c for c in self.svc.store.list_credentials("registry")}


class TestDeploy(RegistryCase):
    async def test_with_login(self):
        body = {"name": "reg", "domain": "Registry.Example.com"}
        await self.roles("POST", "/hosts/local/registry", "admin", json=body)
        _, out = await self.call("POST", "/hosts/local/registry", json=body, expect=201)
        self.assertEqual(out["url"], "https://registry.example.com")
        self.assertEqual(out["address"], "registry.example.com")
        self.assertEqual(out["proxy"], "traefik")
        self.assertEqual(out["username"], "kowan")
        password = out["password"]
        self.assertGreaterEqual(len(password), 24)

        dep = self.svc.store.get_deployment(out["deploymentId"])
        self.assertIn("registry:3", dep.compose)
        self.assertIn("Host(`registry.example.com`)", dep.compose)
        self.assertIn("certresolver: letsencrypt", dep.compose)
        self.assertIn("kowan_proxy", dep.compose)
        self.assertIn("REGISTRY_HTTP_HOST: https://registry.example.com", dep.compose)
        self.assertIn("REGISTRY_AUTH: htpasswd", dep.compose)
        self.assertNotIn("ports:", dep.compose)  # only Traefik reaches it
        self.assertNotIn(password, dep.compose)

        user, _, hashed = self.engine.volume_fs["kowan-registry-reg-config"].get("/htpasswd").decode().strip().partition(":")
        self.assertEqual(user, "kowan")
        self.assertTrue(bcrypt.check_password(password, hashed))

        reg = self.registries()["reg"]
        self.assertEqual((reg.meta["url"], reg.meta["username"]), ("https://registry.example.com", "kowan"))
        self.assertEqual(self.svc.store.registry_view_by_host("registry.example.com").password, password)

        # Same name or same domain again: refused.
        await self.call("POST", "/hosts/local/registry", json=body, expect=409)
        await self.call("POST", "/hosts/local/registry", json={**body, "name": "other"}, expect=409)

    async def test_without_login_and_own_password(self):
        _, out = await self.call("POST", "/hosts/local/registry", json={
            "name": "open", "domain": "open.example.com", "auth": False}, expect=201)
        self.assertNotIn("password", out)
        dep = self.svc.store.get_deployment(out["deploymentId"])
        self.assertNotIn("REGISTRY_AUTH", dep.compose)
        self.assertNotIn("/config", dep.compose)
        self.assertNotIn("kowan-registry-open-config", self.engine.volume_fs)
        self.assertFalse(self.registries()["open"].meta["hasPassword"])

        _, out = await self.call("POST", "/hosts/local/registry", json={
            "name": "mine", "domain": "mine.example.com", "username": "ci", "password": "a-long-password"}, expect=201)
        self.assertNotIn("password", out)  # the caller chose it
        self.assertEqual(out["username"], "ci")

    async def test_validation(self):
        for body, needle in (
            ({}, "domain"),
            ({"domain": "not a domain"}, "domain"),
            ({"domain": "r.example.com", "name": "Bad Name"}, "name"),
            ({"domain": "r.example.com", "password": "short"}, "8 characters"),
            ({"domain": "r.example.com", "username": "no spaces"}, "username"),
        ):
            _, err = await self.call("POST", "/hosts/local/registry", json=body, expect=400)
            self.assertIn(needle, err["error"])
        await self.call("POST", "/hosts/nope/registry", json={"domain": "r.example.com"}, expect=404)


class TestNoProxy(ServerCase):
    async def asyncSetUp(self):
        self.cli = FakeDockerCLI()
        await super().asyncSetUp()

    async def asyncTearDown(self):
        await super().asyncTearDown()
        self.cli.close()

    async def test_needs_traefik(self):
        _, err = await self.call("POST", "/hosts/local/registry", json={"domain": "r.example.com"}, expect=409)
        self.assertIn("Traefik", err["error"])
        self.assertEqual(self.svc.store.list_credentials("registry"), [])


if __name__ == "__main__":
    unittest.main()
