"""Regression tests for the performance / hardening pass.

Covers: concurrent monitor sampling, the Docker Engine path guard and volume
name validation, mux-stream cleanup on agent errors, SQLite pragmas/indexes and
the lock-free snapshot, api-key last_used throttling, and HTTP compression +
SPA cache headers. All offline (no Docker daemon needed).
"""

from __future__ import annotations

import asyncio
import gzip
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kowan.docker.local import LocalClient, _check_engine_path
from kowan.errors import Validation
from kowan.models import APIKey, Container, ContainerStats, Host, SystemInfo
from kowan.store import Store
from kowan.store.apikeys import hash_api_key_secret


# ---------------------------------------------------------------------------
# Monitor: per-container stats and per-host processing run concurrently.
# ---------------------------------------------------------------------------


class _SlowOps:
    """Fake Ops whose stats call takes ``delay`` seconds (like the Engine's
    one-shot stats, which waits for a second CPU sample)."""

    def __init__(self, n: int, delay: float) -> None:
        self.n = n
        self.delay = delay
        self.inflight = 0
        self.max_inflight = 0

    async def ping(self) -> None:
        await asyncio.sleep(self.delay)

    async def list_containers(self, all: bool):
        return [Container(id=f"c{i}", names=[f"/c{i}"], state="running") for i in range(self.n)]

    async def container_stats(self, cid: str) -> ContainerStats:
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.inflight -= 1
        return ContainerStats(cpuPercent=1.0, memUsage=10)

    async def system_info(self) -> SystemInfo:
        return SystemInfo(images=3, memTotal=100)


class _FakeStore:
    def __init__(self, hosts):
        self.hosts = hosts
        self.metrics = []

    def list_hosts(self):
        return self.hosts

    def list_alert_rules(self):
        return []

    def set_host_last_seen(self, *_a):
        pass

    def create_metric(self, m):
        self.metrics.append(m)

    def prune_metrics(self, _before):
        return 0


class TestMonitorConcurrency(unittest.IsolatedAsyncioTestCase):
    async def test_tick_samples_containers_and_hosts_concurrently(self):
        from kowan.monitor import monitor as mon

        hosts = [Host(id="h1", name="h1", type="agent"), Host(id="h2", name="h2", type="agent")]
        ops = {h.id: _SlowOps(n=16, delay=0.1) for h in hosts}
        store = _FakeStore(hosts)
        svc = SimpleNamespace(
            store=store,
            docker=SimpleNamespace(ops=lambda h: ops[h.id]),
            bus=SimpleNamespace(publish=lambda e: None),
            config=None,
        )
        m = mon.Monitor(svc)
        t0 = time.perf_counter()
        await m._tick()
        elapsed = time.perf_counter() - t0
        # Sequential: 2 hosts x (ping + 16 x 0.1s) = ~3.4s. Concurrent with the
        # per-host bound of 8: ping + 2 waves = ~0.3s.
        self.assertLess(elapsed, 1.0)
        self.assertEqual(len(store.metrics), 2)
        for sample in store.metrics:
            self.assertEqual(sample.running, 16)
            self.assertAlmostEqual(sample.cpu_percent, 16.0)
        for o in ops.values():
            self.assertLessEqual(o.max_inflight, mon._SAMPLE_CONCURRENCY)
            self.assertGreater(o.max_inflight, 1)


class _ChurnOps:
    """Fake Ops whose container list the test mutates between ticks."""

    def __init__(self):
        self.containers = []

    async def ping(self):
        return None

    async def list_containers(self, all: bool):
        return list(self.containers)

    async def container_stats(self, cid):
        return ContainerStats()

    async def system_info(self):
        return SystemInfo()


class _AlertStore(_FakeStore):
    def __init__(self, hosts, rules):
        super().__init__(hosts)
        self.rules = rules
        self.alerts = []

    def list_alert_rules(self):
        return self.rules

    def create_alert(self, a):
        self.alerts.append(a)

    def resolve_alert(self, _id):
        pass

    def create_notification(self, _n):
        pass


