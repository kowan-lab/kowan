"""Direct mode over real TLS: the server connects to an agent serving its
generated certificate, and a man in the middle that terminates TLS with its own
certificate and relays every message cannot get a session established."""

import asyncio
import ssl
import unittest
from types import SimpleNamespace

import aiohttp
from aiohttp import web

from kowan.agent import _build_ssl_context, _generate_self_signed_cert, _handle_connect, _load_cert_chain_from_mem
from kowan.config import AgentConfig
from kowan.dialer import StoaDialer
from kowan.models import Container, Host
from kowan.tunnel import Hub
from kowan.tunnel.handshake import tls_binding

TOKEN = "s" * 64


class FakeOps:
    async def ping(self):
        return None

    async def list_containers(self, all):
        return [Container(id="c1", image="nginx", state="running")]


class FakeStore:
    def __init__(self, host):
        self.host = host

    def list_hosts(self):
        return [self.host]

    def get_host(self, host_id):
        return self.host


async def _site(app, ssl_ctx):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=ssl_ctx)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, port


class TestStoaOverTLS(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        cfg = AgentConfig(token=TOKEN, mode="agent")
        ctx, der = _build_ssl_context(cfg)
        binding = tls_binding(der)
        app = web.Application()

        async def connect(request):
            return await _handle_connect(request, cfg, FakeOps(), binding)

        app.router.add_get("/connect", connect)
        self.agent_runner, self.agent_port = await _site(app, ctx)
        self.addAsyncCleanup(self.agent_runner.cleanup)

    async def _connects(self, address, wait=3.0):
        host = Host(id="h1", name="vm", type="agent", mode="agent", address=address, token=TOKEN)
        hub = Hub()
        dialer = StoaDialer(SimpleNamespace(store=FakeStore(host), hub=hub), interval=0.2)
        await dialer.start()
        try:
            for _ in range(int(wait / 0.1)):
                if hub.connected("h1"):
                    return True
                await asyncio.sleep(0.1)
            return False
        finally:
            await dialer.stop()

    async def test_direct_tls_connects(self):
        self.assertTrue(await self._connects(f"wss://127.0.0.1:{self.agent_port}"))

    async def test_tls_interceptor_cannot_establish_a_session(self):
        seen = []
        upstream_ssl = ssl.create_default_context()
        upstream_ssl.check_hostname = False
        upstream_ssl.verify_mode = ssl.CERT_NONE

        async def relay(request):
            # Terminate the server's TLS with our own certificate and pass every
            # message on to the real agent, as an on-path attacker would.
            down = web.WebSocketResponse()
            await down.prepare(request)
            async with aiohttp.ClientSession() as s:
                async with s.ws_connect(f"wss://127.0.0.1:{self.agent_port}/connect", ssl=upstream_ssl,
                                        headers={"Authorization": request.headers.get("Authorization", "")}) as up:
                    async def pump(src, dst):
                        async for msg in src:
                            if msg.type != aiohttp.WSMsgType.BINARY:
                                break
                            seen.append(msg.data)
                            await dst.send_bytes(msg.data)
                        await dst.close()

                    await asyncio.gather(pump(down, up), pump(up, down), return_exceptions=True)
            return down

        cert_pem, key_pem = _generate_self_signed_cert()
        mitm_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        _load_cert_chain_from_mem(mitm_ctx, cert_pem, key_pem)
        app = web.Application()
        app.router.add_get("/connect", relay)
        runner, port = await _site(app, mitm_ctx)
        self.addAsyncCleanup(runner.cleanup)

        self.assertFalse(await self._connects(f"wss://127.0.0.1:{port}"))
        self.assertTrue(seen, "the relay should have carried the handshake")
        self.assertFalse(any(TOKEN.encode() in m for m in seen), "the token crossed the wire")


if __name__ == "__main__":
    unittest.main()
