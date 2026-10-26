"""Regression: the request-body readers must consume the body IN FULL.

aiohttp's ``StreamReader.read(n)`` returns as soon as ANY data is buffered — it
reads *up to* n bytes and never waits for n — so reading a body with a single
``request.content.read(N)`` truncated it to whatever had arrived so far. A
multi-KiB payload (e.g. a compose YAML deployment spec) then reached
``json.loads`` half-written and failed with:

    decode request body: Unterminated string starting at: line 1 column 145

These tests deliver the body in several DELAYED chunks so the handler's first
read sees only the first chunk — exactly the condition that used to truncate.
Without the delay the whole body lands in the buffer before the handler runs and
the bug would not reproduce.
"""

import asyncio
import json
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kowan.web.httpx import read_body, read_json

# A compose spec shaped like the real thing: many lines, quotes and colons —
# all of which live inside ONE JSON string, which is what used to get cut.
_COMPOSE = """name: mesher

services:
  frontend:
    image: 10.175.101.30:5000/mesher-frontend:staging
    entrypoint: ["nginx", "-g", "daemon off;"]
    network_mode: host
    security_opt:
      - no-new-privileges:true
""" + "\n".join(f"    # filler line {i} to push this well past one chunk" for i in range(400))


async def _chunked(payload: bytes, size: int = 512, delay: float = 0.002):
    """Yield the payload in small chunks, pausing so they arrive separately."""
    for i in range(0, len(payload), size):
        yield payload[i : i + size]
        await asyncio.sleep(delay)


def _app() -> web.Application:
    async def echo_json(request: web.Request) -> web.Response:
        body = await read_json(request)
        return web.json_response({"spec": body["spec"]})

    async def echo_raw(request: web.Request) -> web.Response:
        raw = await read_body(request, 1 << 20)
        return web.json_response({"n": len(raw)})

    async def capped(request: web.Request) -> web.Response:
        raw = await read_body(request, 16)  # tiny cap: must stop at 16 bytes
        return web.json_response({"n": len(raw)})

    app = web.Application(client_max_size=1 << 20)
    app.router.add_post("/echo-json", echo_json)
    app.router.add_post("/echo-raw", echo_raw)
    app.router.add_post("/capped", capped)
    return app


class TestReadBody(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = TestClient(TestServer(_app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_multi_chunk_json_body_is_decoded_in_full(self):
        """The exact failure: a YAML spec inside a JSON field, sent in chunks."""
        payload = json.dumps({"spec": _COMPOSE}).encode()
        self.assertGreater(len(payload), 4096, "payload must span several chunks")

        resp = await self.client.post("/echo-json", data=_chunked(payload))

        self.assertEqual(resp.status, 200, await resp.text())
        # The spec round-trips byte-for-byte: nothing was truncated.
        self.assertEqual((await resp.json())["spec"], _COMPOSE)

    async def test_multi_chunk_raw_body_is_read_in_full(self):
        payload = b"a" * 20_000
        resp = await self.client.post("/echo-raw", data=_chunked(payload))
        self.assertEqual(resp.status, 200)
        self.assertEqual((await resp.json())["n"], len(payload))

    async def test_read_body_stops_at_the_cap(self):
        """An oversized body is bounded, not buffered whole."""
        resp = await self.client.post("/capped", data=_chunked(b"b" * 4096))
        self.assertEqual(resp.status, 200)
        self.assertEqual((await resp.json())["n"], 16)

    async def test_empty_body_still_reports_eof(self):
        resp = await self.client.post("/echo-json", data=b"")
        self.assertEqual(resp.status, 400)
        self.assertIn("EOF", await resp.text())
