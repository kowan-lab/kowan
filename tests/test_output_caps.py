"""Exec output and helper logs are read with a byte limit."""

import struct
import unittest

from kowan.docker.local import _demux_stdcopy
from kowan.updater.orchestrator import read_logs


def _frame(stream: int, payload: bytes) -> bytes:
    return bytes([stream, 0, 0, 0]) + struct.pack(">I", len(payload)) + payload


class _Chunks:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.read = 0
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.chunks:
            raise StopAsyncIteration
        self.read += 1
        return self.chunks.pop(0)

    async def close(self):
        self.closed = True


class TestDemux(unittest.IsolatedAsyncioTestCase):
    async def test_frames_split_across_chunks(self):
        raw = _frame(1, b"hello ") + _frame(2, b"world") + _frame(1, b"")
        # One byte at a time: headers and payloads straddle chunk borders.
        out = await _demux_stdcopy(_Chunks(raw[i:i + 1] for i in range(len(raw))))
        self.assertEqual(out, b"hello world")

    async def test_stops_reading_past_the_limit(self):
        src = _Chunks(_frame(1, b"x" * 100) for _ in range(1000))
        out = await _demux_stdcopy(src, limit=250)
        self.assertEqual(out, b"x" * 250)
        self.assertLess(src.read, 10)  # did not drain the other ~990 frames


class _LogOps:
    def __init__(self, chunks):
        self.stream = _Chunks(chunks)

    async def container_logs(self, ref, follow, tail):
        return self.stream


class TestReadLogs(unittest.IsolatedAsyncioTestCase):
    async def test_keeps_the_end(self):
        ops = _LogOps([b"a" * 60, b"b" * 60, b"c" * 60, "done\n"])
        text = await read_logs(ops, "cid", 0, limit=50)
        self.assertEqual(len(text), 50)
        self.assertTrue(text.endswith("c" * 45 + "done\n"))
        self.assertTrue(ops.stream.closed)

    async def test_short_log_whole(self):
        self.assertEqual(await read_logs(_LogOps([b"one\n", b"two\n"]), "cid"), "one\ntwo\n")


if __name__ == "__main__":
    unittest.main()
