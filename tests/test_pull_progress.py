"""Progress of image pulls (kowan/docker/pulls.py): every pull through a
host's CachingOps is summarised and reaches the events WebSocket."""

import asyncio
import json
import unittest

from kowan.docker import pulls
from kowan.docker.imagecache import CachingOps, ImageListCache
from kowan.docker.pulls import PullTracker


def _lines(*msgs):
    return [json.dumps(m).encode() + b"\n" for m in msgs]


class _Stream:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.closed = False

    def __aiter__(self):
        return self._it()

    async def _it(self):
        for c in self.chunks:
            yield c

    async def read(self, n=-1):
        return self.chunks.pop(0) if self.chunks else b""

    async def close(self):
        self.closed = True


PROGRESS = _lines(
    {"status": "Pulling from library/nginx", "id": "1.28"},
    {"status": "Pulling fs layer", "progressDetail": {}, "id": "aaa"},
    {"status": "Already exists", "progressDetail": {}, "id": "bbb"},
    {"status": "Downloading", "progressDetail": {"current": 50, "total": 200}, "id": "aaa"},
    {"status": "Downloading", "progressDetail": {"current": 150, "total": 200}, "id": "aaa"},
    {"status": "Pull complete", "progressDetail": {}, "id": "aaa"},
    {"status": "Status: Downloaded newer image for nginx:1.28"},
)


class TestTracker(unittest.IsolatedAsyncioTestCase):
    async def test_summarises_a_pull_and_passes_the_stream_through(self):
        t = PullTracker(interval=0)
        q = t.subscribe("h1")
        # Split mid-line, as the network does.
        raw = b"".join(PROGRESS)
        stream = t.track("h1", "nginx:1.28", _Stream([raw[:70], raw[70:]]))
        out = b"".join([c async for c in stream])
        await stream.close()
        self.assertEqual(out, raw)
        snaps = []
        while not q.empty():
            snaps.append(q.get_nowait())
        self.assertEqual(snaps[0]["status"], "pulling")
        mid = [s for s in snaps if s["current"] == 150]
        self.assertTrue(mid and mid[0]["total"] == 200)
        last = snaps[-1]
        self.assertEqual((last["status"], last["layers"], last["layersDone"]), ("done", 2, 2))
        self.assertEqual(last["message"], "Downloaded newer image for nginx:1.28")
        # Only one end is reported though the stream ended and was closed.
        self.assertEqual(sum(1 for s in snaps if s["status"] == "done"), 1)

    async def test_error_and_abandoned_pulls(self):
        t = PullTracker(interval=0)
        s = t.track("h1", "nope:1", _Stream(_lines({"error": "manifest unknown"})))
        while await s.read():
            pass
        [snap] = t.active("h1")
        self.assertEqual((snap["status"], snap["message"]), ("error", "manifest unknown"))
        s2 = t.track("h1", "big:1", _Stream(PROGRESS[:3]))
        await s2.read()
        await s2.close()
        snap = [x for x in t.active("h1") if x["ref"] == "big:1"][0]
        self.assertEqual(snap["status"], "error")
        self.assertIn("stopped", snap["message"])

    async def test_throttles_and_forgets_ended_pulls(self):
        now = [0.0]
        t = PullTracker(interval=1.0, linger=5.0, clock=lambda: now[0])
        q = t.subscribe("h1")
        s = t.track("h1", "nginx:1.28", _Stream(PROGRESS))
        async for _ in s:
            pass
        # The start and the end, nothing in between within the interval.
        self.assertEqual([q.get_nowait()["status"] for _ in range(q.qsize())], ["pulling", "done"])
        self.assertEqual(len(t.active("h1")), 1)
        self.assertEqual(t.active("h2"), [])
        now[0] = 10.0
        self.assertEqual(t.active("h1"), [])
        t.unsubscribe("h1", q)


class TestWhoSees(unittest.IsolatedAsyncioTestCase):
    async def test_a_team_member_follows_only_their_own_pulls(self):
        t = PullTracker(interval=0)
        mine, everyone = t.subscribe("h1", "u1"), t.subscribe("h1")
        for user, ref in (("u1", "mine:1"), ("u2", "theirs:1"), ("", "kowan:1")):
            async for _ in t.track("h1", ref, _Stream(PROGRESS[:1]), user):
                pass
        refs = lambda q: {q.get_nowait()["ref"] for _ in range(q.qsize())}  # noqa: E731
        self.assertEqual(refs(mine), {"mine:1"})
        self.assertEqual(refs(everyone), {"mine:1", "theirs:1", "kowan:1"})
        self.assertEqual([p["ref"] for p in t.active("h1", "u1")], ["mine:1"])

    def test_visible_to(self):
        from unittest import mock

        from kowan.auth import authorize

        self.assertIsNone(pulls.visible_to("h1"))  # outside a request
        ctx = mock.Mock(unrestricted=False, user=mock.Mock(id="u1"))
        ctx.host_rank.return_value = 0
        token = authorize.bind(ctx)
        try:
            self.assertEqual(pulls.visible_to("h1"), "u1")
            ctx.host_rank.return_value = 1
            self.assertIsNone(pulls.visible_to("h1"))
        finally:
            authorize.unbind(token)


class _Ops:
    async def pull_image(self, ref, auth):
        return _Stream(PROGRESS)


class TestCachingOps(unittest.IsolatedAsyncioTestCase):
    async def test_every_pull_of_a_host_is_tracked(self):
        t = PullTracker(interval=0)
        orig, pulls.TRACKER = pulls.TRACKER, t
        try:
            ops = CachingOps(_Ops(), "h1", ImageListCache())
            stream = await ops.pull_image("nginx:1.28", "")
            async for _ in stream:
                pass
            [snap] = t.active("h1")
            self.assertEqual((snap["ref"], snap["status"]), ("nginx:1.28", "done"))
        finally:
            pulls.TRACKER = orig


if __name__ == "__main__":
    unittest.main()


from tests.apicase import ServerCase  # noqa: E402


class TestSocketForTeams(ServerCase):
    async def test_a_team_key_opens_a_pull_socket_that_hides_other_pulls(self):
        _, t = await self.call("POST", "/teams", json={"name": "ci"}, expect=201)
        _, key = await self.call("POST", "/apikeys", json={"name": "deployer", "role": "operator",
                                                           "teamId": t["id"]}, expect=201)
        tracker = PullTracker(interval=0)
        orig, pulls.TRACKER = pulls.TRACKER, tracker
        try:
            team = await self.client.ws_connect("/api/v1/hosts/local/events/ws?type=pull",
                                                headers={"X-API-Key": key["secret"]})
            admin = await self.client.ws_connect("/api/v1/hosts/local/events/ws?type=pull", headers=self.h["admin"])
            await asyncio.sleep(0.05)
            async for _ in tracker.track("local", "someone:1", _Stream(PROGRESS[:1]), "other-user"):
                pass
            got = await admin.receive_json(timeout=5)
            self.assertEqual(got["ref"], "someone:1")
            with self.assertRaises(asyncio.TimeoutError):
                await team.receive_json(timeout=0.3)
            self.assertFalse(team.closed)
            await team.close()
            await admin.close()
        finally:
            pulls.TRACKER = orig
