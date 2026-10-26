"""Platform features on/off: who may read and change them, the 404 on the
paths of a feature that is off (its public webhook included), and GitOps
pausing its sync."""

import asyncio
import unittest
from unittest import mock

from kowan import features
from kowan.gitops import service as gitops_service
from tests.apicase import ServerCase


class TestFeatures(ServerCase):
    async def test_state_and_roles(self):
        _, state = await self.call("GET", "/features", "viewer", expect=200)
        self.assertEqual(state, {"templates": True, "networkMap": True, "gitops": True})
        await self.roles("PUT", "/features", "admin", json={"templates": False})
        _, state = await self.call("PUT", "/features", json={"templates": False}, expect=200)
        self.assertFalse(state["templates"])
        self.assertTrue(state["gitops"])
        for body, msg in (({"nope": False}, "unknown feature"), ({"gitops": "no"}, "true or false"),
                          ({}, "send"), ([1], "send")):
            _, err = await self.call("PUT", "/features", json=body, expect=400)
            self.assertIn(msg, err["error"])
        # A broken stored value counts as everything on.
        self.svc.store.set_setting("features", "not json")
        self.assertTrue(all(features.get(self.svc.store).values()))

    async def test_a_feature_that_is_off_answers_404(self):
        await self.call("GET", "/templates", "viewer", expect=200)
        await self.call("PUT", "/features", json={"templates": False, "networkMap": False, "gitops": False}, expect=200)
        for path in ("/templates", "/templates/x", "/netmap", "/hosts/local/netmap", "/gitops/syncs"):
            status, err = await self.call("GET", path)
            self.assertEqual(status, 404, path)
            self.assertIn("turned off in Settings", err["error"])
        r = await self.client.post("/hooks/gitops/some-token", data=b"{}")
        self.assertEqual((r.status, (await r.json())["error"]), (404, "GitOps is turned off in Settings"))
        # Paths that only look alike stay open.
        await self.call("GET", "/networks", "viewer", params={}, expect=None)
        self.assertEqual(features.owner("/api/v1/templatesx"), "")
        await self.call("PUT", "/features", json={"templates": True}, expect=200)
        await self.call("GET", "/templates", "viewer", expect=200)

    async def test_gitops_does_not_sync_while_off(self):
        gs = self.svc.gitops
        gs.tick = mock.AsyncMock()
        features.update(self.svc.store, {"gitops": False})
        with mock.patch.object(gitops_service, "_LOOP_TICK", 0):
            task = asyncio.ensure_future(gs.run_loop())
            await asyncio.sleep(0.05)
            self.assertEqual(gs.tick.await_count, 0)
            features.update(self.svc.store, {"gitops": True})
            for _ in range(50):
                if gs.tick.await_count:
                    break
                await asyncio.sleep(0.01)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.assertGreater(gs.tick.await_count, 0)


if __name__ == "__main__":
    unittest.main()
