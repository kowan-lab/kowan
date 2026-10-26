"""Shared helpers for the MCP tools: host/ops resolution, timeouts, paging,
time parsing and small formatting utilities."""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ...auth import authorize as authz
from ..registry import ToolError

DEFAULT_HOST = "local"
READ_TIMEOUT = 30.0
PING_TIMEOUT = 3.0

# Common schema fragments ----------------------------------------------------

HOST_PROP = {
    "type": "string",
    "description": 'Host id or name (default "local").',
    "minLength": 1,
}
CONTAINER_PROP = {
    "type": "string",
    "description": "Container id (or unique prefix) or name.",
    "minLength": 1,
}
DEPLOYMENT_PROP = {"type": "string", "description": "Deployment id or name.", "minLength": 1}


def limit_prop(default: int, maximum: int) -> Dict[str, Any]:
    return {
        "type": "integer",
        "minimum": 1,
        "maximum": maximum,
        "description": f"Maximum items to return (default {default}, max {maximum}).",
    }


OFFSET_PROP = {"type": "integer", "minimum": 0, "description": "Items to skip (for paging)."}


# Hosts ----------------------------------------------------------------------


def resolve_host(svc: Any, ref: Optional[str]) -> Any:
    """Find a host by id, then by (case-insensitive) name."""
    ref = (ref or DEFAULT_HOST).strip() or DEFAULT_HOST
    try:
        host = svc.store.get_host(ref)
        if authz.host_visible(host.id):  # team/grant scoping
            return host
    except Exception:  # noqa: BLE001 - NotFoundError: fall back to name match
        pass
    hosts = authz.filter_hosts(svc.store.list_hosts())
    matches = [h for h in hosts if (h.name or "").lower() == ref.lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ToolError(f"host name {ref!r} is ambiguous; use the host id ({', '.join(h.id for h in matches)})")
    raise ToolError(f"host {ref!r} not found; call list_hosts to see available hosts")


def ops_for_host(svc: Any, host: Any) -> Any:
    try:
        return svc.docker.ops(host)
    except Exception as err:  # noqa: BLE001
        raise ToolError(f"host {host.name or host.id} is unreachable: {err}") from err


def resolve_ops(svc: Any, ref: Optional[str]) -> Tuple[Any, Any]:
    host = resolve_host(svc, ref)
    return host, ops_for_host(svc, host)


async def ping_host(svc: Any, host: Any) -> bool:
    try:
        ops = svc.docker.ops(host)
        await asyncio.wait_for(ops.ping(), timeout=PING_TIMEOUT)
        return True
    except Exception:  # noqa: BLE001 - any failure means offline
        return False


async def with_timeout(coro: Any, what: str, timeout: float = READ_TIMEOUT) -> Any:
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError as err:
        raise ToolError(f"{what} timed out after {int(timeout)}s") from err


async def close_stream(stream: Any) -> None:
    close = getattr(stream, "close", None)
    if close is None:
        return
    try:
        res = close()
        if asyncio.iscoroutine(res) or hasattr(res, "__await__"):
            await res
    except Exception:  # noqa: BLE001
        pass


# Paging ---------------------------------------------------------------------


def page(items: List[Any], args: Dict[str, Any], default: int, maximum: int) -> Dict[str, Any]:
    limit = int(args.get("limit") or default)
    limit = max(1, min(limit, maximum))
    offset = max(0, int(args.get("offset") or 0))
    chunk = items[offset: offset + limit]
    out: Dict[str, Any] = {
        "items": chunk,
        "total": len(items),
        "offset": offset,
        "returned": len(chunk),
        "truncated": offset + len(chunk) < len(items),
    }
    if out["truncated"]:
        out["nextOffset"] = offset + len(chunk)
    return out


# Formatting -----------------------------------------------------------------


def short_id(v: str) -> str:
    v = v or ""
    if v.startswith("sha256:"):
        v = v[7:]
    return v[:12]


def container_name(c: Any) -> str:
    names = getattr(c, "names", None) or []
    return (names[0] if names else "").lstrip("/")


_EXIT_RE = re.compile(r"Exited \((-?\d+)\)")


def exit_code_from_status(status: str) -> Optional[int]:
    m = _EXIT_RE.search(status or "")
    return int(m.group(1)) if m else None


def human_bytes(n: float) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"  # pragma: no cover


_REL_RE = re.compile(r"^(\d+)\s*([smhd])$")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_since(value: Any, now: Optional[float] = None) -> Optional[float]:
    """Parse ``since``: unix seconds, RFC3339, or a relative age like 15m/2h/1d.

    Returns unix seconds (float) or None when empty. Raises ToolError on junk.
    """
    if value is None or value == "":
        return None
    now = time.time() if now is None else now
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    s = str(value).strip()
    m = _REL_RE.match(s.lower())
    if m:
        return now - int(m.group(1)) * _UNITS[m.group(2)]
    try:
        return float(s)
    except ValueError:
        pass
    ts = parse_rfc3339(s)
    if ts is None:
        raise ToolError(f"invalid since {s!r}: use unix seconds, RFC3339 or a relative age like 15m, 2h, 1d")
    return ts


def parse_rfc3339(s: str) -> Optional[float]:
    s = s.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # Trim sub-microsecond precision (Docker emits nanoseconds).
    m = re.match(r"^(.*?\.\d{1,6})\d*(.*)$", s)
    if m:
        s = m.group(1) + m.group(2)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def find_by_id_or_name(items: List[Any], ref: str, what: str) -> Any:
    for it in items:
        if getattr(it, "id", None) == ref:
            return it
    named = [it for it in items if (getattr(it, "name", "") or "").lower() == ref.lower()]
    if len(named) == 1:
        return named[0]
    if len(named) > 1:
        raise ToolError(f"{what} name {ref!r} is ambiguous; use the id")
    raise ToolError(f"{what} {ref!r} not found")
