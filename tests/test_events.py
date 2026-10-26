"""Docker events: normalisation of Engine event JSON, and the events op over
the in-memory tunnel (RemoteClient -> mux -> agent serve_ops -> fake Ops)."""

import asyncio
import json
import unittest

from kowan.agent_ops_server import serve_agent_ops
from kowan.api.events import normalize_event, parse_event_line
from kowan.docker.remote import make_remote_client
from kowan.mux.session import Session
from kowan.transport.memory import memory_pair


DIE = {
    "status": "die",
    "id": "abc123def456",
    "from": "nginx:1.27",
    "Type": "container",
    "Action": "die",
    "Actor": {
        "ID": "abc123def456",
        "Attributes": {
            "exitCode": "137",
            "image": "nginx:1.27",
            "name": "web",
            "com.docker.compose.project": "shop",
        },
    },
    "scope": "local",
    "time": 1761400000,
    "timeNano": 1761400000123456789,
}


class TestNormalize(unittest.TestCase):
    def test_container_die(self):
        ev = normalize_event(DIE)
        self.assertEqual(ev["type"], "container")
        self.assertEqual(ev["action"], "die")
        self.assertEqual(ev["actorId"], "abc123def456")
        self.assertEqual(ev["actorName"], "web")
        self.assertEqual(ev["image"], "nginx:1.27")
        self.assertEqual(ev["time"], "2025-10-25T13:46:40.123Z")
        # labels are dropped, interesting attributes kept
        self.assertEqual(ev["attributes"], {"exitCode": "137"})
        self.assertTrue(ev["id"].startswith("1761400000123456789-container-"))

    def test_action_suffix_moves_to_detail(self):
        raw = dict(DIE, Action="exec_start: sh -c ls", status="exec_start: sh -c ls")
        ev = normalize_event(raw)
        self.assertEqual(ev["action"], "exec_start")
        self.assertEqual(ev["attributes"]["detail"], "sh -c ls")
        ev = normalize_event(dict(DIE, Action="health_status: healthy"))
        self.assertEqual(ev["action"], "health_status")
        self.assertEqual(ev["attributes"]["detail"], "healthy")

    def test_image_network_volume(self):
        img = normalize_event({
            "Type": "image", "Action": "pull",
            "Actor": {"ID": "redis:7", "Attributes": {"name": "redis:7"}},
            "time": 1761400000,
        })
        self.assertEqual((img["actorName"], img["image"]), ("redis:7", "redis:7"))
        self.assertEqual(img["time"], "2025-10-25T13:46:40.000Z")

        net = normalize_event({
            "Type": "network", "Action": "connect",
            "Actor": {"ID": "n1", "Attributes": {"name": "bridge", "type": "bridge", "container": "c1"}},
            "time": 1,
        })
        self.assertEqual(net["actorName"], "bridge")
        self.assertEqual(net["attributes"], {"container": "c1", "type": "bridge"})
        self.assertEqual(net["image"], "")

        vol = normalize_event({
            "Type": "volume", "Action": "create",
            "Actor": {"ID": "data", "Attributes": {"driver": "local"}}, "time": 1,
        })
        self.assertEqual(vol["actorName"], "data")
        self.assertEqual(vol["attributes"], {"driver": "local"})

    def test_legacy_and_invalid(self):
        legacy = normalize_event({"status": "start", "id": "c9", "from": "alpine", "time": 5})
        self.assertEqual((legacy["type"], legacy["action"], legacy["image"]), ("container", "start", "alpine"))
        self.assertIsNone(normalize_event({"message": "boom"}))
        self.assertIsNone(normalize_event([]))
        self.assertIsNone(parse_event_line(b""))
        self.assertIsNone(parse_event_line(b"not json"))
        self.assertEqual(parse_event_line(json.dumps(DIE).encode())["action"], "die")


class _BytesStream:
    def __init__(self, data: bytes):
        self._data = data
        self.closed = False

    async def read(self, n=-1):
        if n is None or n < 0:
            n = len(self._data)
        out, self._data = self._data[:n], self._data[n:]
        return out

    def __aiter__(self):
        return self

    async def __anext__(self):
        chunk = await self.read(65536)
        if not chunk:
            raise StopAsyncIteration
        return chunk

    async def close(self):
        self.closed = True


