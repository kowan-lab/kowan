"""The control plane's own health page."""

from tests.apicase import ServerCase


class TestHealth(ServerCase):
    async def test_health(self):
        await self.call("GET", "/system/health", "operator", expect=403)
        _, h = await self.call("GET", "/system/health", "admin", expect=200)
        self.assertIn(h["status"], ("ok", "degraded"))
        self.assertTrue(h["server"]["version"])
        [local] = [x for x in h["hosts"] if x["id"] == "local"]
        self.assertTrue(local["online"])
        self.assertIsInstance(local["pingMs"], float)
        names = {f["name"] for f in h["storage"]["files"]}
        self.assertEqual(names, {"kowan.db", "logs.db"})
        self.assertGreater(next(f for f in h["storage"]["files"] if f["name"] == "kowan.db")["bytes"], 0)
        self.assertEqual({lp["name"] for lp in h["loops"]} >= {"Monitor", "Auto-update"}, True)
        self.assertEqual(set(h["work"]), {"runbooksRunning", "pulls", "websockets", "crashSnapshotsTaken"})
