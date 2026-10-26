"""Read-only MCP resources: JSON snapshots an assistant can attach as context.

Resources reuse the read tools' code paths, so they never expose more than the
viewer-level tools do (every authenticated role may read them).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from .protocol import CallContext, RESOURCE_NOT_FOUND, RPCError
from .registry import ToolError, to_plain

_STATIC = [
    {"uri": "kowan://fleet/summary", "name": "fleet-summary", "title": "Fleet summary",
     "description": "Hosts online, container counts, crashed containers, open alerts, reclaimable disk.",
     "mimeType": "application/json"},
    {"uri": "kowan://hosts", "name": "hosts", "title": "Hosts",
     "description": "All managed Docker hosts with online status.", "mimeType": "application/json"},
    {"uri": "kowan://alerts/open", "name": "open-alerts", "title": "Open alerts",
     "description": "Unresolved monitoring alerts, newest first.", "mimeType": "application/json"},
    {"uri": "kowan://deployments", "name": "deployments", "title": "Deployments",
     "description": "Every deployment with status and target host.", "mimeType": "application/json"},
]

_TEMPLATES = [
    {"uriTemplate": "kowan://hosts/{host}/containers", "name": "host-containers", "title": "Containers on a host",
     "description": "All containers on one host (id or name).", "mimeType": "application/json"},
]


class ResourceCatalog:
    def __init__(self, tools: Any) -> None:
        self._tools = tools  # ToolRegistry: resources delegate to read tools

    def list(self) -> List[Dict[str, Any]]:
        return [dict(r) for r in _STATIC]

    def templates(self) -> List[Dict[str, Any]]:
        return [dict(t) for t in _TEMPLATES]

    async def read(self, uri: str, ctx: CallContext) -> Dict[str, Any]:
        if uri == "kowan://fleet/summary":
            data = await self._run("fleet_summary", {}, ctx)
        elif uri == "kowan://hosts":
            data = await self._run("list_hosts", {"limit": 500}, ctx)
        elif uri == "kowan://alerts/open":
            data = await self._run("list_alerts", {"status": "open", "limit": 200}, ctx)
        elif uri == "kowan://deployments":
            data = await self._run("list_deployments", {"limit": 500}, ctx)
        elif uri.startswith("kowan://hosts/") and uri.endswith("/containers"):
            host = uri[len("kowan://hosts/"): -len("/containers")]
            if not host or "/" in host:
                raise RPCError(RESOURCE_NOT_FOUND, "resource not found", {"uri": uri})
            data = await self._run("list_containers", {"host": host, "limit": 500}, ctx)
        else:
            raise RPCError(RESOURCE_NOT_FOUND, "resource not found", {"uri": uri})
        return {"contents": [{"uri": uri, "mimeType": "application/json",
                              "text": json.dumps(data, ensure_ascii=False, separators=(",", ":"))}]}

    async def _run(self, name: str, args: Dict[str, Any], ctx: CallContext) -> Any:
        t = self._tools.get(name)
        try:
            return to_plain(await t.handler(ctx, args))
        except ToolError as err:
            raise RPCError(RESOURCE_NOT_FOUND, str(err)) from err
