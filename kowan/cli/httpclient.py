"""Synchronous HTTP for kowanctl, on the standard library (``http.client``).

The CLI is plain blocking code, so it does not need aiohttp's event loop:
:class:`Session` opens one connection per request, with a connect timeout and
a separate read timeout (``kowanctl mcp`` waits up to 15 minutes for a tool
but only 10 seconds to connect). TLS certificates are not verified, so the CLI
works against a server using an auto-generated, self-signed certificate.

Bodies are read up to a limit (``stream=True`` hands back the open response to
copy a download chunk by chunk); request bodies may be an iterable of chunks,
sent with chunked encoding unless a ``Content-Length`` is given.

Test seam: ``Session(handler=fn)`` sends every request to ``fn(Request)``,
which returns a :class:`Reply` (or raises :class:`HTTPError`) instead of
touching the network.
"""

from __future__ import annotations

import http.client
import json as _json
import ssl
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, Iterator, Mapping, Optional, Tuple, Union
from urllib.parse import urlsplit

# Default cap on a response body read in full (JSON answers from the server).
DEFAULT_LIMIT = 64 << 20

_CHUNK = 64 << 10
_UNSET: Any = object()

Body = Union[bytes, Iterable[bytes], None]


class HTTPError(Exception):
    """No answer: bad URL, DNS, connect, TLS, timeout or a broken response."""


class Headers:
    """A small case-insensitive header map (keeps the first spelling)."""

    def __init__(self, items: Union[Mapping[str, str], Iterable[Tuple[str, str]], None] = None) -> None:
        self._d: Dict[str, Tuple[str, str]] = {}
        pairs = items.items() if isinstance(items, Mapping) else (items or [])
        for k, v in pairs:
            self[k] = v

    def __setitem__(self, key: str, value: str) -> None:
        self._d[key.lower()] = (key, str(value))

    def __getitem__(self, key: str) -> str:
        return self._d[key.lower()][1]

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and key.lower() in self._d

    def get(self, key: str, default: str = "") -> str:
        hit = self._d.get(key.lower())
        return hit[1] if hit else default

    def items(self) -> Iterator[Tuple[str, str]]:
        return iter(self._d.values())


@dataclass
class Request:
    """What a test handler sees."""

    method: str
    url: str
    headers: Headers
    content: bytes = b""

    def json(self) -> Any:
        return _json.loads(self.content)


@dataclass
class Reply:
    """A fake answer returned by a test handler."""

    status: int = 200
    json: Any = _UNSET
    text: Optional[str] = None
    content: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)


class Response:
    """An answer. Without ``stream`` the body is already read (up to the limit);
    with it, read it through :meth:`iter_bytes` / :meth:`read` and close it
    (it is a context manager)."""

    def __init__(self, status: int, reason: str, headers: Headers, url: str,
                 raw: Optional[http.client.HTTPResponse] = None,
                 conn: Optional[http.client.HTTPConnection] = None, content: bytes = b"") -> None:
        self.status_code = status
        self.reason_phrase = reason
        self.headers = headers
        self.url = url
        self.truncated = False
        self._raw = raw
        self._conn = conn
        self._content: Optional[bytes] = None if raw is not None else content

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        self._raw = None

    def iter_bytes(self, chunk: int = _CHUNK) -> Iterator[bytes]:
        if self._raw is None:
            if self._content:
                yield self._content
            return
        try:
            while True:
                data = self._raw.read(chunk)
                if not data:
                    return
                yield data
        except (OSError, http.client.HTTPException) as err:
            raise HTTPError(str(err) or type(err).__name__) from err

    def read(self, limit: int = DEFAULT_LIMIT) -> bytes:
        """The body (at most ``limit`` bytes; ``truncated`` says if it was cut)."""
        if self._content is None:
            buf = bytearray()
            for data in self.iter_bytes():
                buf.extend(data)
                if len(buf) > limit:
                    del buf[limit:]
                    self.truncated = True
                    break
            self._content = bytes(buf)
            self.close()
        return self._content

    @property
    def content(self) -> bytes:
        return self.read()

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")

    def json(self) -> Any:
        return _json.loads(self.content)

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300


Handler = Callable[[Request], Reply]


def _insecure_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class Session:
    """Blocking requests sharing timeouts. ``timeout`` bounds each socket read
    (and the connect, unless ``connect_timeout`` is given)."""

    def __init__(self, *, timeout: float = 60.0, connect_timeout: Optional[float] = None,
                 handler: Optional[Handler] = None) -> None:
        self.timeout = timeout
        self.connect_timeout = timeout if connect_timeout is None else connect_timeout
        self._handler = handler
        self._ssl: Optional[ssl.SSLContext] = None

    def close(self) -> None:
        """Nothing is pooled; kept so callers can close it like any client."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        json: Any = _UNSET,
        content: Body = None,
        stream: bool = False,
        limit: int = DEFAULT_LIMIT,
    ) -> Response:
        hdrs = Headers(headers)
        if json is not _UNSET:
            content = _json.dumps(json, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if "Content-Type" not in hdrs:
                hdrs["Content-Type"] = "application/json"
        if self._handler is not None:
            return self._fake(method, url, hdrs, content, stream, limit)
        resp = self._send(method, url, hdrs, content)
        if not stream:
            resp.read(limit)
        return resp

    def _fake(self, method: str, url: str, headers: Headers, content: Body, stream: bool, limit: int) -> Response:
        assert self._handler is not None
        if content is None:
            body = b""
        elif isinstance(content, (bytes, bytearray)):
            body = bytes(content)
        else:
            body = b"".join(content)
        reply = self._handler(Request(method.upper(), url, headers, body))
        rh = Headers(reply.headers)
        if reply.json is not _UNSET:
            data = _json.dumps(reply.json).encode("utf-8")
            if "Content-Type" not in rh:
                rh["Content-Type"] = "application/json"
        elif reply.text is not None:
            data = reply.text.encode("utf-8")
        else:
            data = reply.content
        resp = Response(reply.status, http.client.responses.get(reply.status, ""), rh, url, content=data[:limit])
        resp.truncated = len(data) > limit
        return resp

    def _send(self, method: str, url: str, headers: Headers, content: Body) -> Response:
        u = urlsplit(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise HTTPError(f"invalid URL {url!r}")
        target = (u.path or "/") + ("?" + u.query if u.query else "")
        conn: http.client.HTTPConnection
        try:
            if u.scheme == "https":
                if self._ssl is None:
                    self._ssl = _insecure_context()
                conn = http.client.HTTPSConnection(u.hostname, u.port, timeout=self.connect_timeout,
                                                   context=self._ssl)
            else:
                conn = http.client.HTTPConnection(u.hostname, u.port, timeout=self.connect_timeout)
        except ValueError as err:  # bad port
            raise HTTPError(f"invalid URL {url!r}: {err}") from err
        try:
            conn.connect()
            if conn.sock is not None:
                conn.sock.settimeout(self.timeout)
            conn.request(method.upper(), target, body=content, headers=dict(headers.items()))
            raw = conn.getresponse()
        except (OSError, http.client.HTTPException) as err:
            conn.close()
            raise HTTPError(str(err) or type(err).__name__) from err
        return Response(raw.status, raw.reason, Headers(raw.getheaders()), url, raw=raw, conn=conn)


__all__ = ["DEFAULT_LIMIT", "HTTPError", "Headers", "Reply", "Request", "Response", "Session"]
