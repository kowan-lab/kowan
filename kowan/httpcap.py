"""Outbound HTTP for the server and agent, with every response body bounded.

All outbound calls (registries, triggers, notifications, secrets back-ends,
OIDC, uptime probes, workflow HTTP steps, the Portainer importer) go through
:class:`Client` / :func:`request`, built on aiohttp's client. The body is read
chunk by chunk and reading stops at the caller's ``limit``: a hostile feed, git
server or webhook target cannot stream gigabytes into the server's memory.
``Response.truncated`` says whether the body was cut.

Test seam: :func:`mock` installs a handler that receives a :class:`Request` and
returns a :class:`Reply` (or raises :class:`HTTPError`), in place of the network,
for every client; ``Client(handler=...)`` does the same for one client.
It sees exactly the method, URL (query included), headers and body bytes the
real path would send, and redirects are followed the same way.

Why aiohttp and not a separate HTTP library: it is already the server's web
framework, so its client costs no extra dependency.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import inspect
import json as _json
from dataclasses import dataclass, field
from typing import Any, AsyncIterable, Awaitable, Callable, Dict, Iterator, Mapping, Optional, Tuple, Union
from urllib.parse import parse_qs, urlencode

import aiohttp
from multidict import CIMultiDict
from yarl import URL

# Reasonable cap for a JSON API answer (token endpoints, cloud APIs, secrets).
JSON_LIMIT = 4 << 20

_CHUNK = 64 << 10
_MAX_REDIRECTS = 20
_REDIRECTS = (301, 302, 303, 307, 308)
_UNSET: Any = object()


class HTTPError(Exception):
    """The request did not get an answer: DNS, connect, TLS, protocol, too many
    redirects or a bad URL. An HTTP error *status* is not an exception."""


class Timeout(HTTPError):
    """The request did not complete within the client's timeout."""


@dataclass
class Response:
    status_code: int
    headers: CIMultiDict = field(default_factory=CIMultiDict)
    content: bytes = b""
    # Final URL, after any followed redirect.
    url: str = ""
    # True when the body was longer than the limit and was cut.
    truncated: bool = False

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")

    def json(self) -> Any:
        """Decode the body as JSON; raises ``ValueError`` when it is not."""
        return _json.loads(self.content)

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300


@dataclass
class Request:
    """What a :func:`mock` handler sees."""

    method: str
    url: URL
    headers: CIMultiDict
    content: bytes = b""

    def json(self) -> Any:
        return _json.loads(self.content)

    def form(self) -> Dict[str, list]:
        return parse_qs(self.content.decode("utf-8"), keep_blank_values=True)


@dataclass
class Reply:
    """A fake answer for :func:`mock` handlers. ``content`` may also be an async
    iterable of chunks (to prove the reader stops at the limit)."""

    status: int = 200
    json: Any = _UNSET
    text: Optional[str] = None
    content: Union[bytes, AsyncIterable[bytes]] = b""
    headers: Mapping[str, str] = field(default_factory=dict)

    def _headers(self) -> CIMultiDict:
        h = CIMultiDict(self.headers)
        if self.json is not _UNSET:
            h.setdefault("Content-Type", "application/json")
        elif self.text is not None:
            h.setdefault("Content-Type", "text/plain; charset=utf-8")
        return h

    async def _chunks(self) -> AsyncIterable[bytes]:
        if self.json is not _UNSET:
            yield _json.dumps(self.json).encode("utf-8")
        elif self.text is not None:
            yield self.text.encode("utf-8")
        elif isinstance(self.content, (bytes, bytearray)):
            yield bytes(self.content)
        else:
            async for chunk in self.content:
                yield chunk


Handler = Callable[[Request], Union[Reply, Awaitable[Reply]]]
_handler: Optional[Handler] = None


@contextlib.contextmanager
def mock(handler: Handler) -> Iterator[None]:
    """Route every outbound request to ``handler`` for the duration."""
    global _handler
    prev, _handler = _handler, handler
    try:
        yield
    finally:
        _handler = prev


async def _read_capped(chunks: AsyncIterable[bytes], limit: int) -> Tuple[bytes, bool]:
    body = bytearray()
    async for chunk in chunks:
        body.extend(chunk)
        if len(body) > limit:
            del body[limit:]
            return bytes(body), True
    return bytes(body), False


def basic_auth(username: str, password: str) -> str:
    raw = f"{username}:{password}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


