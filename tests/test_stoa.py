r"""End-to-end test of the STOA (server-dials-agent) transport:

    StoaDialer --ws dial--> agent /connect --> serve_agent_ops --> Ops
                 \-> registers the mux session on the Hub -> hub.dial() ops

proves the server-side dialer establishes the tunnel, registers it on the hub,
and that the control plane can then drive the agent over it — with a real
WebSocket (aiohttp agent server + websockets client dial) and a fake Ops.
"""

import asyncio
import unittest
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestServer

from kowan.agent import _handle_connect
from kowan.config import AgentConfig
from kowan.dialer import StoaDialer
from kowan.docker.remote import make_remote_client
from kowan.models import Container, Host
from kowan.tunnel import Hub
from kowan.tunnel.handshake import preauth_value

TOKEN = "stoa-secret-token"


class FakeOps:
    """Canned agent Ops (duck-typed; serve_agent_ops dispatches by method)."""

    def __init__(self):
        self.pinged = False

    async def ping(self):
        self.pinged = True
        return None

    async def list_containers(self, all):
        self.list_all = all
        return [Container(id="c1", image="nginx", state="running")]


class FakeStore:
    def __init__(self, hosts):
        self._hosts = {h.id: h for h in hosts}

    def list_hosts(self):
        return list(self._hosts.values())

    def get_host(self, host_id):
        return self._hosts[host_id]  # KeyError if absent -> dialer treats as gone


class TestStoaDialer(unittest.IsolatedAsyncioTestCase):
    async def test_server_dials_agent_and_serves_ops(self):
        ops = FakeOps()
        cfg = AgentConfig(token=TOKEN, mode="agent")

        # Agent side: a real /connect WebSocket endpoint that runs the agent's
        # acceptor handshake (verifies our token) and serves ops over the mux.
        app = web.Application()

        async def connect_handler(request):
            return await _handle_connect(request, cfg, ops)

        app.router.add_get("/connect", connect_handler)
        server = TestServer(app)
        await server.start_server()
        self.addAsyncCleanup(server.close)

        # An agent-mode host pointing at that endpoint over plain ws (no TLS in
        # the test); the dialer sends this token, the agent verifies it.
        host = Host(
            id="h1",
            name="vm",
            type="agent",
            mode="agent",
            address=f"ws://{server.host}:{server.port}",
            token=TOKEN,
        )

        hub = Hub()
        svc = SimpleNamespace(store=FakeStore([host]), hub=hub)
        dialer = StoaDialer(svc, interval=0.2)
        await dialer.start()
        self.addAsyncCleanup(dialer.stop)

        # Wait for the outbound tunnel to establish + register on the hub.
        for _ in range(50):
            if hub.connected("h1"):
                break
            await asyncio.sleep(0.1)
        self.assertTrue(hub.connected("h1"), "stoa tunnel never registered on hub")

        # Drive the agent over the tunnel exactly as the provider does.
        remote = make_remote_client(lambda: hub.dial("h1"))
        await remote.ping()
        self.assertTrue(ops.pinged, "ping did not reach the agent over the stoa tunnel")

        containers = await remote.list_containers(all=True)
        self.assertEqual([c.id for c in containers], ["c1"])
        self.assertTrue(ops.list_all, "op args did not round-trip")


if __name__ == "__main__":
    unittest.main()


class TestAgentListenerGuards(unittest.IsolatedAsyncioTestCase):
    """The agent's /connect refuses wrong tokens and disallowed addresses
    before it upgrades to a WebSocket."""

    async def _status(self, cfg, headers):
        app = web.Application()

        async def connect_handler(request):
            return await _handle_connect(request, cfg, FakeOps())

        app.router.add_get("/connect", connect_handler)
        server = TestServer(app)
        await server.start_server()
        self.addAsyncCleanup(server.close)
        import aiohttp

        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://{server.host}:{server.port}/connect", headers={
                "Connection": "Upgrade", "Upgrade": "websocket", "Sec-WebSocket-Version": "13",
                "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==", **headers}) as r:
                return r.status

    async def test_wrong_token_is_refused(self):
        cfg = AgentConfig(token=TOKEN, mode="agent")
        self.assertEqual(await self._status(cfg, {"Authorization": preauth_value("wrong")}), 401)
        self.assertEqual(await self._status(cfg, {}), 401)
        # The raw token is not a credential on the wire any more.
        self.assertEqual(await self._status(cfg, {"Authorization": "Bearer " + TOKEN}), 401)

    async def test_address_outside_allow_list_is_refused(self):
        cfg = AgentConfig(token=TOKEN, mode="agent", allow_cidrs="198.51.100.0/24")
        self.assertEqual(await self._status(cfg, {"Authorization": preauth_value(TOKEN)}), 403)
        cfg = AgentConfig(token=TOKEN, mode="agent", allow_cidrs="127.0.0.0/8")
        self.assertEqual(await self._status(cfg, {"Authorization": preauth_value(TOKEN)}), 101)
