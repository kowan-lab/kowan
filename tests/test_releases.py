"""Release check: versions from registry tags, the cache and Check's refresh,
one bell notice per new release, and the versions in the update status (a
server run from source included)."""

import asyncio
import os
import tempfile
import unittest
from unittest import mock

from kowan import __version__, releases
from kowan.releases import Releases, is_newer, newest, parse
from kowan.store import Store
from tests.apicase import ServerCase


class FakeSvc:
    def __init__(self):
        self.store = Store(os.path.join(tempfile.mkdtemp(), "t.db"))
        self.published = []
        self.bus = self

    def publish(self, event):
        self.published.append(event)


def fetcher(tags):
    calls = []

    async def fetch(repo):
        calls.append(repo)
        if isinstance(tags[0], Exception):
            raise tags[0]
        return list(tags[0])

    return fetch, calls


class TestVersions(unittest.TestCase):
    def test_parse_and_newest(self):
        self.assertEqual(parse("0.1.1"), (0, 1, 1))
        self.assertEqual(parse("v1.2.3"), (1, 2, 3))
        for bad in ("latest", "0.1", "1.0.0-rc1", "dev", "", None):
            self.assertIsNone(parse(bad))
        self.assertEqual(newest(["0.1.0", "0.1", "latest", "0.1.1", "0.10.0", "0.9.9"]), "0.10.0")
        self.assertEqual(newest(["latest", "0.1"]), "")
        self.assertTrue(is_newer("0.1.1", "0.1.0"))
        self.assertTrue(is_newer("0.2.0", "dev"))
        self.assertFalse(is_newer("0.1.0", "0.1.0"))
        self.assertFalse(is_newer("", "0.1.0"))


class TestReleases(unittest.IsolatedAsyncioTestCase):
    async def test_latest_caches_and_refresh_skips_the_cache(self):
        tags = [["0.1.0", "0.1.1", "latest"]]
        fetch, calls = fetcher(tags)
        r = Releases(FakeSvc(), "ghcr.io/kowan-lab/kowan", fetch)
        self.assertEqual(await r.latest(), ("0.1.1", ""))
        tags[0] = ["0.1.1", "0.2.0"]
        self.assertEqual(await r.latest(), ("0.1.1", ""))  # cached
        self.assertEqual(await r.latest(refresh=True), ("0.2.0", ""))
        self.assertEqual(calls, ["ghcr.io/kowan-lab/kowan"] * 2)

    async def test_errors_and_off(self):
        tags = [RuntimeError("offline")]
        fetch, _ = fetcher(tags)
        r = Releases(FakeSvc(), "ghcr.io/x/y", fetch)
        version, err = await r.latest()
        self.assertEqual(version, "")
        self.assertIn("offline", err)
        tags[0] = ["latest"]
        self.assertIn("no release versions", (await r.latest(refresh=True))[1])
        self.assertFalse(Releases(FakeSvc(), "").enabled)
        self.assertFalse(Releases(FakeSvc(), "off").enabled)
        self.assertEqual(await Releases(FakeSvc(), "").latest(), ("", ""))
        self.assertEqual(await Releases(FakeSvc(), "off").status(), {
            "currentVersion": __version__, "latestVersion": "", "newVersion": False, "releaseError": ""})

    async def test_a_new_release_is_announced_once(self):
        svc = FakeSvc()
        tags = [[__version__]]
        fetch, _ = fetcher(tags)
        r = Releases(svc, "ghcr.io/kowan-lab/kowan", fetch)
        self.assertEqual(await r.check(), "")
        self.assertEqual(svc.published, [])
        tags[0] = [__version__, "99.0.0"]
        self.assertEqual(await r.check(), "99.0.0")
        self.assertEqual(await r.check(), "99.0.0")
        self.assertEqual(len(svc.published), 1)
        e = svc.published[0]
        self.assertEqual((e.type, e.payload["title"], e.payload["refId"]), ("notice", "Kowan 99.0.0 is available", "99.0.0"))
        self.assertIn(f"runs {__version__}", e.payload["body"])
        tags[0] = ["99.0.1"]
        await r.check()
        self.assertEqual([p.payload["refId"] for p in svc.published], ["99.0.0", "99.0.1"])

    async def test_the_loop_checks_after_a_delay_and_stops(self):
        fetch, calls = fetcher([["0.0.1"]])
        r = Releases(FakeSvc(), "ghcr.io/kowan-lab/kowan", fetch)
        with mock.patch.object(releases, "FIRST_CHECK_AFTER", 0), mock.patch.object(releases, "CHECK_EVERY", 3600):
            r.start()
            r.start()  # idempotent
            for _ in range(50):
                if calls:
                    break
                await asyncio.sleep(0.01)
            await r.stop()
        self.assertEqual(len(calls), 1)
        Releases(FakeSvc(), "").start()  # off: no task

    async def test_registry_tags_through_the_v2_api(self):
        seen = []

        async def v2_tags(username, password, host, repo, insecure):
            seen.append((host, repo))
            return ["1.0.0"]

        r = Releases(FakeSvc(), "ghcr.io/kowan-lab/kowan")
        with mock.patch("kowan.registry.browse.v2_tags", v2_tags):
            self.assertEqual(await r.latest(), ("1.0.0", ""))
            await Releases(FakeSvc(), "kowan").latest()
            await Releases(FakeSvc(), "docker.io/acme/kowan").latest()
        self.assertEqual(seen, [("ghcr.io", "kowan-lab/kowan"), ("registry-1.docker.io", "library/kowan"),
                                ("registry-1.docker.io", "acme/kowan")])


class TestReleasesAPI(ServerCase):
    async def test_status_carries_versions_and_the_notice_reaches_the_bell(self):
        tags = [[__version__, "99.0.0"]]
        fetch, calls = fetcher(tags)
        self.svc.attach("releases", Releases(self.svc, "ghcr.io/kowan-lab/kowan", fetch))
        # Run from source: no self-update, but the versions are known.
        _, st = await self.call("GET", "/system/server/update", expect=200)
        self.assertFalse(st["supported"])
        self.assertEqual((st["currentVersion"], st["latestVersion"], st["newVersion"]), (__version__, "99.0.0", True))
        tags[0] = ["99.0.1"]
        _, st = await self.call("GET", "/system/server/update", expect=200)
        self.assertEqual(st["latestVersion"], "99.0.0")  # cached
        _, st = await self.call("GET", "/system/server/update", params={"refresh": "1"}, expect=200)
        self.assertEqual(st["latestVersion"], "99.0.1")
        self.assertEqual(len(calls), 2)

        await self.svc.releases.check()
        for _ in range(100):
            _, feed = await self.call("GET", "/notifications", "viewer", expect=200)
            if feed:
                break
            await asyncio.sleep(0.02)
        self.assertEqual(feed[0]["title"], "Kowan 99.0.1 is available")


if __name__ == "__main__":
    unittest.main()
