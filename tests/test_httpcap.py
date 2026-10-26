"""Outbound responses are read only up to their limit."""

import asyncio
import unittest

import httpx

from kowan.httpcap import request_capped


class TestCapped(unittest.TestCase):
    def test_stops_at_limit(self):
        sent = {"bytes": 0}

        async def stream():
            for _ in range(1000):
                sent["bytes"] += 65536
                yield b"x" * 65536

        def handler(request):
            return httpx.Response(200, content=stream())

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
                return await request_capped(c, "GET", "https://feed.example/", 1000)

        resp = asyncio.run(go())
        self.assertEqual((resp.status_code, len(resp.content), resp.truncated), (200, 1000, True))
        self.assertLess(sent["bytes"], 1 << 20)  # never pulled the whole 64 MB


if __name__ == "__main__":
    unittest.main()
