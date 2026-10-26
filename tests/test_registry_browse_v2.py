"""Registry browse over the v2 API against the fake registry: Basic and
Bearer (token realm) flows, catalog paging, tags, not-found and auth errors."""

import unittest

from kowan.models import Registry
from kowan.registry import browse
from tests.fakeservices import FakeRegistry


class TestBrowse(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.reg = FakeRegistry()
        self.addr = await self.reg.start()

    async def asyncTearDown(self):
        await self.reg.stop()

    def record(self, **kw):
        base = dict(type="generic", url="http://" + self.addr, username="bob", password="pw", insecure=True)
        return Registry(**{**base, **kw})

    async def test_basic_and_bearer(self):
        for bearer in (False, True):
            self.reg.bearer = bearer
            self.assertEqual(await browse.catalog(self.record(), 50), ["app/web", "tools"])
            self.assertEqual(await browse.tags(self.record(), "app/web"), ["1.0", "1.1", "latest"])
            self.assertEqual(await browse.v2_repositories("bob", "pw", "http://" + self.addr, True), ["app/web", "tools"])
            self.assertEqual(await browse.v2_tags("bob", "pw", "http://" + self.addr, "tools", True), ["v2"])

    async def test_errors(self):
        with self.assertRaises(ValueError) as cm:
            await browse.tags(self.record(), "nope")
        self.assertIn("not found", str(cm.exception))
        with self.assertRaises(ValueError):
            await browse.tags(self.record(), " ")
        with self.assertRaises(ValueError) as cm:
            await browse.catalog(self.record(password="wrong"), 0)
        self.assertIn("authentication required", str(cm.exception))
        self.reg.bearer = True
        with self.assertRaises(ValueError) as cm:
            await browse.catalog(self.record(password="wrong"), 0)
        self.assertIn("token status 401", str(cm.exception))
        self.assertEqual(browse._parse_challenge('Bearer realm="r",service="s"'), {"realm": "r", "service": "s"})


if __name__ == "__main__":
    unittest.main()
