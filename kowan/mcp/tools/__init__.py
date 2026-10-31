"""The MCP tool catalog for Kowan (see docs/mcp.md for the full table)."""

from __future__ import annotations

from ..registry import ToolRegistry


def build_registry() -> ToolRegistry:
    from . import actions, admin, crashes, docker_read, fleet, history, operations, runbooks, workloads

    reg = ToolRegistry()
    docker_read.register(reg)
    fleet.register(reg)
    workloads.register(reg)
    history.register(reg)
    operations.register(reg)
    actions.register(reg)
    runbooks.register(reg)
    crashes.register(reg)
    admin.register(reg)
    return reg