class _IdleStream:
    """A live events stream with nothing happening: read blocks until closed."""

    def __init__(self):
        self._closed = asyncio.Event()
        self.closed = False

    async def read(self, n=-1):
        await self._closed.wait()
        return b""

    async def close(self):
        self.closed = True
        self._closed.set()


class FakeOps:
    def __init__(self, stream):
        self.stream = stream
        self.args = None

    async def events(self, since, until, filters):
        self.args = (since, until, filters)
        return self.stream


class TestEventsOverTunnel(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        ta, tb = memory_pair()
        self.cp = Session(ta, is_client=True).start()
        self.agent = Session(tb, is_client=False).start()

    async def asyncTearDown(self):
        self.serve.cancel()
        await self.cp.close()
        await self.agent.close()

    def _serve(self, ops):
        self.serve = asyncio.ensure_future(serve_agent_ops(self.agent, ops))
        return make_remote_client(lambda: self.cp.open_stream())

    async def test_bounded_history(self):
        lines = (json.dumps(DIE) + "\n" + json.dumps(dict(DIE, Action="start")) + "\n").encode()
        ops = FakeOps(_BytesStream(lines))
        remote = self._serve(ops)
        body = await remote.events(100, 200, {"type": ["container"]})
        data = await body.read(-1)
        await body.close()
        evs = [parse_event_line(l) for l in data.splitlines()]
        self.assertEqual([e["action"] for e in evs], ["die", "start"])
        self.assertEqual(ops.args, (100, 200, {"type": ["container"]}))

    async def test_idle_live_stream_closed_by_client(self):
        idle = _IdleStream()
        ops = FakeOps(idle)
        remote = self._serve(ops)
        body = await remote.events(None, None, None)
        self.assertEqual(ops.args, (None, None, None))
        await body.close()  # resets the mux stream
        # The agent must notice the reset and close the Engine stream even
        # though no event ever arrived.
        for _ in range(100):
            if idle.closed:
                break
            await asyncio.sleep(0.01)
        self.assertTrue(idle.closed, "agent did not close the idle events stream")



class _HostOps(FakeOps):
    pass


class TestEventsHTTP(unittest.IsolatedAsyncioTestCase):
    async def _client(self, ops):
        from types import SimpleNamespace

        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from kowan.api import events as events_api
        from kowan.web import SERVICES_KEY
        from kowan.web.app import recover_middleware

        svc = SimpleNamespace(
            store=SimpleNamespace(get_host=lambda hid: SimpleNamespace(id=hid)),
            docker=SimpleNamespace(ops=lambda host: ops),
        )
        app = web.Application(middlewares=[recover_middleware])
        app[SERVICES_KEY] = svc
        events_api.register(app, svc)
        client = TestClient(TestServer(app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        return client

    async def test_recent_newest_first_and_filter(self):
        lines = "".join(
            json.dumps(dict(DIE, Action=a, timeNano=DIE["timeNano"] + i)) + "\n"
            for i, a in enumerate(["create", "start", "die"])
        ).encode()
        ops = _HostOps(_BytesStream(lines))
        client = await self._client(ops)
        resp = await client.get("/api/v1/hosts/local/events?type=container")
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertEqual([e["action"] for e in body], ["die", "start", "create"])
        since, until, filters = ops.args
        self.assertEqual(filters, {"type": ["container"]})
        self.assertAlmostEqual(until - since, 3600, delta=2)
        self.assertTrue(ops.stream.closed)

    async def test_bad_type_is_400(self):
        client = await self._client(_HostOps(_BytesStream(b"")))
        resp = await client.get("/api/v1/hosts/local/events?type=bogus")
        self.assertEqual(resp.status, 400)

    async def test_ws_streams_normalised_events(self):
        ops = _HostOps(_BytesStream((json.dumps(DIE) + "\n").encode()))
        client = await self._client(ops)
        ws = await client.ws_connect("/api/v1/hosts/local/events/ws")
        msg = await ws.receive_json(timeout=5)
        self.assertEqual((msg["action"], msg["actorName"]), ("die", "web"))
        await ws.close()
        self.assertIsNone(ops.args[1], "live stream must not set until")

if __name__ == "__main__":
    unittest.main()
