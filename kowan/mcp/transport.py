"""Streamable HTTP transport for MCP at ``/mcp`` (JSON response mode).

* ``POST /mcp``   one JSON-RPC message per request. Requests get a single JSON
  object back (``application/json``), or one SSE ``message`` event when the
  client accepts only ``text/event-stream``. Notifications and responses get
  ``202 Accepted`` with no body.
* ``GET /mcp``    ``405`` — we do not offer a server-initiated SSE stream.
* ``DELETE /mcp`` ends the session named by ``Mcp-Session-Id`` (``204``).

Checks, in order: Origin (DNS-rebinding guard, ``403``), bearer auth through
Kowan's own auth middleware (API key or login JWT, ``401``), content type
(``415``), Accept (``406``), JSON parse (``400`` + parse error),
``MCP-Protocol-Version`` (``400`` if unsupported) and the session header
(``400`` missing, ``404`` unknown/expired/owned by someone else).

The route lives outside ``/api/v1``, so the app's scoped auth/audit middleware
does not run here; this module invokes the same ``auth_middleware`` itself and
the tool registry writes its own audit entries.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional, Tuple
from urllib.parse import urlsplit

from aiohttp import web

from ..auth.middleware import REQUEST_USER_KEY, auth_middleware
from ..auth.sessions import make_session_checker
from ..web.audit import client_ip
from .protocol import (
    DEFAULT_ASSUMED_VERSION,
    INVALID_REQUEST,
    PARSE_ERROR,
    SUPPORTED_PROTOCOL_VERSIONS,
    CallContext,
    MCPServer,
    RPCError,
    classify,
    make_error,
)
from .sessions import SessionStore

log = logging.getLogger("kowan.mcp.http")

MCP_PATH = "/mcp"
SESSION_HEADER = "Mcp-Session-Id"
VERSION_HEADER = "MCP-Protocol-Version"
# Comma-separated extra origins allowed to call /mcp from a browser.
ALLOWED_ORIGINS_ENV = "KOWAN_MCP_ALLOWED_ORIGINS"

_LOOPBACK = {"localhost", "127.0.0.1", "::1", "[::1]"}


def _http_error(status: int, message: str, code: int = -32000, headers: Optional[dict] = None) -> web.Response:
    body = json.dumps(make_error(None, code, message), separators=(",", ":"))
    return web.Response(status=status, text=body, content_type="application/json", headers=headers)


def _hostname(netloc: str) -> str:
    try:
        return (urlsplit("//" + netloc).hostname or "").lower()
    except ValueError:
        return ""


def origin_allowed(request: web.Request, external_url: str = "") -> bool:
    """Allow requests without Origin (non-browser clients) and same-origin ones.

    Same-origin means the Origin's host:port equals the Host the request was
    sent to (or the first X-Forwarded-Host behind a proxy), the configured
    KOWAN_EXTERNAL_URL, an entry of KOWAN_MCP_ALLOWED_ORIGINS, or both sides are
    loopback (the Vite dev proxy on :5173 talking to :5081). Bearer auth is the
    primary defence; this stops a rebinding page from even reaching it.
    """
    origin = request.headers.get("Origin")
    if origin is None or origin == "":
        return True
    if origin == "null":
        return False
    try:
        o = urlsplit(origin)
    except ValueError:
        return False
    if o.scheme not in ("http", "https") or not o.netloc:
        return False
    o_netloc = o.netloc.lower()
    host = (request.host or "").lower()
    if o_netloc == host:
        return True
    xfh = request.headers.get("X-Forwarded-Host", "").split(",")[0].strip().lower()
    if xfh and o_netloc == xfh:
        return True
    norm = f"{o.scheme}://{o_netloc}"
    allowed = [external_url] + os.environ.get(ALLOWED_ORIGINS_ENV, "").split(",")
    for a in allowed:
        a = (a or "").strip().rstrip("/")
        if not a:
            continue
        try:
            p = urlsplit(a)
        except ValueError:
            continue
        if f"{p.scheme}://{p.netloc.lower()}" == norm:
            return True
    if (o.hostname or "").lower() in _LOOPBACK and _hostname(host) in _LOOPBACK:
        return True
    return False


def _media_types(header: str) -> set:
    return {part.split(";", 1)[0].strip().lower() for part in header.split(",") if part.strip()}


def _negotiate_accept(request: web.Request) -> Tuple[bool, bool]:
    """Return (use_json, use_sse) for a request response; both False => 406."""
    raw = request.headers.get("Accept")
    if raw is None or raw.strip() == "":
        return True, False  # lenient: plain clients like curl without -H Accept
    types = _media_types(raw)
    if types & {"application/json", "application/*", "*/*"}:
        return True, False
    if "text/event-stream" in types:
        return False, True
    return False, False


def _principal(user: Any) -> Tuple[str, str]:
    return (getattr(user, "id", "") or "", getattr(user, "username", "") or "")


class MCPHttpHandler:
    def __init__(self, svc: Any, server: MCPServer, sessions: Optional[SessionStore] = None) -> None:
        self.svc = svc
        self.server = server
        self.sessions = sessions or SessionStore()
        sessions = make_session_checker(svc.store) if getattr(svc, "store", None) is not None else None
        self._auth = auth_middleware(svc.jwt, svc.keys, sessions) if svc.jwt is not None else None

    # -- shared guards -------------------------------------------------------

    def _external_url(self) -> str:
        return getattr(getattr(self.svc, "config", None), "external_url", "") or ""

    async def _authed(self, request: web.Request, inner) -> web.StreamResponse:
        if not origin_allowed(request, self._external_url()):
            return _http_error(403, "Forbidden: origin not allowed")
        if self._auth is None:
            return _http_error(401, "authentication unavailable", headers={"WWW-Authenticate": "Bearer"})

        async def wrapped(req: web.Request) -> web.StreamResponse:
            return await inner(req)

        resp = await self._auth(request, wrapped)
        if resp.status == 401 and request.get(REQUEST_USER_KEY) is None:
            resp.headers["WWW-Authenticate"] = 'Bearer realm="kowan"'
        return resp

    def _session_for(self, request: web.Request) -> Tuple[Any, Optional[web.Response]]:
        sid = request.headers.get(SESSION_HEADER, "").strip()
        if not sid:
            return None, _http_error(400, "Bad Request: missing Mcp-Session-Id header (initialize first)")
        s = self.sessions.get(sid, _principal(request[REQUEST_USER_KEY]))
        if s is None:
            return None, _http_error(404, "Not Found: unknown or expired session; initialize a new one")
        return s, None

    # -- routes --------------------------------------------------------------

    async def get(self, request: web.Request) -> web.StreamResponse:
        return web.Response(status=405, headers={"Allow": "POST, DELETE"}, text="Method Not Allowed")

    async def delete(self, request: web.Request) -> web.StreamResponse:
        async def inner(req: web.Request) -> web.StreamResponse:
            sid = req.headers.get(SESSION_HEADER, "").strip()
            if not sid:
                return _http_error(400, "Bad Request: missing Mcp-Session-Id header")
            if not self.sessions.delete(sid, _principal(req[REQUEST_USER_KEY])):
                return _http_error(404, "Not Found: unknown or expired session")
            return web.Response(status=204)

        return await self._authed(request, inner)

    async def post(self, request: web.Request) -> web.StreamResponse:
        return await self._authed(request, self._post)

    async def _post(self, request: web.Request) -> web.StreamResponse:
        ctype = request.headers.get("Content-Type", "")
        if ctype.split(";", 1)[0].strip().lower() != "application/json":
            return _http_error(415, "Unsupported Media Type: Content-Type must be application/json")
        use_json, use_sse = _negotiate_accept(request)
        if not use_json and not use_sse:
            return _http_error(406, "Not Acceptable: accept application/json or text/event-stream")

        raw = await request.read()
        try:
            msg = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return _http_error(400, "Parse error: body is not valid JSON", PARSE_ERROR)
        try:
            kind = classify(msg)
        except RPCError as err:
            return _http_error(400, err.message, INVALID_REQUEST)

        version_hdr = request.headers.get(VERSION_HEADER, "").strip()
        if version_hdr and version_hdr not in SUPPORTED_PROTOCOL_VERSIONS:
            return _http_error(400, f"Bad Request: unsupported MCP-Protocol-Version {version_hdr!r}")

        user = request[REQUEST_USER_KEY]
        ctx = CallContext(svc=self.svc, user=user, ip=client_ip(request))
        is_init = kind == "request" and msg.get("method") == "initialize"
        if not is_init:
            session, err_resp = self._session_for(request)
            if err_resp is not None:
                return err_resp
            ctx.session = session
            ctx.protocol_version = version_hdr or session.protocol_version or DEFAULT_ASSUMED_VERSION

        reply = await self.server.handle(msg, ctx)
        if kind != "request":
            return web.Response(status=202)

        headers = {}
        if is_init and reply is not None and "result" in reply:
            info = ctx.extra.get("client_info") or {}
            s = self.sessions.create(_principal(user), ctx.protocol_version, str(info.get("name", ""))[:100])
            headers[SESSION_HEADER] = s.id
            log.info("mcp session opened user=%s client=%s version=%s", getattr(user, "username", ""),
                     s.client_name, s.protocol_version)
        body = json.dumps(reply, ensure_ascii=False, separators=(",", ":"), default=str)
        if use_json:
            return web.Response(status=200, text=body, content_type="application/json", headers=headers)
        headers["Cache-Control"] = "no-cache"
        return web.Response(status=200, text=f"event: message\ndata: {body}\n\n",
                            content_type="text/event-stream", headers=headers)
