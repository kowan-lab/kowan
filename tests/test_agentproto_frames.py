"""The agent protocol over an in-memory stream pair: request bodies sent as
bytes, readers or async iterables, streamed responses of every source shape,
uploads, archives, exec in both directions, live streams stopped by the
caller, and every error the server can answer (bad header, unknown op,
typed errors, framing errors, the Docker daemon failing)."""

import asyncio
import unittest
from unittest import mock

from kowan import agentproto as ap
from kowan import errors


class _Half:
    def __init__(self):
        self.buf = bytearray()
        self.eof = False
        self.ev = asyncio.Event()


class End:
    """One end of a duplex byte pipe with the mux stream's read/write/close/reset."""

    def __init__(self, rx, tx):
        self.rx, self.tx = rx, tx
        self.resets = 0

    async def read(self, n=65536):
        while not self.rx.buf and not self.rx.eof:
            self.rx.ev.clear()
            await self.rx.ev.wait()
        out = bytes(self.rx.buf[:n])
        del self.rx.buf[:n]
        return out

    async def write(self, data):
        if self.tx.eof:
            raise ConnectionResetError("stream reset")
        self.tx.buf.extend(data)
        self.tx.ev.set()

    async def close(self):
        self.tx.eof = True
        self.tx.ev.set()

    async def reset(self):
        self.resets += 1
        for h in (self.rx, self.tx):
            h.eof = True
            h.ev.set()


def pair():
    a, b = _Half(), _Half()
    return End(a, b), End(b, a)


class Chunks:
    """An AsyncByteStream with read(n) and close()."""

    def __init__(self, data, chunk=3):
        self.data, self.chunk, self.closed = data, chunk, False

    async def read(self, n=-1):
        out, self.data = self.data[: min(n, self.chunk) if n > 0 else len(self.data)], self.data[min(n, self.chunk) if n > 0 else len(self.data):]
        return out

    async def close(self):
        self.closed = True


async def agen(*parts):
    for p in parts:
        yield p


class FakeOps:
    def __init__(self):
        self.uploaded = None
        self.archive = None
        self.stdin = bytearray()
        self.closed_src = False

    async def system_info(self):
        return {"ok": 1}

    async def read_volume_file(self, name, sub):
        return b"x" * 70000  # bytes: streamed in 32 KiB slices

    async def pull_image(self, ref, auth):
        return agen(b"a", b"", b"b")  # async iterable without read()

    async def download_volume_file(self, name, path):
        self.src = Chunks(b"hello world")
        return self.src

    async def container_logs(self, cid, follow, tail):
        ops = self

        class Live:
            async def read(self, n=-1):
                await asyncio.sleep(3600)

            async def close(self):
                ops.closed_src = True
        return Live()

    async def upload_volume_file(self, name, dest, filename, size, reader):
        first = await reader.read(4)
        rest = await reader.read(-1)
        self.uploaded = (name, dest, filename, size, first + rest)

    async def container_put_archive(self, cid, path, reader):
        head = await reader.read(5)
        if path == "/fail":
            raise errors.Validation("not a directory")
        self.archive = head + await reader.read(-1) + await reader.read(10)

    async def container_read_file(self, cid, path, limit):
        return b"file body"

    async def exec_attach(self, cid, cmd, tty):
        ops = self

        class Exec:
            def __init__(self):
                self.out = [b"$ ", b"done\n"]

            async def write(self, data):
                ops.stdin.extend(data)

            async def read(self, n):
                await asyncio.sleep(0.01)
                return self.out.pop(0) if self.out else b""

            async def close(self):
                pass
        return Exec()

    async def remove_volume(self, name, force):
        if name == "gone":
            raise errors.NotFound("no such volume")
        if name == "daemon":
            raise errors.BadGateway("docker daemon failed")
        raise RuntimeError("boom")


