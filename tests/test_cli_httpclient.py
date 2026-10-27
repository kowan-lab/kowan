"""kowan.cli.httpclient: the stdlib client kowanctl uses, against a real local
HTTP server (chunked uploads, capped and streamed reads, timeouts, errors) and
through its handler seam."""

import json
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from kowan.cli.httpclient import Headers, HTTPError, Reply, Session


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep the test output quiet
        pass

    def _body(self) -> bytes:
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            out = b""
            while True:
                size = int(self.rfile.readline().strip(), 16)
                if size == 0:
                    self.rfile.readline()
                    return out
                out += self.rfile.read(size)
                self.rfile.readline()
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def _send(self, status: int, data: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/slow":
            time.sleep(1)
        if self.path == "/big":
            return self._send(200, b"z" * 5000)
        self._send(200, json.dumps({"path": self.path, "x": self.headers.get("X-Test", "")}).encode())

    def do_POST(self):
        body = self._body()
        self._send(201, json.dumps({"len": len(body), "te": self.headers.get("Transfer-Encoding", ""),
                                    "ct": self.headers.get("Content-Type", "")}).encode())


class TestRealServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.srv.daemon_threads = True
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def test_json_query_and_headers(self):
        r = Session().request("GET", self.url + "/a?b=1", headers={"X-Test": "yes"})
        self.assertEqual((r.status_code, r.reason_phrase, r.is_success), (200, "OK", True))
        self.assertEqual(r.json(), {"path": "/a?b=1", "x": "yes"})
        self.assertEqual(r.headers.get("content-length"), str(len(r.content)))

    def test_uploads(self):
        r = Session().request("POST", self.url + "/up", json={"a": 1})
        self.assertEqual(r.json(), {"len": 7, "te": "", "ct": "application/json"})
        r = Session().request("POST", self.url + "/up", content=iter([b"ab", b"cde"]))
        self.assertEqual(r.json()["len"], 5)
        self.assertEqual(r.json()["te"], "chunked")

    def test_capped_and_streamed(self):
        r = Session().request("GET", self.url + "/big", limit=1000)
        self.assertEqual((len(r.content), r.truncated), (1000, True))
        with Session().request("GET", self.url + "/big", stream=True) as r:
            self.assertEqual(sum(len(c) for c in r.iter_bytes(1024)), 5000)

    def test_errors(self):
        with self.assertRaisesRegex(HTTPError, "timed out"):
            Session(timeout=0.2).request("GET", self.url + "/slow")
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        with self.assertRaises(HTTPError):
            Session(timeout=2).request("GET", f"http://127.0.0.1:{port}/")
        for bad in ("ftp://x/", "nope", "http://h:notaport/"):
            with self.assertRaises(HTTPError):
                Session().request("GET", bad)


class TestHandler(unittest.TestCase):
    def test_fake_and_headers(self):
        seen = []

        def handler(req):
            seen.append(req)
            return Reply(200, text="hello" * 10)

        s = Session(handler=handler)
        r = s.request("PUT", "http://k/x", json={"a": "è"}, limit=8)
        self.assertEqual((r.content, r.truncated, r.text), (b"hellohel", True, "hellohel"))
        self.assertEqual(seen[0].json(), {"a": "è"})
        self.assertEqual(seen[0].headers["content-type"], "application/json")
        r = s.request("POST", "http://k/x", content=iter([b"a", b"b"]))
        self.assertEqual(seen[1].content, b"ab")
        self.assertEqual(list(r.iter_bytes()), [b"hello" * 10])
        s.close()
        h = Headers([("A", "1")])
        self.assertEqual((h["a"], "A" in h, 1 in h, h.get("b", "-"), list(h.items())), ("1", True, False, "-", [("A", "1")]))


if __name__ == "__main__":
    unittest.main()