class TestMonitorForgetsRemovedContainers(unittest.IsolatedAsyncioTestCase):
    async def test_recreate_does_not_alert_but_crash_does(self):
        from kowan.models import AlertRule
        from kowan.monitor import monitor as mon

        host = Host(id="h1", name="h1", type="agent")
        rule = AlertRule(id="r1", name="down", type="container_down", enabled=True, autoResolve=True)
        ops = _ChurnOps()
        store = _AlertStore([host], [rule])
        svc = SimpleNamespace(
            store=store,
            docker=SimpleNamespace(ops=lambda h: ops),
            bus=SimpleNamespace(publish=lambda e: None),
            config=None,
        )
        m = mon.Monitor(svc)

        ops.containers = [Container(id="old", names=["/web"], state="running")]
        await m._tick()
        # Redeploy: the old container is removed and a new id replaces it.
        ops.containers = [Container(id="new", names=["/web"], state="running")]
        await m._tick()
        self.assertEqual(store.alerts, [])
        self.assertNotIn("old", m._seen_running["h1"])
        self.assertFalse(any(k.endswith("|old") for k in m._cond))

        # A crash leaves the container listed but not running: still alerts.
        ops.containers = [Container(id="new", names=["/web"], state="exited", status="Exited (1)")]
        await m._tick()
        self.assertEqual(len(store.alerts), 1)


# ---------------------------------------------------------------------------
# Docker Engine path guard + volume helper bind validation.
# ---------------------------------------------------------------------------


class TestEnginePathGuard(unittest.IsolatedAsyncioTestCase):
    def test_rejects_dot_segments_and_query(self):
        for bad in (
            "/volumes/../containers/x/json",
            "/volumes/../../swarm/unlockkey",
            "/volumes/x/../y",
            "/volumes/./x",
            "/containers/x/archive?path=/etc/shadow",
            "/volumes/a#b",
        ):
            with self.assertRaises(Validation, msg=bad):
                _check_engine_path(bad)

    def test_allows_normal_paths(self):
        for ok in (
            "/containers/json",
            "/containers/abc123/stats",
            "/images/library/nginx:1.27/json",
            "/images/nginx@sha256:abcd/json",
            "/volumes/my_vol.data-1",
            "/_ping",
        ):
            _check_engine_path(ok)  # must not raise

    async def test_volume_helper_reuses_image(self):
        from kowan.errors import NotFound

        c = LocalClient(socket_path="/nonexistent/docker.sock")
        calls = []
        gone = set()

        async def fake(method, path, *, params=None, json_body=None):
            calls.append(path)
            if path == "/images/json":
                return [{"Id": "sha256:1", "RepoTags": ["alpine:3"]}]
            if path == "/containers/create":
                if json_body["Image"] in gone:
                    raise NotFound("No such image")
                return {"Id": "cid"}
            raise AssertionError(path)

        c._request_json = fake
        await c._create_volume_helper("vol1", read_only=True)
        await c._create_volume_helper("vol1", read_only=True)
        self.assertEqual(calls.count("/images/json"), 1)
        gone.add("alpine:3")  # cached image removed meanwhile -> re-pick once
        with self.assertRaises(NotFound):
            await c._create_volume_helper("vol1", read_only=True)
        self.assertEqual(calls.count("/images/json"), 2)

    async def test_volume_helper_rejects_host_paths(self):
        c = LocalClient(socket_path="/nonexistent/docker.sock")
        for bad in ("/", "/etc", "../x", "", "a/b"):
            with self.assertRaises(Validation, msg=bad):
                await c._create_volume_helper(bad, read_only=True)


# ---------------------------------------------------------------------------
# RemoteClient: a failed streaming op resets its mux stream.
# ---------------------------------------------------------------------------


class _FakeStream:
    def __init__(self):
        self.reset_called = False

    async def reset(self):
        self.reset_called = True


class TestRemoteStreamCleanup(unittest.IsolatedAsyncioTestCase):
    async def _check(self, invoke):
        from kowan import agentproto
        from kowan.docker.remote import RemoteClient

        stream = _FakeStream()

        async def dial():
            return stream

        async def failing_call(*_a, **_k):
            raise agentproto.ProtocolError("no such container")

        rc = RemoteClient(dial)
        with mock.patch.object(agentproto, "call", failing_call):
            with self.assertRaises(agentproto.ProtocolError):
                await invoke(rc)
        self.assertTrue(stream.reset_called)

    async def test_stream_op_error_resets(self):
        await self._check(lambda rc: rc.container_logs("x", True, 10))

    async def test_exec_attach_error_resets(self):
        await self._check(lambda rc: rc.exec_attach("x", ["sh"], True))