class TestProtocol(unittest.IsolatedAsyncioTestCase):
    async def serve(self, ops=None):
        client, server = pair()
        task = asyncio.ensure_future(ap.serve_ops(server, ops or FakeOps()))
        return client, server, task

    async def test_unary_and_errors(self):
        c, s, t = await self.serve()
        meta, body = await ap.call(c, "system_info")
        self.assertEqual((meta, body), ({"ok": 1}, None))
        await t
        for name, exc in (("gone", errors.NotFound), ("daemon", errors.BadGateway), ("other", ap.ProtocolError)):
            c, s, t = await self.serve()
            with self.assertRaises(exc):
                await ap.call(c, "remove_volume", {"name": name, "force": False})
            await t
        # Unknown op, with a body the server drains first.
        c, s, t = await self.serve()
        with self.assertRaisesRegex(ap.ProtocolError, "unknown op: frobnicate"):
            await ap.call(c, "frobnicate", body=b"payload")
        await t

    async def test_bad_frames(self):
        # A header that is not JSON.
        c, s, t = await self.serve()
        await c.write(b"{nope\n")
        self.assertIn(b"bad request header", await c.read())
        await t
        # The peer opens and closes without a header: nothing happens.
        c, s, t = await self.serve()
        await c.close()
        await t
        # A body cut short mid-frame: framing error, the server bails quietly.
        c, s, t = await self.serve()
        await c.write(b'{"op": "upload_volume_file", "args": {"name": "v"}, "body": true}\n' + ap._LEN.pack(10) + b"abc")
        await c.close()
        await t
        # The client side: a garbage response header, then EOF before any.
        c, s = pair()
        await s.write(b"not json\n")
        with self.assertRaisesRegex(ap.ProtocolError, "bad response header"):
            await ap.call(c, "system_info")
        c, s = pair()
        await s.close()
        with self.assertRaisesRegex(ap.ProtocolError, "eof before header"):
            await ap.call(c, "system_info")
        # A header line longer than the cap.
        c, s = pair()
        await s.write(b"x" * 20 + b"\n")
        with mock.patch.object(ap, "_MAX_LINE", 10), self.assertRaisesRegex(ap.ProtocolError, "too long"):
            await ap.call(c, "system_info")
        # A body frame cut short.
        c, s = pair()
        await s.write(b"ab")
        await s.close()
        with self.assertRaisesRegex(ap.ProtocolError, "eof after 2/4"):
            await ap._read_exact(c, 4)

    async def test_streams(self):
        ops = FakeOps()
        c, s, t = await self.serve(ops)
        _, body = await ap.call(c, "read_volume_file", {"name": "v"})
        self.assertEqual(len(await body.read(-1)), 70000)
        await body.close()
        await body.close()  # idempotent
        self.assertEqual(await body.read(), b"")
        await t
        c, s, t = await self.serve(ops)
        _, body = await ap.call(c, "pull_image", {"ref": "x"})
        self.assertEqual(b"".join([chunk async for chunk in body]), b"ab")
        await t
        c, s, t = await self.serve(ops)
        _, body = await ap.call(c, "download_volume_file", {"name": "v", "path": "/f"})
        self.assertEqual(await body.read(-1), b"hello world")
        await t
        self.assertTrue(ops.src.closed)
        c, s, t = await self.serve(ops)
        _, body = await ap.call(c, "container_read_file", {"id": "c", "path": "/etc/hostname"})
        self.assertEqual(await body.read(-1), b"file body")
        await t

    async def test_live_stream_stops_when_the_caller_closes(self):
        ops = FakeOps()
        c, s, t = await self.serve(ops)
        _, body = await ap.call(c, "container_logs", {"id": "c", "follow": True})
        await body.close()
        await asyncio.wait_for(t, 2)
        self.assertTrue(ops.closed_src)

    async def test_uploads_and_archives(self):
        for payload in (b"0123456789", Chunks(b"0123456789"), agen(b"01234", b"", b"56789")):
            ops = FakeOps()
            c, s, t = await self.serve(ops)
            await ap.call(c, "upload_volume_file", {"name": "v", "destDir": "/d", "filename": "f", "size": 10}, body=payload)
            await t
            self.assertEqual(ops.uploaded, ("v", "/d", "f", 10, b"0123456789"))
        ops = FakeOps()
        c, s, t = await self.serve(ops)
        await ap.call(c, "container_put_archive", {"id": "c", "path": "/srv"}, body=agen(b"tar", b"ball"))
        await t
        self.assertEqual(ops.archive, b"tarball")
        # The op fails after reading part: the rest is drained, the error reported.
        c, s, t = await self.serve(ops)
        with self.assertRaises(errors.Validation):
            await ap.call(c, "container_put_archive", {"id": "c", "path": "/fail"}, body=b"x" * 100)
        await t
        await ap._pump_body(c, None)
        await ap._pump_body(c, b"")

    async def test_exec_both_ways(self):
        ops = FakeOps()
        c, s, t = await self.serve(ops)
        _, body = await ap.call(c, "exec_attach", {"id": "c", "cmd": ["sh"]})
        await body.write(b"ls\n")
        out = b""
        while b"done" not in out:
            chunk = await body.read(100)
            if not chunk:
                break
            out += chunk
        self.assertEqual(out, b"$ done\n")
        await asyncio.wait_for(t, 2)
        self.assertEqual(bytes(ops.stdin), b"ls\n")


if __name__ == "__main__":
    unittest.main()
