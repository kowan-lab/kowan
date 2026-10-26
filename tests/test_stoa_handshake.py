"""Direct mode (stoa): the server and the agent prove they share the token
without ever sending it, and the proofs are bound to the agent's TLS
certificate, so an interceptor with its own certificate fails on both sides."""

import asyncio
import unittest

from kowan.transport.memory import memory_pair
from kowan.tunnel.endpoint import establish_agent_side, establish_server_side
from kowan.tunnel.handshake import HandshakeError, preauth_ok, preauth_value, tls_binding

TOKEN = "t" * 64
AGENT_CERT = tls_binding(b"agent-certificate-der")
MITM_CERT = tls_binding(b"interceptor-certificate-der")


class Recording:
    """Wraps a transport and keeps every byte it sends."""

    def __init__(self, inner):
        self.inner, self.sent = inner, []

    async def send(self, data):
        self.sent.append(bytes(data))
        await self.inner.send(data)

    async def recv(self):
        return await self.inner.recv()

    async def close(self):
        await self.inner.close()


async def _connect(server_token, agent_token, server_binding, agent_binding):
    t_server, t_agent = memory_pair()
    rs, ra = Recording(t_server), Recording(t_agent)
    agent = asyncio.ensure_future(establish_agent_side(ra, dialed=False, token=agent_token, binding=agent_binding))
    server = asyncio.ensure_future(establish_server_side(rs, dialed=True, token=server_token, host_id="h1",
                                                         binding=server_binding))
    results = await asyncio.gather(server, agent, return_exceptions=True)
    for r in results:
        if not isinstance(r, Exception):
            s = r[1] if isinstance(r, tuple) else r
            await s.close()
    return results, rs.sent + ra.sent


class TestStoaHandshake(unittest.IsolatedAsyncioTestCase):
    async def test_success_without_the_token_on_the_wire(self):
        (server, agent), wire = await _connect(TOKEN, TOKEN, AGENT_CERT, AGENT_CERT)
        self.assertEqual(server[0], "h1")
        self.assertFalse(isinstance(agent, Exception))
        self.assertFalse(any(TOKEN.encode() in m for m in wire))

    async def test_interception_fails_on_both_sides(self):
        # The server sees the interceptor's certificate, the agent its own.
        (server, agent), _ = await _connect(TOKEN, TOKEN, MITM_CERT, AGENT_CERT)
        self.assertIsInstance(server, HandshakeError)
        self.assertIsInstance(agent, HandshakeError)

    async def test_impostor_agent_is_refused(self):
        # Something at the agent's address without the token: the server must
        # not open a session (and never hands it anything).
        (server, agent), _ = await _connect(TOKEN, "x" * 64, AGENT_CERT, AGENT_CERT)
        self.assertIsInstance(server, HandshakeError)
        self.assertIsInstance(agent, HandshakeError)

    async def test_wrong_server_token_is_refused(self):
        (server, agent), _ = await _connect("y" * 64, TOKEN, AGENT_CERT, AGENT_CERT)
        self.assertIsInstance(agent, HandshakeError)
        self.assertIsInstance(server, HandshakeError)


class TestPreauth(unittest.TestCase):
    def test_fresh_value_accepted(self):
        self.assertTrue(preauth_ok(TOKEN, preauth_value(TOKEN, now=1000), now=1000))
        self.assertNotIn(TOKEN, preauth_value(TOKEN))

    def test_stale_wrong_or_missing_refused(self):
        self.assertFalse(preauth_ok(TOKEN, preauth_value(TOKEN, now=1000), now=1000 + 600))
        self.assertFalse(preauth_ok(TOKEN, preauth_value("z" * 64, now=1000), now=1000))
        self.assertFalse(preauth_ok(TOKEN, "Bearer " + TOKEN, now=1000))
        self.assertFalse(preauth_ok("", preauth_value("", now=1000), now=1000))


if __name__ == "__main__":
    unittest.main()
