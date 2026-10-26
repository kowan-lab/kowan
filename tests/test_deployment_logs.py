"""Deployment log streaming sends TEXT frames (regression guard).

The browser log viewer only parses string WebSocket messages; a binary frame
renders nothing and the tab sticks on "Waiting for log output…". The deployment
logs handler must therefore stream lines as text — which it does by reusing the
shared ``stream_lines`` pump (the same one behind the container-logs endpoint)
with ``strip_docker_log_header``. This test drives that exact pump with a fake
socket + stream and asserts: frames arrive as ``send_str`` (text), and Docker's
8-byte stream-multiplex header is stripped.
"""

import asyncio
import unittest

from kowan.api._dockerws import stream_lines, strip_docker_log_header


class _FakeStream:
    """Minimal AsyncByteStream: yields the given chunks, then EOF."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.closed = False

    def __aiter__(self):
        async def _gen():
            for c in self._chunks:
                yield c

        return _gen()

    async def close(self) -> None:
        self.closed = True


class _FakeWS:
    """Records outbound frames and their kind (str vs bytes)."""

    def __init__(self) -> None:
        self.str_frames: list[str] = []
        self.bytes_frames: list[bytes] = []
        self.closed = False

    def __aiter__(self):
        async def _gen():
            # No inbound frames; the client never sends anything.
            if False:  # pragma: no cover
                yield None

        return _gen()

    async def send_str(self, s: str) -> None:
        self.str_frames.append(s)

    async def send_bytes(self, b: bytes) -> None:  # pragma: no cover
        self.bytes_frames.append(b)

    async def close(self, code: int = 1000) -> None:
        self.closed = True


def _docker_frame(payload: bytes, stream_byte: int = 1) -> bytes:
    """Wrap ``payload`` in Docker's 8-byte multiplex header + trailing newline."""
    size = len(payload).to_bytes(4, "big")
    return bytes([stream_byte, 0, 0, 0]) + size + payload + b"\n"


class TestDeploymentLogsPump(unittest.TestCase):
    def test_frames_are_text_and_header_is_stripped(self):
        stream = _FakeStream(
            [_docker_frame(b"hello"), _docker_frame(b"world", stream_byte=2)]
        )
        ws = _FakeWS()

        asyncio.run(stream_lines(ws, stream, strip_docker_log_header))

        # Text frames only — a binary frame here is exactly the bug we fixed.
        self.assertEqual(ws.str_frames, ["hello", "world"])
        self.assertEqual(ws.bytes_frames, [])
        # The stream is closed with a normal-closure at EOF.
        self.assertTrue(ws.closed)

    def test_plain_lines_without_header_pass_through(self):
        stream = _FakeStream([b"line-a\nline-b\n"])
        ws = _FakeWS()

        asyncio.run(stream_lines(ws, stream, strip_docker_log_header))

        self.assertEqual(ws.str_frames, ["line-a", "line-b"])
        self.assertEqual(ws.bytes_frames, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
