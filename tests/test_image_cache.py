"""Per-host image list cache (kowan/docker/imagecache.py).

The Provider hands out :class:`CachingOps` proxies: ``list_images`` is served
from a short-TTL cache, and every image-mutating op (pull/build/remove/prune,
compose/stack deploys) invalidates it, so a kowan-initiated change is never
followed by a stale list.
"""

import asyncio
import unittest

from kowan.docker.imagecache import CachingOps, ImageListCache
from kowan.docker.provider import Provider


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class _Stream:
    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.closed = False

    async def read(self, n=-1):
        return self._chunks.pop(0) if self._chunks else b""

    async def __aiter__(self):
        while self._chunks:
            yield self._chunks.pop(0)

    async def close(self):
        self.closed = True


class _FakeOps:
    def __init__(self):
        self.images = ["a"]
        self.list_calls = 0
        self.gate = None  # optional asyncio.Event to hold list_images open

    async def list_images(self):
        self.list_calls += 1
        snapshot = list(self.images)
        if self.gate is not None:
            await self.gate.wait()
        return snapshot

    async def remove_image(self, id, force):
        self.images.remove(id)

    async def prune_images(self, all):
        n = len(self.images)
        self.images = []
        return n

    async def build_image(self, opts):
        self.images.append(opts)
        return "built"

    async def compose_up(self, project, yaml):
        self.images.append("compose-" + project)
        return "ok"

    async def pull_image(self, ref, auth):
        ops = self

        class _Pull(_Stream):
            async def __aiter__(self):
                async for c in _Stream.__aiter__(self):
                    yield c
                ops.images.append(ref)  # image exists once the pull finished

        return _Pull([b'{"status":"Pulling"}\n', b'{"status":"Done"}\n'])

    async def ping(self):
        return "pong"

    marker = "plain-attr"


def _run(coro):
    return asyncio.run(coro)


