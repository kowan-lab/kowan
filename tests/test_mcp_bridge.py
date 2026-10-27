"""Tests for the ``kowanctl mcp`` stdio <-> HTTP bridge against a fake server
(a kowan.cli.httpclient handler): session handling, headers, notifications, SSE replies,
HTTP errors, re-initialization after a lost session and cleanup."""

from __future__ import annotations

import io
import json
import os
import unittest
from unittest import mock

from kowan.cli.httpclient import HTTPError, Reply, Request, Session
from kowan.cli.mcp import Bridge, resolve_bridge_config


class FakeServer:
    def __init__(self):
        self.requests = []
        self.sessions = set()
        self.counter = 0
        self.sse = False
        self.fail_status = None

    def __call__(self, request: Request) -> Reply:
        self.requests.append(request)
        if self.fail_status:
            return Reply(self.fail_status, json={"jsonrpc": "2.0", "id": None,
                                                 "error": {"code": -32000, "message": "nope"}})
        if request.method == "DELETE":
            self.sessions.discard(request.headers.get("mcp-session-id"))
            return Reply(204)
        msg = json.loads(request.content)
        if msg.get("method") == "initialize":
            self.counter += 1
            sid = f"s{self.counter}"
            self.sessions.add(sid)
            return Reply(200, headers={"Mcp-Session-Id": sid}, json={
                "jsonrpc": "2.0", "id": msg["id"], "result": {"protocolVersion": "2025-06-18"}})
        if request.headers.get("mcp-session-id") not in self.sessions:
            return Reply(404, json={"jsonrpc": "2.0", "id": None,
                                    "error": {"code": -32000, "message": "unknown session"}})
        if "id" not in msg:
            return Reply(202)
        body = {"jsonrpc": "2.0", "id": msg["id"], "result": {"echo": msg["method"]}}
        if self.sse:
            return Reply(200, headers={"Content-Type": "text/event-stream"},
                         content=f"event: message\ndata: {json.dumps(body)}\n\n".encode())
        return Reply(200, json=body)


def make_bridge(server: FakeServer) -> Bridge:
    http = Session(handler=server)
    return Bridge("http://kowan.test/", api_key="pk_test", http=http, log=io.StringIO())


INIT = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})


class TestBridge(unittest.TestCase):
    def test_session_and_headers(self):
        srv = FakeServer()
        b = make_bridge(srv)
        out = b.handle_line(INIT)
        self.assertEqual(json.loads(out[0])["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(b.session_id, "s1")
        first = srv.requests[0]
        self.assertEqual(str(first.url), "http://kowan.test/mcp")
        self.assertEqual(first.headers["authorization"], "Bearer pk_test")
        self.assertIn("text/event-stream", first.headers["accept"])
        self.assertNotIn("mcp-session-id", first.headers)

        self.assertEqual(b.handle_line('{"jsonrpc":"2.0","method":"notifications/initialized"}'), [])
        out = b.handle_line('{"jsonrpc":"2.0","id":2,"method":"tools/list"}')
        self.assertEqual(json.loads(out[0]), {"jsonrpc": "2.0", "id": 2, "result": {"echo": "tools/list"}})
        last = srv.requests[-1]
        self.assertEqual(last.headers["mcp-session-id"], "s1")
        self.assertEqual(last.headers["mcp-protocol-version"], "2025-06-18")

    def test_sse_reply(self):
        srv = FakeServer()
        b = make_bridge(srv)
        b.handle_line(INIT)
        srv.sse = True
        out = b.handle_line('{"jsonrpc":"2.0","id":"x","method":"ping"}')
        self.assertEqual(json.loads(out[0])["id"], "x")

    def test_bad_stdin_line(self):
        b = make_bridge(FakeServer())
        out = b.handle_line("{nope")
        self.assertEqual(json.loads(out[0])["error"]["code"], -32700)
        self.assertEqual(b.handle_line("   "), [])

    def test_reinitializes_after_lost_session(self):
        srv = FakeServer()
        b = make_bridge(srv)
        b.handle_line(INIT)
        srv.sessions.clear()  # server restarted
        out = b.handle_line('{"jsonrpc":"2.0","id":5,"method":"tools/list"}')
        self.assertEqual(json.loads(out[0])["result"]["echo"], "tools/list")
        self.assertEqual(b.session_id, "s2")

    def test_http_error_becomes_jsonrpc_error(self):
        srv = FakeServer()
        b = make_bridge(srv)
        srv.fail_status = 401
        out = b.handle_line('{"jsonrpc":"2.0","id":9,"method":"ping"}')
        err = json.loads(out[0])
        self.assertEqual(err["id"], 9)
        self.assertEqual(err["error"]["code"], -32001)
        self.assertIn("401", err["error"]["message"])
        self.assertEqual(b.handle_line('{"jsonrpc":"2.0","method":"notifications/x"}'), [])

    def test_unreachable_server(self):
        def boom(request):
            raise HTTPError("refused")

        b = Bridge("http://kowan.test", api_key="pk", http=Session(handler=boom),
                   log=io.StringIO())
        out = b.handle_line('{"jsonrpc":"2.0","id":1,"method":"ping"}')
        self.assertIn("unreachable", json.loads(out[0])["error"]["message"])

    def test_run_loop_and_delete_on_eof(self):
        srv = FakeServer()
        b = make_bridge(srv)
        stdin = io.StringIO(INIT + "\n" + '{"jsonrpc":"2.0","id":2,"method":"ping"}\n')
        stdout = io.StringIO()
        b.run(stdin, stdout)
        lines = stdout.getvalue().splitlines()
        self.assertEqual([json.loads(l)["id"] for l in lines], [1, 2])
        self.assertEqual(srv.requests[-1].method, "DELETE")
        self.assertEqual(srv.sessions, set())

    def test_env_config(self):
        with mock.patch.dict(os.environ, {"KOWAN_URL": "http://env.test", "KOWAN_API_KEY": "pk_env"}):
            server, key, _token = resolve_bridge_config()
            self.assertEqual((server, key), ("http://env.test", "pk_env"))
            server, key, _token = resolve_bridge_config("http://flag.test", "pk_flag")
            self.assertEqual((server, key), ("http://flag.test", "pk_flag"))


if __name__ == "__main__":
    unittest.main()
