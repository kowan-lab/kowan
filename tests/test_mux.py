import asyncio
import unittest

from kowan.mux.session import Session
from kowan.transport.memory import memory_pair


def _spawn_serve(session, handler):
    """Background accept-loop that runs ``handler`` per stream, then closes it.

    Mirrors the old Session(on_stream=...) auto-serve the production code no
    longer carries: real serve paths (agent_ops_server / hub) use accept loops.
    """
    async def loop():
        while True:
            s = await session.accept_stream()
            if s is None:
                return
            async def run(s):
                try:
                    await handler(s)
                finally:
                    try:
                        await s.close()
                    except Exception:
                        pass
            asyncio.ensure_future(run(s))
    return asyncio.ensure_future(loop())


async def make_pair(on_client_stream=None, on_server_stream=None, window=None):
    ta, tb = memory_pair()
    kw = {} if window is None else {"window": window}
    client = Session(ta, is_client=True, **kw).start()
    server = Session(tb, is_client=False, **kw).start()
    if on_client_stream is not None:
        _spawn_serve(client, on_client_stream)
    if on_server_stream is not None:
        _spawn_serve(server, on_server_stream)
    return client, server


class TestMux(unittest.IsolatedAsyncioTestCase):
    async def test_basic_client_to_server(self):
        client, server = await make_pair()
        cs = await client.open_stream()
        await cs.write(b"hello world")
        await cs.close()
        ss = await server.accept_stream()
        self.assertEqual(await ss.readall(), b"hello world")
        await client.close()
        await server.close()

    async def test_symmetric_server_opens_stream(self):
        # The key reverse-tunnel requirement: the SERVER (agent) side can
        # ALSO open a stream to the client (control-plane).
        client, server = await make_pair()
        ss = await server.open_stream()
        await ss.write(b"event: ping")
        await ss.close()
        cs = await client.accept_stream()
        self.assertEqual(await cs.readall(), b"event: ping")
        # ids come from disjoint spaces
        self.assertEqual(ss.id % 2, 0)  # server -> even
        await client.close()
        await server.close()

    async def test_stream_id_spaces_disjoint(self):
        client, server = await make_pair()
        c1 = await client.open_stream()
        c2 = await client.open_stream()
        s1 = await server.open_stream()
        self.assertEqual(c1.id % 2, 1)   # client -> odd
        self.assertEqual(c2.id % 2, 1)
        self.assertEqual(s1.id % 2, 0)   # server -> even
        self.assertNotEqual(c1.id, c2.id)
        await client.close()
        await server.close()

    async def test_duplex_echo_on_one_stream(self):
        async def echo(s):
            data = await s.readall()
            await s.write(b"echo:" + data)
            await s.close()

        client, server = await make_pair(on_server_stream=echo)
        cs = await client.open_stream()
        await cs.write(b"hi")
        await cs.close()  # half-close so the server's readall() returns
        self.assertEqual(await cs.readall(), b"echo:hi")
        await client.close()
        await server.close()

    async def test_concurrent_streams_isolated(self):
        async def echo(s):
            data = await s.readall()
            await s.write(data[::-1])
            await s.close()

        client, server = await make_pair(on_server_stream=echo)
        payloads = [b"alpha", b"bravo", b"charlie", b"delta", b"echo!!"]

        async def roundtrip(p):
            s = await client.open_stream()
            await s.write(p)
            await s.close()
            return await s.readall()

        results = await asyncio.gather(*(roundtrip(p) for p in payloads))
        self.assertEqual(results, [p[::-1] for p in payloads])
        await client.close()
        await server.close()

    async def test_backpressure_blocks_until_drained(self):
        W = 1024
        ta, tb = memory_pair()
        client = Session(ta, is_client=True, window=W).start()
        server = Session(tb, is_client=False, window=W).start()

        cs = await client.open_stream()
        ss = await server.accept_stream()

        payload = bytes(4 * W)  # 4x the window — cannot fit without credit
        writer = asyncio.ensure_future(cs.write(payload))
        await asyncio.sleep(0.05)
        self.assertFalse(writer.done(),
                         "write must block once the receive window is exhausted")

        # Drain on the receiver; each read replenishes credit via WINDOW_UPDATE.
        got = bytearray()
        while len(got) < len(payload):
            got.extend(await ss.read(65536))
        await asyncio.wait_for(writer, timeout=2)
        self.assertEqual(len(got), len(payload))
        await client.close()
        await server.close()

    async def test_large_transfer_integrity(self):
        async def sink_and_report(s):
            data = await s.readall()
            await s.write(str(len(data)).encode())
            await s.close()

        client, server = await make_pair(on_server_stream=sink_and_report, window=4096)
        cs = await client.open_stream()
        blob = bytes((i % 256) for i in range(200_000))
        await cs.write(blob)
        await cs.close()
        self.assertEqual(await cs.readall(), b"200000")
        await client.close()
        await server.close()

    async def test_reset_propagates(self):
        client, server = await make_pair()
        cs = await client.open_stream()
        await cs.write(b"x")
        ss = await server.accept_stream()
        await ss.read()          # consume the byte
        await cs.reset()         # abort from the client
        from kowan.mux.stream import StreamReset
        with self.assertRaises(StreamReset):
            # eventually the reset is observed on the server read
            for _ in range(100):
                await ss.read()
        await client.close()
        await server.close()

    async def test_window_overrun_resets_stream(self):
        # A peer that ignores flow control (sends DATA past the credit it was
        # granted) gets its stream reset instead of growing our buffer.
        from kowan.mux.stream import StreamReset
        client, server = await make_pair(window=1024)
        cs = await client.open_stream()
        await cs.write(b"a" * 1024)  # exactly the window: accepted
        ss = await server.accept_stream()
        await client._send_data(cs.id, b"b")  # one byte over, bypassing the window
        for _ in range(100):
            if ss._reset:
                break
            await asyncio.sleep(0)
        with self.assertRaises(StreamReset):
            await ss.read()
        self.assertNotIn(ss.id, server._streams)
        self.assertEqual(len(ss._recv_buf), 0)
        # The offender learns about it too.
        for _ in range(100):
            if cs._reset:
                break
            await asyncio.sleep(0)
        self.assertTrue(cs._reset)
        await client.close()
        await server.close()

    async def test_refused_peer_streams(self):
        # The control plane never accepts streams: an agent-opened one is reset
        # at once and nothing waits in the accept queue.
        from kowan.mux.stream import StreamReset
        ta, tb = memory_pair()
        client = Session(ta, is_client=True, accept_streams=False).start()
        server = Session(tb, is_client=False).start()
        ss = await server.open_stream()
        with self.assertRaises(StreamReset):
            for _ in range(100):
                await ss.write(b"x")
                await asyncio.sleep(0)
        self.assertEqual(client._accept_q.qsize(), 0)
        self.assertEqual(client._streams, {})
        await client.close()
        await server.close()


if __name__ == "__main__":
    unittest.main()
