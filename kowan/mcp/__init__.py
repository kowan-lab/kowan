"""Model Context Protocol (MCP) server for Kowan.

A dependency-free implementation of MCP 2025-06-18 over Streamable HTTP at
``/mcp``: JSON-RPC 2.0 framing (protocol.py), sessions (sessions.py), the
role-gated tool catalog (registry.py + tools/), read-only resources
(resources.py) and prompts (prompts.py). It reuses Kowan's auth (API keys and
login JWTs), service layer and Docker ``Ops``, so tools behave exactly like
the REST API. See docs/mcp.md.

``register(app, svc)`` follows the api-module convention used by
``kowan/server.py::_registrars()``.
"""

from __future__ import annotations

from typing import Any

from aiohttp import web

from .protocol import MCPServer
from .prompts import PromptCatalog
from .resources import ResourceCatalog
from .tools import build_registry
from .transport import MCP_PATH, MCPHttpHandler


def build_server() -> MCPServer:
    tools = build_registry()
    return MCPServer(tools, ResourceCatalog(tools), PromptCatalog())


def register(app: web.Application, svc: Any) -> None:
    handler = MCPHttpHandler(svc, build_server())
    svc.attach("mcp", handler)
    app.router.add_route("POST", MCP_PATH, handler.post)
    app.router.add_route("GET", MCP_PATH, handler.get)
    app.router.add_route("DELETE", MCP_PATH, handler.delete)


__all__ = ["register", "build_server", "MCP_PATH"]
