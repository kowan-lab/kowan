"""Outbound HTTP responses read up to a limit.

``resp.content[:N]`` looks bounded but httpx has already downloaded the whole
body by then: a hostile feed, git server or webhook target could stream
gigabytes into the server's memory. These helpers stop reading at the limit.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx


@dataclass
class CappedResponse:
    status_code: int
    headers: httpx.Headers = field(default_factory=httpx.Headers)
    content: bytes = b""
    url: str = ""
    # True when the body was longer than the limit and was cut.
    truncated: bool = False

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")


async def request_capped(client: httpx.AsyncClient, method: str, url: str, limit: int, **kwargs) -> CappedResponse:
    """``client.request`` that keeps at most ``limit`` bytes of the body."""
    async with client.stream(method, url, **kwargs) as resp:
        body = bytearray()
        truncated = False
        async for chunk in resp.aiter_bytes():
            body.extend(chunk)
            if len(body) > limit:
                del body[limit:]
                truncated = True
                break
        return CappedResponse(resp.status_code, resp.headers, bytes(body), str(resp.url), truncated)
