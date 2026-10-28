"""JSON-RPC 2.0 framing + the MCP method dispatcher (transport-agnostic).

This module knows nothing about HTTP: it takes one decoded JSON-RPC message
plus a :class:`CallContext` (who is calling, from where, which session) and
returns the response object (or ``None`` for notifications/responses). The
Streamable HTTP transport in :mod:`kowan.mcp.transport` and the unit tests both
drive it directly.

Implemented MCP surface (spec revision 2025-06-18):

  * lifecycle: ``initialize`` / ``notifications/initialized`` / ``ping``
  * tools: ``tools/list`` / ``tools/call`` (role-filtered, see registry.py)
  * resources: ``resources/list`` / ``resources/templates/list`` / ``resources/read``
  * prompts: ``prompts/list`` / ``prompts/get``

Python note: the dispatcher is a plain ``dict`` from method name to an
``async def`` bound method, the asyncio analogue of a switch statement; every
handler raises :class:`RPCError` for protocol-level failures and the single
``handle`` entrypoint turns that into a JSON-RPC error object.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

log = logging.getLogger("kowan.mcp")

JSONRPC_VERSION = "2.0"

# Standard JSON-RPC 2.0 error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
# Implementation-defined (server error range): the caller's role may not use
# the requested tool.
FORBIDDEN = -32003
# MCP's conventional "resource not found" code.
RESOURCE_NOT_FOUND = -32002

# Protocol versions. We implement 2025-06-18 and accept the older revisions
# (our feature subset is wire-compatible with them) plus 2025-11-25, whose
# additions are all optional for a server like this one.
LATEST_PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
# Version to assume when an HTTP request carries no MCP-Protocol-Version header
# and the session has none recorded (spec: SHOULD assume 2025-03-26).
DEFAULT_ASSUMED_VERSION = "2025-03-26"

SERVER_NAME = "kowan"
SERVER_TITLE = "Kowan Docker manager"

INSTRUCTIONS = (
    "Kowan manages Docker hosts (a local daemon plus remote agents). Start with "
    "fleet_summary or list_workloads (deployments, compose stacks and lone "
    "containers, problems first), then drill in with get_workload, "
    "inspect_container and container_logs; search_logs searches the retained "
    "logs and workload_resources the CPU and memory history. The `host` argument accepts a host id "
    "or name and defaults to \"local\". Mutating tools are only listed when your "
    "role allows them; destructive tools additionally require `confirm: true`. "
    "Outputs are bounded: use `limit`/`offset` and `tail` to page."
)


class RPCError(Exception):
    """A protocol-level failure rendered as a JSON-RPC ``error`` object."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


@dataclass
class CallContext:
    """Everything a handler may need about the current call."""

    svc: Any
    user: Any  # kowan.auth.CtxUser (id / username / role)
    ip: str = ""
    session: Any = None  # kowan.mcp.sessions.Session, None during initialize
    protocol_version: str = LATEST_PROTOCOL_VERSION
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def role(self) -> str:
        return getattr(self.user, "role", "") or ""


# ---------------------------------------------------------------------------
# message helpers
# ---------------------------------------------------------------------------


def make_result(msg_id: Any, result: Any) -> dict:
    return {"jsonrpc": JSONRPC_VERSION, "id": msg_id, "result": result}


def make_error(msg_id: Any, code: int, message: str, data: Any = None) -> dict:
    err: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": JSONRPC_VERSION, "id": msg_id, "error": err}


def _valid_id(v: Any) -> bool:
    # JSON-RPC ids are strings or numbers; MCP forbids null. bool is an int
    # subclass in Python, so exclude it explicitly.
    return isinstance(v, (str, int, float)) and not isinstance(v, bool)


def classify(msg: Any) -> str:
    """Return "request", "notification", "response" or raise RPCError.

    Batches (JSON arrays) were removed from MCP in 2025-06-18, so an array is an
    invalid request rather than something to iterate.
    """
    if isinstance(msg, list):
        raise RPCError(INVALID_REQUEST, "batch requests are not supported")
    if not isinstance(msg, dict):
        raise RPCError(INVALID_REQUEST, "message must be a JSON object")
    if msg.get("jsonrpc") != JSONRPC_VERSION:
        raise RPCError(INVALID_REQUEST, 'jsonrpc must be "2.0"')
    if "method" in msg:
        if not isinstance(msg["method"], str) or not msg["method"]:
            raise RPCError(INVALID_REQUEST, "method must be a non-empty string")
        if "params" in msg and msg["params"] is not None and not isinstance(msg["params"], dict):
            raise RPCError(INVALID_REQUEST, "params must be an object")
        if "id" not in msg:
            return "notification"
        if not _valid_id(msg["id"]):
            raise RPCError(INVALID_REQUEST, "id must be a string or number")
        return "request"
    if "id" in msg and ("result" in msg or "error" in msg):
        return "response"
    raise RPCError(INVALID_REQUEST, "not a JSON-RPC request, notification or response")


def negotiate_version(requested: Any) -> str:
    """Echo the client's version when supported, else offer our latest."""
    if isinstance(requested, str) and requested in SUPPORTED_PROTOCOL_VERSIONS:
        return requested
    return LATEST_PROTOCOL_VERSION


