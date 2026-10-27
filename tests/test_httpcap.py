"""kowan.httpcap: bodies are read only up to their limit, on the real aiohttp
path and through the mock seam; requests carry what the caller asked for."""

import asyncio
import unittest

from aiohttp import web

from kowan import httpcap


async def _serve(app: web.Application):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    return runner, f"http://127.0.0.1:{port}"


class TestMock(unittest.TestCase):
    def test_stops_at_limit(self):
        sent = {"bytes": 0}

        async def stream():
            for _ in range(1000):
                sent["bytes"] += 65536
                yield b"x" * 65536

        async def go():
            with httpcap.mock(lambda r: httpcap.Reply(200, content=stream())):
                return await httpcap.request("GET", "https://feed.example/", limit=1000)

        resp = asyncio.run(go())
        self.assertEqual((resp.status_code, len(resp.content), resp.truncated), (200, 1000, True))
        self.assertLess(sent["bytes"], 1 << 20)  # never pulled the whole 64 MB

    def test_request_shape_and_redirects(self):
        seen = []

        async def handler(r: httpcap.Request):
            seen.append(r)
            if r.url.path == "/start":
                return httpcap.Reply(302, headers={"Location": "https://other.example/end?x=1"})
            return httpcap.Reply(200, json={"ok": True})

        async def go():
            with httpcap.mock(handler):
                a = await httpcap.request("POST", "https://a.example/start", limit=100, data={"k": "v"},
                                          auth=("u", "p"), params={"q": 1})
                b = await httpcap.request("POST", "https://a.example/start", limit=100, json={"n": 1},
                                          follow_redirects=True, auth=("u", "p"))
            return a, b

        a, b = asyncio.run(go())
        self.assertEqual(a.status_code, 302)
        self.assertEqual(seen[0].form(), {"k": ["v"]})
        self.assertEqual(seen[0].url.query["q"], "1")
        self.assertTrue(seen[0].headers["Authorization"].startswith("Basic "))
        self.assertEqual((b.status_code, b.json(), b.url, b.is_success), (200, {"ok": True}, "https://other.example/end?x=1", True))
        self.assertEqual(seen[1].json(), {"n": 1})
        self.assertEqual(seen[2].method, "GET")  # 302 after POST becomes GET
        self.assertNotIn("Authorization", seen[2].headers)  # dropped on a new origin

    def test_redirect_loop(self):
        async def go():
            with httpcap.mock(lambda r: httpcap.Reply(301, headers={"Location": "/again"})):
                await httpcap.request("GET", "https://a.example/", limit=10, follow_redirects=True)

        with self.assertRaisesRegex(httpcap.HTTPError, "too many redirects"):
            asyncio.run(go())


class TestReal(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async def big(_):
            resp = web.StreamResponse()
            await resp.prepare(_)
            try:
                for _i in range(64):
                    await resp.write(b"y" * 65536)
            except ConnectionError:
                pass  # the client stopped reading at its limit
            return resp

        async def echo(request):
            return web.json_response({"method": request.method, "body": (await request.read()).decode(),
                                      "auth": request.headers.get("Authorization", ""), "q": request.query_string})

        async def slow(_):
            await asyncio.sleep(2)
            return web.Response(text="late")

        app = web.Application()
        app.router.add_get("/big", big)
        app.router.add_route("*", "/echo", echo)
        app.router.add_get("/slow", slow)
        app.router.add_get("/hop", lambda _: web.HTTPFound("/echo"))
        self.runner, self.url = await _serve(app)

    async def asyncTearDown(self):
        await self.runner.cleanup()

    async def test_capped_and_options(self):
        r = await httpcap.request("GET", self.url + "/big", limit=5000)
        self.assertEqual((r.status_code, len(r.content), r.truncated), (200, 5000, True))
        async with httpcap.Client(headers={"X-A": "1"}) as c:
            r = await c.request("PUT", self.url + "/echo", limit=1 << 16, json={"a": 1}, auth=("u", "p"),
                                params={"z": "9"})
        body = r.json()
        self.assertEqual((body["method"], body["body"], body["q"]), ("PUT", '{"a":1}', "z=9"))
        self.assertTrue(body["auth"].startswith("Basic "))
        self.assertFalse(r.truncated)
        hop = await httpcap.request("GET", self.url + "/hop", limit=100)
        self.assertEqual(hop.status_code, 302)
        hop = await httpcap.request("GET", self.url + "/hop", limit=1 << 16, follow_redirects=True)
        self.assertEqual((hop.status_code, hop.url), (200, self.url + "/echo"))

    async def test_errors(self):
        with self.assertRaisesRegex(httpcap.Timeout, "timed out"):
            await httpcap.request("GET", self.url + "/slow", limit=10, timeout=0.2)
        await self.runner.cleanup()
        with self.assertRaises(httpcap.HTTPError):
            await httpcap.request("GET", self.url + "/echo", limit=10, timeout=2)
        with self.assertRaises(httpcap.HTTPError):
            await httpcap.request("GET", "not a url", limit=10)
        self.runner, self.url = await _serve(web.Application())


if __name__ == "__main__":
    unittest.main()