class _IdleLogStream:
    """A follow log stream for a quiet container: never yields a byte."""

    def __init__(self):
        self.closed = asyncio.Event()

    async def read(self, n: int = -1) -> bytes:
        await asyncio.Event().wait()  # blocks until cancelled
        return b""

    async def close(self):
        self.closed.set()


class TestAgentStopsIdleFollow(unittest.IsolatedAsyncioTestCase):
    async def test_closing_viewer_closes_idle_engine_stream(self):
        from kowan.agent_ops_server import serve_agent_ops
        from kowan.docker.remote import make_remote_client
        from kowan.mux.session import Session
        from kowan.transport.memory import memory_pair

        src = _IdleLogStream()

        class Ops:
            async def container_logs(self, id, follow, tail):
                return src

        ta, tb = memory_pair()
        cp = Session(ta, is_client=True).start()
        agent = Session(tb, is_client=False).start()
        serve = asyncio.ensure_future(serve_agent_ops(agent, Ops()))
        try:
            remote = make_remote_client(lambda: cp.open_stream())
            body = await remote.container_logs("c1", True, 10)
            await body.close()  # the browser closed the log viewer
            # Before: the agent only noticed on its next write, i.e. never for
            # a quiet container, leaking the Engine request.
            await asyncio.wait_for(src.closed.wait(), timeout=2)
        finally:
            serve.cancel()
            await cp.close()
            await agent.close()


# ---------------------------------------------------------------------------
# SQLite: pragmas, indexes, lock-free snapshot, api-key stamp throttle.
# ---------------------------------------------------------------------------


class TestStoreTuning(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.dir, "kowan.db"))

    def tearDown(self):
        self.store.close()

    def test_pragmas_and_indexes(self):
        self.assertEqual(self.store._one("PRAGMA synchronous")[0], 1)  # NORMAL
        names = {r["name"] for r in self.store._query("SELECT name FROM sqlite_master WHERE type='index'")}
        for idx in ("idx_metrics_time", "idx_alerts_created", "idx_workflow_runs_wf"):
            self.assertIn(idx, names)
        plan = " ".join(
            str(tuple(r)) for r in self.store._query("EXPLAIN QUERY PLAN DELETE FROM metrics WHERE time < 'x'")
        )
        self.assertIn("idx_metrics_time", plan)

    def test_snapshot_does_not_need_store_lock(self):
        dest = os.path.join(self.dir, "snap.db")
        done = threading.Event()
        errors = []

        def run():
            try:
                self.store.snapshot(dest)
            except Exception as e:  # noqa: BLE001
                errors.append(e)
            done.set()

        # Hold the store lock (as an in-flight store call on the loop would);
        # the snapshot must still complete on its own connection.
        with self.store._lock:
            t = threading.Thread(target=run)
            t.start()
            self.assertTrue(done.wait(10), "snapshot blocked on the store lock")
        t.join()
        self.assertEqual(errors, [])
        self.assertTrue(os.path.getsize(dest) > 0)

    def test_api_key_last_used_throttled(self):
        secret = "pk_" + "a" * 64
        self.store.create_api_key(
            APIKey(id="k1", name="ci", prefix=secret[:11], role="viewer", createdAt="2026-01-01T00:00:00Z"),
            hash_api_key_secret(secret),
        )
        key, ok = self.store.verify_api_key(secret)
        self.assertTrue(ok)
        self.assertNotEqual(key.last_used, "")
        with mock.patch.object(self.store, "_execute", wraps=self.store._execute) as ex:
            key2, ok2 = self.store.verify_api_key(secret)
        self.assertTrue(ok2)
        self.assertEqual(ex.call_count, 0)  # same minute: no write
        self.assertEqual(key2.last_used, key.last_used)


# ---------------------------------------------------------------------------
# HTTP: gzip for JSON, cache headers + gzip for SPA assets.
# ---------------------------------------------------------------------------


