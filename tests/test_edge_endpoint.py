"""The public edge endpoint refuses strangers before it upgrades to WebSocket,
bounds message size, and drops a peer that never completes the handshake."""

import asyncio
import datetime
import json
import tempfile
import unittest
from unittest import mock

import aiohttp
from aiohttp.test_utils import TestClient, TestServer

from kowan.config import ServerConfig
from kowan.models import Host
from kowan.server import build_app

TOKEN = "e" * 64


def _cfg():
    return ServerConfig(
        addr="127.0.0.1:0",
        data_dir=tempfile.mkdtemp(),
        jwt_secret="x" * 48,
        monitor_interval=datetime.timedelta(seconds=3600),
        metrics_retention=datetime.timedelta(hours=1),
        secret_key="",
        external_url="",
    )


class TestEdgeEndpoint(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        app, self.svc = await build_app(_cfg())
        self.svc.store.create_host(Host(id="h-edge", name="edge", type="agent", mode="edge",
                                        token=TOKEN, createdAt="2026-01-01T00:00:00Z"))
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_unknown_token_is_refused_before_upgrade(self):
        for headers in ({}, {"Authorization": "Bearer nope"}, {"Authorization": "Bearer "}):
            with self.assertRaises(aiohttp.WSServerHandshakeError) as ctx:
                await self.client.ws_connect("/edge/connect", headers=headers)
            self.assertEqual(ctx.exception.status, 401, headers)

    async def test_silent_peer_is_dropped(self):
        with mock.patch("kowan.server.HANDSHAKE_TIMEOUT", 0.2):
            ws = await self.client.ws_connect("/edge/connect", headers={"Authorization": "Bearer " + TOKEN})
            msg = await asyncio.wait_for(ws.receive(), 5)
            self.assertIn(msg.type, (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING))

    async def test_oversized_message_closes_the_socket(self):
        ws = await self.client.ws_connect("/edge/connect", headers={"Authorization": "Bearer " + TOKEN})
        try:
            await ws.send_bytes(b"x" * (1 << 20))
        except (ConnectionError, aiohttp.ClientError):
            return  # the server already hung up mid-send: refused as well
        msg = await asyncio.wait_for(ws.receive(), 5)
        self.assertIn(msg.type, (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED,
                                 aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.ERROR))


if __name__ == "__main__":
    unittest.main()
