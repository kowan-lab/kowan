"""Tool catalog: definitions, role gating, argument validation, output bounds,
the destructive-action confirm guard and audit recording.

Every tool is declared once with a :func:`tool` decorator carrying its JSON
Schema, minimum role and MCP annotations. :class:`ToolRegistry` then:

  * filters ``tools/list`` to what the caller's role may use;
  * on ``tools/call`` checks the role again (never trust the list), validates
    the arguments against the schema, enforces ``confirm: true`` on destructive
    tools, runs the handler, bounds the output size and writes an audit entry
    for every mutating call (actor = the key/user, ``source=mcp`` in detail).

Failures inside a tool (Docker errors, validation of business rules, a missing
container...) are returned as a normal result with ``isError: true`` so the
model can read and react to them; only malformed calls (unknown tool, bad
``params`` shape, role violation) are JSON-RPC errors.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .. import jsonx
from .protocol import FORBIDDEN, INVALID_PARAMS, CallContext, RPCError

log = logging.getLogger("kowan.mcp.tools")

ROLE_VIEWER = "viewer"
ROLE_OPERATOR = "operator"
ROLE_ADMIN = "admin"
_RANK = {ROLE_VIEWER: 0, ROLE_OPERATOR: 1, ROLE_ADMIN: 2}

# Hard cap on the text block of any tool result (~64 KiB). Individual tools
# bound their own lists/lines first; this is the last line of defence.
MAX_TEXT_BYTES = 64 * 1024

AUDIT_ACTION = "MCP"
AUDIT_PATH_PREFIX = "/mcp/tools/"


def role_rank(role: str) -> int:
    """Rank a role; unknown roles get viewer rights (REST reads are open to any
    authenticated principal, so this matches the API)."""
    return _RANK.get(role or "", 0)


def role_allows(role: str, required: str) -> bool:
    return role_rank(role) >= _RANK[required]


class ToolError(Exception):
    """A tool-level failure, reported as ``isError: true`` content."""


@dataclass
class ToolOutput:
    """Rich handler return value: structured data plus optional custom text."""

    data: Dict[str, Any]
    text: Optional[str] = None


Handler = Callable[[CallContext, Dict[str, Any]], Awaitable[Any]]


@dataclass
class Tool:
    name: str
    title: str
    description: str
    input_schema: Dict[str, Any]
    handler: Handler
    role: str = ROLE_VIEWER
    read_only: bool = True
    destructive: bool = False
    idempotent: bool = True
    open_world: bool = False
    output_schema: Optional[Dict[str, Any]] = None
    requires_confirm: bool = False

    def descriptor(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": {
                "title": self.title,
                "readOnlyHint": self.read_only,
                "destructiveHint": self.destructive,
                "idempotentHint": self.idempotent,
                "openWorldHint": self.open_world,
            },
        }
        if self.output_schema is not None:
            d["outputSchema"] = self.output_schema
        return d


# ---------------------------------------------------------------------------
# minimal JSON Schema validation (the subset our tool schemas use)
# ---------------------------------------------------------------------------

_TYPES = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: (isinstance(v, int) and not isinstance(v, bool))
    or (isinstance(v, float) and v.is_integer()),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
}


def validate(schema: Dict[str, Any], value: Any, path: str = "") -> List[str]:
    """Return human-readable violations of ``schema`` by ``value`` (empty = ok)."""
    errs: List[str] = []
    where = path or "arguments"
    t = schema.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        if not any(_TYPES[x](value) for x in types if x in _TYPES):
            return [f"{where}: expected {' or '.join(types)}"]
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{where}: must be one of {', '.join(map(str, schema['enum']))}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errs.append(f"{where}: must not be empty" if schema["minLength"] == 1 else f"{where}: too short")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errs.append(f"{where}: longer than {schema['maxLength']} characters")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errs.append(f"{where}: must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errs.append(f"{where}: must be <= {schema['maximum']}")
    if isinstance(value, dict):
        props = schema.get("properties") or {}
        for req in schema.get("required") or []:
            if req not in value:
                errs.append(f"{path + '.' if path else ''}{req}: is required")
        for k, v in value.items():
            sub = props.get(k)
            if sub is None:
                addl = schema.get("additionalProperties", True)
                if addl is False:
                    errs.append(f"{path + '.' if path else ''}{k}: unknown argument")
                elif isinstance(addl, dict):
                    errs.extend(validate(addl, v, f"{path + '.' if path else ''}{k}"))
                continue
            errs.extend(validate(sub, v, f"{path + '.' if path else ''}{k}"))
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errs.append(f"{where}: more than {schema['maxItems']} items")
        for i, v in enumerate(value):
            errs.extend(validate(schema["items"], v, f"{where}[{i}]"))
    return errs


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------


def to_plain(obj: Any) -> Any:
    """Turn pydantic models (by alias) and friends into plain JSON values."""
    return json.loads(jsonx.dumps_response(obj))


def _dump_text(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)


def text_result(text: str, *, is_error: bool = False, structured: Any = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": is_error}
    if structured is not None:
        out["structuredContent"] = structured
    return out


def bound_text(text: str, limit: int = MAX_TEXT_BYTES) -> tuple[str, bool]:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text, False
    cut = raw[:limit].decode("utf-8", "ignore")
    note = (
        f"\n\n[output truncated: {len(raw)} bytes exceeds the {limit}-byte limit; "
        "narrow the request with limit/offset, tail or filters]"
    )
    return cut + note, True


def build_result(tool: Tool, value: Any) -> Dict[str, Any]:
    if isinstance(value, ToolOutput):
        data, text = value.data, value.text
    else:
        data, text = value, None
    data = to_plain(data)
    if not isinstance(data, dict):  # structuredContent must be an object
        data = {"result": data}
    full = text if text is not None else _dump_text(data)
    shown, truncated = bound_text(full)
    if truncated and tool.output_schema is None:
        # The structured copy would be just as large; replace it with a marker.
        data = {"truncated": True, "note": "output exceeded the size limit; see text content"}
    return text_result(shown, structured=data)


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------

_REDACT_KEYS = {"compose", "spec", "inputs", "values"}


def _args_summary(args: Dict[str, Any]) -> str:
    safe: Dict[str, Any] = {}
    for k, v in args.items():
        if k in _REDACT_KEYS:
            if isinstance(v, dict):
                safe[k] = {kk: "***" for kk in v}  # keep input names, drop values
            else:
                safe[k] = f"<{len(str(v))} chars>"
        else:
            safe[k] = v
    s = _dump_text(safe)
    return s if len(s) <= 800 else s[:800] + "..."


def record_audit(ctx: CallContext, tool_name: str, status: int, args: Dict[str, Any], error: str = "") -> None:
    rec = getattr(ctx.svc, "audit", None)
    if rec is None:
        return
    detail = f"source=mcp tool={tool_name} args={_args_summary(args)}"
    if error:
        detail += f" error={error[:300]}"
    entry = {
        "id": str(uuid.uuid4()),
        "action": AUDIT_ACTION,
        "path": AUDIT_PATH_PREFIX + tool_name,
        "status": status,
        "ip": ctx.ip,
        "userId": getattr(ctx.user, "id", "") or "",
        "username": getattr(ctx.user, "username", "") or "",
        "detail": detail,
    }
    try:
        res = rec.record(entry)
        if asyncio.iscoroutine(res):  # pragma: no cover - recorder is sync today
            asyncio.ensure_future(res)
    except Exception as err:  # noqa: BLE001 - never fail a call on audit errors
        log.error("mcp audit: record %s: %s", tool_name, err)


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------

CONFIRM_PROP = {
    "type": "boolean",
    "description": "Must be true to perform this destructive action. Ask the user first.",
}


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}

    def add(self, t: Tool) -> Tool:
        if t.name in self._tools:
            raise ValueError(f"duplicate tool {t.name}")
        schema = t.input_schema
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        schema.setdefault("additionalProperties", False)
        if t.requires_confirm:
            schema["properties"]["confirm"] = CONFIRM_PROP
        self._tools[t.name] = t
        return t

    def tool(self, name: str, *, title: str, description: str, schema: Optional[Dict[str, Any]] = None, **kw: Any):
        def deco(fn: Handler) -> Handler:
            self.add(Tool(name=name, title=title, description=description,
                          input_schema=schema or {}, handler=fn, **kw))
            return fn

        return deco

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def all(self) -> List[Tool]:
        return list(self._tools.values())

    def visible(self, role: str) -> List[Tool]:
        return [t for t in self._tools.values() if role_allows(role, t.role)]

    async def call(self, name: str, args: Dict[str, Any], ctx: CallContext) -> Dict[str, Any]:
        t = self._tools.get(name)
        if t is None:
            raise RPCError(INVALID_PARAMS, f"unknown tool: {name}")
        mutating = not t.read_only
        if not role_allows(ctx.role, t.role):
            if mutating:
                record_audit(ctx, name, 403, args, "insufficient role")
            raise RPCError(
                FORBIDDEN,
                f"tool {name} requires the {t.role} role (you are {ctx.role or 'unknown'})",
                {"requiredRole": t.role},
            )
        errs = validate(t.input_schema, args)
        if errs:
            return text_result("invalid arguments: " + "; ".join(errs), is_error=True)
        if t.requires_confirm and args.get("confirm") is not True:
            return text_result(
                f"{name} is destructive and was not executed. Confirm with the user, "
                "then call it again with \"confirm\": true.",
                is_error=True,
            )
        try:
            # Team/grant scoping (kowan/auth/authorize.py): the default level a
            # deployment/workflow check needs follows the tool's nature.
            from ..auth import authorize as _authz

            lvl = _authz.LEVEL_READ if t.read_only else (
                _authz.LEVEL_MANAGE if t.destructive else _authz.LEVEL_OPERATE)
            with _authz.default_level(lvl):
                value = await t.handler(ctx, args)
        except ToolError as err:
            if mutating:
                record_audit(ctx, name, 400, args, str(err))
            return text_result(str(err), is_error=True)
        except Exception as err:  # noqa: BLE001 - AppError, docker errors, ...
            from ..errors import AppError

            if isinstance(err, AppError):
                msg, status = err.message or str(err), int(err.status)
            else:
                log.warning("mcp tool %s failed: %r", name, err)
                msg, status = f"{type(err).__name__}: {err}" if str(err) else type(err).__name__, 500
            if mutating:
                record_audit(ctx, name, status, args, msg)
            return text_result(msg, is_error=True)
        if mutating:
            record_audit(ctx, name, 200, args)
        return build_result(t, value)