class Client:
    """A session with shared options; use as ``async with Client(...) as c``.

    ``timeout`` bounds each whole request (connect + headers + capped body);
    ``verify=False`` skips TLS certificate verification; ``follow_redirects``
    is the default for :meth:`request`. ``handler`` replaces the network for
    this client (tests; see :func:`mock`).
    """

    def __init__(self, *, timeout: float = 30.0, verify: bool = True, follow_redirects: bool = False,
                 headers: Optional[Mapping[str, str]] = None, handler: Optional[Handler] = None) -> None:
        self._handler = handler
        self._timeout = timeout
        self._verify = verify
        self._follow = follow_redirects
        self._headers = dict(headers or {})
        self._session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self) -> "Client":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._timeout),
                connector=aiohttp.TCPConnector(ssl=self._verify),
                trust_env=True,  # honour HTTP(S)_PROXY / NO_PROXY
            )
        return self._session

    async def request(
        self,
        method: str,
        url: str,
        *,
        limit: int,
        headers: Optional[Mapping[str, str]] = None,
        params: Optional[Mapping[str, Any]] = None,
        json: Any = _UNSET,
        data: Optional[Mapping[str, Any]] = None,
        content: Optional[bytes] = None,
        auth: Optional[Tuple[str, str]] = None,
        follow_redirects: Optional[bool] = None,
    ) -> Response:
        """Send one request and keep at most ``limit`` bytes of the body.

        ``json`` sends a compact JSON body, ``data`` an urlencoded form,
        ``content`` raw bytes; ``auth`` is HTTP Basic. Raises :class:`HTTPError`
        (or :class:`Timeout`) when there is no answer.
        """
        method = method.upper()
        try:
            target = URL(url)
        except (ValueError, TypeError) as err:
            raise HTTPError(f"invalid URL {url!r}: {err}") from None
        if params:
            target = target.update_query({k: str(v) for k, v in params.items()})
        hdrs = CIMultiDict(self._headers)
        hdrs.update(headers or {})
        body = b""
        if json is not _UNSET:
            body = _json.dumps(json, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        elif data is not None:
            body = urlencode(data, doseq=True).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
        elif content is not None:
            body = bytes(content)
        if auth is not None:
            hdrs["Authorization"] = basic_auth(*auth)
        follow = self._follow if follow_redirects is None else follow_redirects
        handler = self._handler or _handler
        if handler is not None:
            return await self._fake(handler, method, target, hdrs, body, limit, follow)
        return await self._real(method, target, hdrs, body, limit, follow)

    async def _real(self, method: str, url: URL, headers: CIMultiDict, body: bytes, limit: int,
                    follow: bool) -> Response:
        try:
            async with self._get_session().request(
                method, url, headers=headers, data=body or None,
                allow_redirects=follow, max_redirects=_MAX_REDIRECTS,
            ) as resp:
                content, truncated = await _read_capped(resp.content.iter_chunked(_CHUNK), limit)
                return Response(resp.status, CIMultiDict(resp.headers), content, str(resp.url), truncated)
        except asyncio.TimeoutError:
            raise Timeout(f"timed out after {self._timeout:g}s") from None
        except aiohttp.ClientError as err:
            raise HTTPError(str(err) or type(err).__name__) from err
        except ValueError as err:  # malformed URL / header value
            raise HTTPError(str(err)) from err

    async def _fake(self, handler: Handler, method: str, url: URL, headers: CIMultiDict, body: bytes,
                    limit: int, follow: bool) -> Response:
        for _ in range(_MAX_REDIRECTS + 1):
            out = handler(Request(method, url, headers, body))
            reply: Reply = await out if inspect.isawaitable(out) else out  # type: ignore[assignment]
            location = reply._headers().get("Location", "")
            if not (follow and reply.status in _REDIRECTS and location):
                content, truncated = await _read_capped(reply._chunks(), limit)
                return Response(reply.status, reply._headers(), content, str(url), truncated)
            nxt = url.join(URL(location))
            if nxt.origin() != url.origin():
                headers = CIMultiDict((k, v) for k, v in headers.items() if k.lower() != "authorization")
            if reply.status == 303 or (reply.status in (301, 302) and method == "POST"):
                method, body = "GET", b""
            url = nxt
        raise HTTPError("too many redirects")


async def request(method: str, url: str, *, limit: int, timeout: float = 30.0, verify: bool = True,
                  follow_redirects: bool = False, **kwargs: Any) -> Response:
    """One request on a one-off :class:`Client`; see :meth:`Client.request`."""
    async with Client(timeout=timeout, verify=verify, follow_redirects=follow_redirects) as client:
        return await client.request(method, url, limit=limit, **kwargs)


__all__ = ["Client", "HTTPError", "JSON_LIMIT", "Reply", "Request", "Response", "Timeout", "basic_auth",
           "mock", "request"]