def _server_version() -> str:
    from kowan import __version__

    return __version__


# ---------------------------------------------------------------------------
# dispatcher
# ---------------------------------------------------------------------------


class MCPServer:
    """Routes MCP methods to the tool / resource / prompt catalogs."""

    def __init__(self, tools: Any, resources: Any, prompts: Any) -> None:
        self.tools = tools
        self.resources = resources
        self.prompts = prompts
        self._methods = {
            "initialize": self._initialize,
            "ping": self._ping,
            "tools/list": self._tools_list,
            "tools/call": self._tools_call,
            "resources/list": self._resources_list,
            "resources/templates/list": self._resource_templates_list,
            "resources/read": self._resources_read,
            "prompts/list": self._prompts_list,
            "prompts/get": self._prompts_get,
        }

    async def handle(self, msg: Any, ctx: CallContext) -> Optional[dict]:
        """Handle one message; return the response dict or None (no reply)."""
        try:
            kind = classify(msg)
        except RPCError as err:
            msg_id = msg.get("id") if isinstance(msg, dict) and _valid_id(msg.get("id")) else None
            return make_error(msg_id, err.code, err.message, err.data)

        if kind == "response":
            # We never send server->client requests, so there is nothing to
            # correlate; accept and drop.
            return None
        if kind == "notification":
            await self._notification(msg, ctx)
            return None

        msg_id = msg["id"]
        method = msg["method"]
        params = msg.get("params") or {}
        fn = self._methods.get(method)
        if fn is None:
            return make_error(msg_id, METHOD_NOT_FOUND, f"method not found: {method}")
        try:
            result = await fn(params, ctx)
        except RPCError as err:
            return make_error(msg_id, err.code, err.message, err.data)
        except Exception:  # noqa: BLE001 - never leak a traceback to the client
            log.exception("mcp: internal error handling %s", method)
            return make_error(msg_id, INTERNAL_ERROR, "internal error")
        return make_result(msg_id, result)

    async def _notification(self, msg: dict, ctx: CallContext) -> None:
        method = msg["method"]
        if method == "notifications/initialized":
            if ctx.session is not None:
                ctx.session.initialized = True
        # notifications/cancelled and anything else: nothing in flight to
        # cancel (every request completes within its own HTTP exchange).

    # -- lifecycle -----------------------------------------------------------

    async def _initialize(self, params: dict, ctx: CallContext) -> dict:
        requested = params.get("protocolVersion")
        if not isinstance(requested, str) or not requested:
            raise RPCError(INVALID_PARAMS, "protocolVersion is required")
        version = negotiate_version(requested)
        ctx.protocol_version = version
        ctx.extra["client_info"] = params.get("clientInfo") if isinstance(params.get("clientInfo"), dict) else {}
        return {
            "protocolVersion": version,
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"subscribe": False, "listChanged": False},
                "prompts": {"listChanged": False},
            },
            "serverInfo": {
                "name": SERVER_NAME,
                "title": SERVER_TITLE,
                "version": _server_version(),
            },
            "instructions": INSTRUCTIONS,
        }

    async def _ping(self, params: dict, ctx: CallContext) -> dict:
        return {}

    # -- tools ---------------------------------------------------------------

    async def _tools_list(self, params: dict, ctx: CallContext) -> dict:
        _check_cursor(params)
        return {"tools": [t.descriptor() for t in self.tools.visible(ctx.role)]}

    async def _tools_call(self, params: dict, ctx: CallContext) -> dict:
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise RPCError(INVALID_PARAMS, "name is required")
        args = params.get("arguments")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise RPCError(INVALID_PARAMS, "arguments must be an object")
        return await self.tools.call(name, args, ctx)

    # -- resources -----------------------------------------------------------

    async def _resources_list(self, params: dict, ctx: CallContext) -> dict:
        _check_cursor(params)
        return {"resources": self.resources.list()}

    async def _resource_templates_list(self, params: dict, ctx: CallContext) -> dict:
        _check_cursor(params)
        return {"resourceTemplates": self.resources.templates()}

    async def _resources_read(self, params: dict, ctx: CallContext) -> dict:
        uri = params.get("uri")
        if not isinstance(uri, str) or not uri:
            raise RPCError(INVALID_PARAMS, "uri is required")
        return await self.resources.read(uri, ctx)

    # -- prompts -------------------------------------------------------------

    async def _prompts_list(self, params: dict, ctx: CallContext) -> dict:
        _check_cursor(params)
        return {"prompts": self.prompts.list()}

    async def _prompts_get(self, params: dict, ctx: CallContext) -> dict:
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise RPCError(INVALID_PARAMS, "name is required")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            raise RPCError(INVALID_PARAMS, "arguments must be an object")
        return self.prompts.get(name, args)


def _check_cursor(params: dict) -> None:
    # Every catalog fits in one page, so we never hand out a cursor; any cursor
    # presented is therefore not one of ours.
    cursor = params.get("cursor")
    if cursor not in (None, ""):
        raise RPCError(INVALID_PARAMS, "invalid cursor")
