"""Typed errors keep their HTTP status through the API and the agent tunnel."""

import asyncio
import json
import unittest

from kowan import agentproto
from kowan.errors import AppError, NotFound, Validation
from kowan.web import ops_error_response


class OpsErrorResponseTests(unittest.TestCase):
    def body(self, resp):
        return json.loads(resp.body)

    def test_not_found_maps_to_404(self):
        r = ops_error_response(NotFound("no such container: abc"))
        self.assertEqual(r.status, 404)
        self.assertEqual(self.body(r)["error"], "no such container: abc")

    def test_validation_maps_to_400(self):
        self.assertEqual(ops_error_response(Validation("bad name")).status, 400)

    def test_untyped_error_stays_502(self):
        r = ops_error_response(RuntimeError("socket closed"))
        self.assertEqual(r.status, 502)
        self.assertEqual(self.body(r)["error"], "socket closed")


class _Buf:
    def __init__(self):
        self.data = b""

    async def write(self, b):
        self.data += b


class AgentErrorFrameTests(unittest.TestCase):
    def frame(self, *args):
        buf = _Buf()
        asyncio.run(agentproto._write_error(buf, *args))
        return json.loads(buf.data)

    def test_status_included_for_typed_errors(self):
        self.assertEqual(self.frame("gone", 404), {"ok": False, "error": "gone", "status": 404})

    def test_status_omitted_when_unknown(self):
        self.assertEqual(self.frame("boom"), {"ok": False, "error": "boom"})
        self.assertEqual(self.frame("boom", "x"), {"ok": False, "error": "boom"})

    def test_app_error_carries_status(self):
        err = AppError("gone", status=404)
        self.assertEqual(err.status, 404)


if __name__ == "__main__":
    unittest.main()
