"""The Docker WebSockets end to end: container logs (follow + batching),
live stats, the events stream, the interactive exec console (the engine's
hijacked stream replaced by an echoing fake), plus one-shot exec, stats and
recent events, with the role gates and the ?token= credential for browsers."""

import asyncio
import json
import unittest
from unittest import mock

import aiohttp

from kowan.docker.local import LocalClient
from tests.apicase import ServerCase

H = "/api/v1/hosts/local"


class EchoShell:
    """An interactive exec session: prints a prompt, echoes what it is sent."""

    def __init__(self):
        self.q: asyncio.Queue = asyncio.Queue()
        self.q.put_nowait(b"$ ")
        self.closed = False

    async def read(self, n=-1):
        data = await self.q.get()
        return data

    async def write(self, data):
        if data.strip() == b"exit":
            self.q.put_nowait(b"")
            return
        self.q.put_nowait(b"echo:" + data)

    async def close(self):
        self.closed = True
        self.q.put_nowait(b"")


async def _texts(ws, until=None, timeout=5.0):
    out = []
    try:
        while True:
            msg = await asyncio.wait_for(ws.receive(), timeout)
            if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
                break
            data = msg.data if isinstance(msg.data, str) else msg.data.decode()
            out.append(data)
            if until and until in "".join(out):
                break
    except asyncio.TimeoutError:
        pass
    return "".join(out)


class TestDockerSockets(ServerCase):
    async def test_logs_socket(self):
        cid = self.engine.add_container("web", logs=[f"line {i}" for i in range(30)])
        ws = await self.client.ws_connect(f"{H}/containers/{cid}/logs?follow=0&tail=10", headers=self.h["viewer"])
        text = await _texts(ws)
        self.assertIn("line 29", text)
        self.assertNotIn("line 5\n", text)
        # Browsers can't set headers on a WebSocket: the token travels as ?token=.
        token = self.h["viewer"]["Authorization"].split(" ", 1)[1]
        ws = await self.client.ws_connect(f"{H}/containers/{cid}/logs?follow=0&timestamps=1&token={token}")
        self.assertIn("2026-01-01", await _texts(ws))
        r = await self.client.get(f"{H}/containers/{cid}/logs")
        self.assertEqual(r.status, 401)

    async def test_stats(self):
        cid = self.engine.add_container("busy")
        r = await self.client.get(f"{H}/containers/{cid}/stats", headers=self.h["viewer"])
        self.assertEqual(r.status, 200)
        self.assertIn("cpuPercent", await r.json())
        ws = await self.client.ws_connect(f"{H}/containers/{cid}/stats/stream", headers=self.h["viewer"])
        text = await _texts(ws, until="memLimit", timeout=3)
        self.assertIn("cpuPercent", text)
        await ws.close()
        r = await self.client.get(f"{H}/containers/missing/stats", headers=self.h["viewer"])
        self.assertEqual(r.status, 404)

    async def test_events(self):
        cid = self.engine.add_container("evt")
        self.engine.emit("container", "start", cid, {"name": "evt"})
        self.engine.emit("container", "die", cid, {"name": "evt"})
        r = await self.client.get(f"{H}/events", headers=self.h["viewer"], params={"type": "container"})
        self.assertEqual(r.status, 200)
        self.assertEqual([e["action"] for e in await r.json()], ["die", "start"])  # newest first
        r = await self.client.get(f"{H}/events", headers=self.h["viewer"], params={"since": "x"})
        self.assertEqual(r.status, 400)
        ws = await self.client.ws_connect(f"{H}/events/ws", headers=self.h["viewer"])
        await asyncio.sleep(0.1)
        self.engine.emit("container", "restart", cid, {"name": "evt"})
        text = await _texts(ws, until="restart", timeout=3)
        self.assertIn("restart", text)
        await ws.close()

    async def test_exec(self):
        cid = self.engine.add_container("shell")
        r = await self.client.post(f"{H}/containers/{cid}/exec", headers=self.h["operator"], json={"cmd": ["echo", "hi"]})
        self.assertEqual(r.status, 200, await r.text())
        self.assertIn("hi", (await r.json())["output"])
        r = await self.client.post(f"{H}/containers/{cid}/exec", headers=self.h["operator"], json={"cmd": []})
        self.assertEqual(r.status, 400)
        r = await self.client.post(f"{H}/containers/{cid}/exec", headers=self.h["viewer"], json={"cmd": ["id"]})
        self.assertEqual(r.status, 403)
        r = await self.client.post(f"{H}/containers/missing/exec", headers=self.h["operator"], json={"cmd": ["id"]})
        self.assertEqual(r.status, 404)

        shell = EchoShell()

        async def attach(self_, id, cmd, tty):
            self.assertEqual((cmd, tty), (["/bin/bash"], False))
            return shell

        with mock.patch.object(LocalClient, "exec_attach", attach):
            ws = await self.client.ws_connect(f"{H}/containers/{cid}/exec?cmd=/bin/bash&tty=0",
                                              headers=self.h["operator"])
            self.assertEqual(await _texts(ws, until="$ "), "$ ")
            await ws.send_str("ls\n")
            self.assertIn("echo:ls", await _texts(ws, until="echo:ls"))
            await ws.send_bytes(b"pwd\n")
            self.assertIn("echo:pwd", await _texts(ws, until="echo:pwd"))
            await ws.send_str("exit\n")
            await _texts(ws, timeout=2)
            self.assertTrue(ws.closed or shell.closed)

        async def broken(self_, id, cmd, tty):
            raise RuntimeError("no such exec")

        with mock.patch.object(LocalClient, "exec_attach", broken):
            ws = await self.client.ws_connect(f"{H}/containers/{cid}/exec", headers=self.h["operator"])
            self.assertIn("no such exec", await _texts(ws, timeout=2) + str(ws.close_code) + str(
                getattr(ws, "_close_message", "")))


if __name__ == "__main__":
    unittest.main()
