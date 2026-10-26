"""Batched log streaming (kowan/api/_dockerws.py).

Log WebSockets group lines into newline-joined messages, flushed every
LOG_BATCH_DELAY (50 ms) or once LOG_BATCH_BYTES (64 KB) is reached, whichever
comes first. The stats stream keeps one message per line.
"""

import asyncio
import unittest

from kowan.api import _dockerws
from kowan.api._dockerws import LineBatcher, _LineSplitter, stream_lines


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class TestLineBatcher(unittest.TestCase):
    def test_lines_accumulate_until_due(self):
        clk = _Clock()
        b = LineBatcher(max_bytes=1000, max_delay=0.05, clock=clk)
        self.assertEqual(b.add(b"one"), [])
        self.assertEqual(b.add(b"two"), [])
        self.assertFalse(b.due())
        self.assertAlmostEqual(b.time_left(), 0.05)
        clk.t = 0.049
        self.assertFalse(b.due())
        clk.t = 0.05
        self.assertTrue(b.due())
        self.assertEqual(b.flush(), b"one\ntwo")
        self.assertIsNone(b.flush())
        self.assertIsNone(b.time_left())

    def test_delay_counts_from_first_pending_line(self):
        clk = _Clock()
        b = LineBatcher(max_bytes=1000, max_delay=0.05, clock=clk)
        b.add(b"a")
        clk.t = 0.04
        b.add(b"b")
        clk.t = 0.05
        self.assertTrue(b.due())

    def test_size_cap_flushes_before_overflow(self):
        b = LineBatcher(max_bytes=10, max_delay=1, clock=_Clock())
        self.assertEqual(b.add(b"aaaa"), [])  # 4
        self.assertEqual(b.add(b"bbbb"), [])  # 4+1+4 = 9
        # +1+4 would be 14 > 10: the pending batch goes out first
        self.assertEqual(b.add(b"cccc"), [b"aaaa\nbbbb"])
        self.assertEqual(b.flush(), b"cccc")

    def test_reaching_cap_exactly_flushes(self):
        b = LineBatcher(max_bytes=9, max_delay=1, clock=_Clock())
        b.add(b"aaaa")
        self.assertEqual(b.add(b"bbbb"), [b"aaaa\nbbbb"])
        self.assertEqual(len(b), 0)

    def test_oversized_single_line_goes_alone(self):
        b = LineBatcher(max_bytes=10, max_delay=1, clock=_Clock())
        b.add(b"x")
        self.assertEqual(b.add(b"y" * 50), [b"x", b"y" * 50])
        self.assertIsNone(b.flush())

    def test_single_line_batch_is_the_old_format(self):
        b = LineBatcher(clock=_Clock())
        b.add(b"2024-01-01T00:00:00Z hello")
        self.assertEqual(b.flush(), b"2024-01-01T00:00:00Z hello")

    def test_empty_lines_keep_their_boundaries(self):
        b = LineBatcher(clock=_Clock())
        for line in (b"a", b"", b"c"):
            b.add(line)
        self.assertEqual(b.flush().split(b"\n"), [b"a", b"", b"c"])


class TestLineSplitter(unittest.TestCase):
    def test_partial_chunks_and_crlf(self):
        s = _LineSplitter()
        self.assertEqual(s.feed(b"one\r\ntw"), [b"one"])
        self.assertEqual(s.feed(b"o\nthree"), [b"two"])
        self.assertEqual(s.finish(), [b"three"])
        self.assertEqual(s.finish(), [])

    def test_overlong_line_is_flushed(self):
        s = _LineSplitter()
        big = b"x" * (_dockerws._MAX_LINE + 1)
        self.assertEqual(s.feed(big), [big])


class _FakeWS:
    def __init__(self):
        self.sent = []
        self.closed = False
        self.close_code = None

    async def send_str(self, s):
        self.sent.append(s)

    async def close(self, code=1000, message=b""):
        self.closed = True
        self.close_code = code

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.Event().wait()  # client never sends anything


class _ScriptedStream:
    """Yields scripted chunks; a float entry means 'sleep that long first'."""

    def __init__(self, script):
        self._script = list(script)
        self.closed = False

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for item in self._script:
            if isinstance(item, float):
                await asyncio.sleep(item)
                continue
            yield item

    async def close(self):
        self.closed = True


class TestStreamLinesPump(unittest.TestCase):
    def _pump(self, script, **kw):
        ws = _FakeWS()
        asyncio.run(stream_lines(ws, _ScriptedStream(script), None, **kw))
        return ws

    def test_burst_becomes_one_message(self):
        ws = self._pump([b"l1\nl2\n", b"l3\nl4\n", b"l5"], batch=True)
        self.assertEqual(ws.sent, ["l1\nl2\nl3\nl4\nl5"])
        self.assertEqual(ws.close_code, 1000)

    def test_quiet_gap_flushes_by_time(self):
        ws = self._pump([b"a\nb\n", 0.15, b"c\n"], batch=True, max_delay=0.02)
        self.assertEqual(ws.sent, ["a\nb", "c"])

    def test_size_cap_splits_batches(self):
        chunk = b"".join(b"%04d\n" % i for i in range(10))  # 10 lines x 4 bytes
        ws = self._pump([chunk], batch=True, max_bytes=14, max_delay=10.0)
        # 3 lines = 4+1+4+1+4 = 14 bytes per batch
        self.assertEqual(ws.sent[0], "0000\n0001\n0002")
        self.assertEqual("\n".join(ws.sent).split("\n"), ["%04d" % i for i in range(10)])
        for msg in ws.sent:
            self.assertLessEqual(len(msg.encode()), 14)

    def test_transform_applies_per_line(self):
        ws = _FakeWS()
        asyncio.run(
            stream_lines(ws, _ScriptedStream([b"a\nb\n"]), lambda l: l.upper(), batch=True)
        )
        self.assertEqual(ws.sent, ["A\nB"])

    def test_unbatched_mode_is_one_message_per_line(self):
        ws = self._pump([b'{"cpu":1}\n{"cpu":2}\n'])
        self.assertEqual(ws.sent, ['{"cpu":1}', '{"cpu":2}'])

    def test_client_gone_stops_without_close(self):
        class _GoneWS(_FakeWS):
            async def send_str(self, s):
                raise ConnectionResetError

        ws = _GoneWS()
        asyncio.run(stream_lines(ws, _ScriptedStream([b"a\n"]), None, batch=True))
        self.assertFalse(ws.closed)


if __name__ == "__main__":
    unittest.main()