class TestHTTPWeight(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from kowan.config import ServerConfig
        from kowan.web import Services, add_spa, create_app, json_response

        self.dist = Path(tempfile.mkdtemp())
        (self.dist / "assets").mkdir()
        (self.dist / "index.html").write_text("<html>" + "x" * 4000 + "</html>")
        (self.dist / "assets" / "app-abc123.js").write_text("console.log(1);" * 500)
        (self.dist / "sw.js").write_text("self.addEventListener('fetch', () => {});")
        (self.dist / "manifest.webmanifest").write_text('{"name": "Kowan"}')
        (self.dist / "icon-192.png").write_bytes(b"\x89PNG")

        async def big(_r):
            return json_response([{"id": i, "name": "container-%d" % i} for i in range(200)])

        async def small(_r):
            return json_response({"ok": True})

        app = create_app(Services(config=ServerConfig()))
        app.router.add_get("/big", big)
        app.router.add_get("/small", small)
        add_spa(app, self.dist)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_large_json_is_gzipped(self):
        r = await self.client.get("/big", headers={"Accept-Encoding": "gzip"}, auto_decompress=False)
        self.assertEqual(r.headers.get("Content-Encoding"), "gzip")
        self.assertIn("Accept-Encoding", r.headers.get("Vary", ""))
        raw = await r.read()
        self.assertIn(b"container-199", gzip.decompress(raw))

    async def test_small_json_and_no_accept_are_plain(self):
        r = await self.client.get("/small", headers={"Accept-Encoding": "gzip"}, auto_decompress=False)
        self.assertIsNone(r.headers.get("Content-Encoding"))
        r = await self.client.get("/big", headers={"Accept-Encoding": "identity"}, auto_decompress=False)
        self.assertIsNone(r.headers.get("Content-Encoding"))
        self.assertEqual(len(await r.json()), 200)

    async def test_spa_cache_headers_and_gzip(self):
        r = await self.client.get("/assets/app-abc123.js", headers={"Accept-Encoding": "gzip"}, auto_decompress=False)
        self.assertEqual(r.status, 200)
        self.assertIn("immutable", r.headers.get("Cache-Control", ""))
        self.assertEqual(r.headers.get("Content-Encoding"), "gzip")
        self.assertEqual(gzip.decompress(await r.read()), (self.dist / "assets" / "app-abc123.js").read_bytes())

        for path in ("/", "/containers/some/client/route"):
            r = await self.client.get(path)
            self.assertEqual(r.status, 200)
            self.assertEqual(r.headers.get("Cache-Control"), "no-cache")
            self.assertTrue((await r.text()).startswith("<html>"))

    async def test_pwa_files(self):
        # The worker and the manifest change with a build: always revalidated.
        r = await self.client.get("/sw.js")
        self.assertEqual((r.status, r.headers.get("Cache-Control")), (200, "no-cache"))
        self.assertIn("javascript", r.headers.get("Content-Type", ""))
        r = await self.client.get("/manifest.webmanifest")
        self.assertEqual(r.headers.get("Cache-Control"), "no-cache")
        self.assertTrue(r.headers.get("Content-Type", "").startswith("application/manifest+json"))
        self.assertEqual((await r.json(content_type=None))["name"], "Kowan")
        r = await self.client.get("/icon-192.png")
        self.assertEqual((r.status, r.headers.get("Cache-Control")), (200, None))


class TestImagesUnknownHostStatus(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_host_renders_404_not_minus_one(self):
        from aiohttp.test_utils import make_mocked_request

        from kowan.api import images
        from kowan.config import ServerConfig
        from kowan.web import SERVICES_KEY, Services

        class _Store:
            def get_host(self, _id):
                raise KeyError("nope")

        app = web.Application()
        app[SERVICES_KEY] = Services(config=ServerConfig(), store=_Store())
        req = make_mocked_request("POST", "/api/v1/hosts/x/images/pull", app=app, match_info={"hostID": "x"})
        with self.assertRaises(web.HTTPException) as cm:
            await images._resolve_ops(req)
        self.assertEqual(cm.exception.status, 404)
        self.assertIn("host not found", cm.exception.text)


if __name__ == "__main__":
    unittest.main()
