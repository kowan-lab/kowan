"""exec_attach reads the raw stream that follows Docker's ``101 UPGRADED``.

Regression: the upgraded bytes were read from the HTTP response body, which
aiohttp closes after a 101, so every interactive console ended immediately.
A fake Engine on a unix socket answers the exec start with 101 and then echoes.
"""

import asyncio
import json
import os
import tempfile
import unittest

from kowan.docker.local import LocalClient


async def _engine(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    while True:
        line = await reader.readline()
        if not line:
            break
        method, path, _ = line.decode().split(" ", 2)
        headers = {}
        while (h := await reader.readline()) not in (b"\r\n", b""):
            k, _, v = h.decode().partition(":")
            headers[k.strip().lower()] = v.strip()
        if int(headers.get("content-length", "0")):
            await reader.readexactly(int(headers["content-length"]))
        if path.endswith("/start"):
            writer.write(b"HTTP/1.1 101 UPGRADED\r\nContent-Type: application/vnd.docker.raw-stream\r\n"
                         b"Connection: Upgrade\r\nUpgrade: tcp\r\n\r\n")
            writer.write(b"$ ")
            await writer.drain()
            data = await reader.read(1024)
            writer.write(b"echo:" + data)
            await writer.drain()
            writer.close()
            return
        if "/exec" in path:
            body = json.dumps({"Id": "e1"}).encode()
            status = "201 Created"
        else:  # version negotiation and anything else
            body = json.dumps({"ApiVersion": "1.43", "MinAPIVersion": "1.24", "Version": "27.0.0"}).encode()
            status = "200 OK"
        writer.write(f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
        await writer.drain()


class ExecAttachTest(unittest.IsolatedAsyncioTestCase):
    async def test_reads_upgraded_stream(self) -> None:
        d = tempfile.mkdtemp()
        sock = os.path.join(d, "docker.sock")
        server = await asyncio.start_unix_server(_engine, path=sock)
        client = LocalClient(socket_path=sock)
        try:
            stream = await client.exec_attach("c1", ["/bin/sh"], True)
            self.assertEqual(await asyncio.wait_for(stream.read(64), 3), b"$ ")
            await stream.write(b"ls\n")
            out = b""
            while len(out) < len(b"echo:ls\n"):
                chunk = await asyncio.wait_for(stream.read(64), 3)
                if not chunk:
                    break
                out += chunk
            self.assertEqual(out, b"echo:ls\n")
            self.assertEqual(await asyncio.wait_for(stream.read(64), 3), b"")
            await stream.close()
        finally:
            close = getattr(client, "close", None)
            if close:
                await close()
            server.close()
            await server.wait_closed()


if __name__ == "__main__":
    unittest.main()
