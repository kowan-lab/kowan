"""A quick agent call that gets no answer fails instead of hanging; long ops wait."""

import asyncio
import unittest
from unittest import mock

from kowan.docker import remote
from kowan.errors import BadGateway


class _Stream:
    reset_called = False

    async def reset(self):
        _Stream.reset_called = True


class TestRemoteTimeout(unittest.IsolatedAsyncioTestCase):
    async def test_unary_timeout(self):
        async def dial():
            return _Stream()

        async def never(stream, op, args):
            await asyncio.sleep(3600)

        async def slow_ok(stream, op, args):
            await asyncio.sleep(0.2)
            return {"ok": True}, None

        client = remote.make_remote_client(dial)
        with mock.patch.object(remote, "UNARY_TIMEOUT", 0.05), mock.patch.object(remote.agentproto, "call", never):
            with self.assertRaisesRegex(BadGateway, "did not answer list_containers"):
                await client._call("list_containers", {"all": True})
        self.assertTrue(_Stream.reset_called)
        with mock.patch.object(remote, "UNARY_TIMEOUT", 0.05), mock.patch.object(remote.agentproto, "call", slow_ok):
            self.assertEqual(await client._call("compose_up", {}), {"ok": True})  # a long op waits


if __name__ == "__main__":
    unittest.main()