class TestImageListCache(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        self.cache = ImageListCache(ttl=30, clock=self.clock)
        self.inner = _FakeOps()
        self.ops = CachingOps(self.inner, "h1", self.cache)

    def test_hit_within_ttl_and_refetch_after(self):
        async def go():
            self.assertEqual(await self.ops.list_images(), ["a"])
            self.inner.images.append("b")  # external change, no event
            self.clock.t = 29
            self.assertEqual(await self.ops.list_images(), ["a"])  # cached
            self.clock.t = 31
            self.assertEqual(await self.ops.list_images(), ["a", "b"])

        _run(go())
        self.assertEqual(self.inner.list_calls, 2)

    def test_mutations_invalidate(self):
        async def go():
            await self.ops.list_images()
            await self.ops.remove_image("a", False)
            self.assertEqual(await self.ops.list_images(), [])
            await self.ops.build_image("img:1")
            self.assertEqual(await self.ops.list_images(), ["img:1"])
            await self.ops.compose_up("p", "")
            self.assertEqual(await self.ops.list_images(), ["img:1", "compose-p"])
            self.assertEqual(await self.ops.prune_images(True), 2)
            self.assertEqual(await self.ops.list_images(), [])

        _run(go())

    def test_pull_invalidates_when_stream_finishes(self):
        async def go():
            await self.ops.list_images()
            stream = await self.ops.pull_image("nginx", "")
            # A list taken mid-pull doesn't see the image yet...
            self.assertEqual(await self.ops.list_images(), ["a"])
            async for _ in stream:
                pass
            # ...and is not served from cache once the pull completed.
            self.assertEqual(await self.ops.list_images(), ["a", "nginx"])
            await stream.close()

        _run(go())

    def test_fetch_racing_an_invalidation_is_not_cached(self):
        async def go():
            self.inner.gate = asyncio.Event()
            t = asyncio.ensure_future(self.ops.list_images())
            await asyncio.sleep(0)  # fetch started with the old snapshot
            self.inner.images = []
            self.ops.invalidate_image_cache()  # e.g. a docker image event
            self.inner.gate.set()
            self.assertEqual(await t, ["a"])  # that caller gets what it asked for
            self.inner.gate = None
            self.assertEqual(await self.ops.list_images(), [])  # but it wasn't stored

        _run(go())

    def test_concurrent_misses_share_one_fetch(self):
        async def go():
            self.inner.gate = asyncio.Event()
            tasks = [asyncio.ensure_future(self.ops.list_images()) for _ in range(5)]
            await asyncio.sleep(0)
            self.inner.gate.set()
            return await asyncio.gather(*tasks)

        results = _run(go())
        self.assertEqual(results, [["a"]] * 5)
        self.assertEqual(self.inner.list_calls, 1)

    def test_fetch_error_is_not_cached(self):
        calls = {"n": 0}

        async def boom():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("daemon down")
            return ["x"]

        async def go():
            with self.assertRaises(RuntimeError):
                await self.cache.get("h", boom)
            self.assertEqual(await self.cache.get("h", boom), ["x"])

        _run(go())

    def test_returned_list_is_a_copy(self):
        async def go():
            first = await self.ops.list_images()
            first.append("mutated")
            self.assertEqual(await self.ops.list_images(), ["a"])

        _run(go())

    def test_delegates_everything_else(self):
        self.assertEqual(self.ops.marker, "plain-attr")
        self.assertEqual(_run(self.ops.ping()), "pong")
        self.assertIs(self.ops.inner, self.inner)

    def test_hosts_are_independent(self):
        other = CachingOps(_FakeOps(), "h2", self.cache)

        async def go():
            await self.ops.list_images()
            await other.list_images()
            await other.remove_image("a", False)  # invalidates h2 only
            self.assertIsNotNone(self.cache.peek("h1"))
            self.assertIsNone(self.cache.peek("h2"))

        _run(go())


class _Host:
    def __init__(self, id, type="local"):
        self.id = id
        self.type = type
        self.token = ""


class TestProviderWrapsOps(unittest.TestCase):
    def test_local_ops_is_cached_proxy_with_shared_cache(self):
        local = _FakeOps()
        p = Provider(local=local, local_host_id="local")
        ops = p.ops(_Host("local"))
        self.assertIsInstance(ops, CachingOps)
        self.assertIs(ops, p.ops(_Host("local")))  # stable identity
        self.assertIs(ops.inner, local)
        self.assertIs(p._local, local)  # shutdown still closes the raw client

        async def go():
            await ops.list_images()
            self.assertIsNotNone(p.image_cache.peek("local"))
            ops.invalidate_image_cache()
            self.assertIsNone(p.image_cache.peek("local"))

        _run(go())


class TestEventsInvalidate(unittest.TestCase):
    """A live /events/ws stream drops the host's cached list on image events."""

    def test_image_event_invalidates(self):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from kowan.api.events import events_ws
        from kowan.web import SERVICES_KEY

        lines = [
            b'{"Type":"container","Action":"start","Actor":{"ID":"c1"},"time":1}\n',
            b'{"Type":"image","Action":"pull","Actor":{"ID":"nginx"},"time":2}\n',
        ]

        class _EvOps(_FakeOps):
            async def events(self, since, until, filters):
                return _Stream(list(lines))

        local = _EvOps()
        provider = Provider(local=local, local_host_id="local")

        class _Store:
            def get_host(self, host_id):
                return _Host(host_id)

        class _Svc:
            store = _Store()
            docker = provider

        async def go():
            ops = provider.ops(_Host("local"))
            await ops.list_images()
            self.assertIsNotNone(provider.image_cache.peek("local"))
            app = web.Application()
            app[SERVICES_KEY] = _Svc()
            app.router.add_get("/hosts/{hostID}/events/ws", events_ws)
            async with TestClient(TestServer(app)) as client:
                ws = await client.ws_connect("/hosts/local/events/ws")
                got = [m.data async for m in ws if m.type.name == "TEXT"]
            self.assertEqual(len(got), 2)
            self.assertIsNone(provider.image_cache.peek("local"))

        _run(go())


if __name__ == "__main__":
    unittest.main()
