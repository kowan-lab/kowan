"""Per-id locks and caches drop entries nobody uses any more."""

import asyncio
import unittest
from unittest import mock

from kowan.gitops.service import GitOpsService
from kowan.updater import orchestrator
from kowan.updater.orchestrator import UpdateManager
from kowan.volbackup.service import VolumeBackupService


class TestIdleLocksDropped(unittest.IsolatedAsyncioTestCase):
    async def _check(self, svc):
        held = svc._lock("busy")
        await held.acquire()
        for i in range(50):
            async with svc._lock(f"done-{i}"):
                pass
        # The finished ids are gone; the one still held is kept and reused.
        self.assertEqual(set(svc._locks), {"busy", "done-49"})
        self.assertIs(svc._lock("busy"), held)
        held.release()

    async def test_gitops(self):
        await self._check(GitOpsService(mock.Mock()))

    async def test_volbackup(self):
        await self._check(VolumeBackupService(mock.Mock()))


class TestLatestDigestCache(unittest.IsolatedAsyncioTestCase):
    async def test_expired_refs_dropped(self):
        mgr = UpdateManager(mock.Mock())
        now = [1000.0]

        async def remote(store, ref):
            return "sha256:" + ref

        with mock.patch("kowan.agentupdate._remote_digest", remote), \
                mock.patch.object(orchestrator.time, "monotonic", lambda: now[0]):
            for i in range(20):
                await mgr._latest_digest(f"img{i}")
            now[0] += orchestrator.LATEST_CACHE_TTL + 1
            self.assertEqual(await mgr._latest_digest("fresh"), ("sha256:fresh", ""))
        self.assertEqual(set(mgr._latest), {"fresh"})


if __name__ == "__main__":
    unittest.main()
