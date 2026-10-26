"""Admin-only audit/activity log over the protected ``/api/v1`` surface.

Serves ``GET /api/v1/audit``,
returning the recorded mutating API actions and authentication events
(newest-first) with username/time filtering and limit/offset pagination.

Recording itself is performed by the audit middleware (:mod:`kowan.web.audit`) and
the login handler; this module is read-only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiohttp import web

from kowan.store.audit import AuditListOptions
from kowan.web import api_route, bind_svc, error_response, json_response, require_role

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kowan.web import Services


# Default page size and hard cap.
_DEFAULT_LIMIT = 100
_MAX_LIMIT = 500


def _parse_positive(value: str, default: int) -> int:
    """Return ``value`` as a positive int, or ``default`` when absent/invalid.

    Only a valid ``n > 0`` overrides the default; anything
    else is ignored.
    """
    if not value:
        return default
    try:
        n = int(value)
    except ValueError:
        return default
    return n if n > 0 else default


async def _list_audit(svc: "Services", request: web.Request) -> web.Response:
    """Return audit entries newest-first with optional user/since/path filters.

    ``path`` narrows the log to one resource (the deployment page's activity).

    Limit defaults to 100 and is capped at 500; offset defaults to 0.
    """
    q = request.query

    limit = _parse_positive(q.get("limit", ""), _DEFAULT_LIMIT)
    if limit > _MAX_LIMIT:
        limit = _MAX_LIMIT

    offset = _parse_positive(q.get("offset", ""), 0)

    try:
        items, total = svc.store.list_audit(
            AuditListOptions(
                limit=limit,
                offset=offset,
                user=q.get("user", ""),
                since=q.get("since", ""),
                path=q.get("path", ""),
            )
        )
    except Exception:  # noqa: BLE001
        return error_response(500, "failed to list audit log")

    # json_response only recurses lists at the top level; a list nested inside a
    # dict would be passed to json.dumps and its pydantic models stringified via
    # repr. Dump the entries here so the response body carries camelCase JSON.
    return json_response(
        {
            "items": [e.model_dump(by_alias=True) for e in (items or [])],
            "total": total,
        }
    )


def register(app: web.Application, svc: "Services") -> None:
    """Mount ``GET /audit`` under ``/api/v1``. Reading the audit log is admin-only."""
    h = bind_svc(svc)
    api_route(app, "GET", "/audit", require_role("admin")(h(_list_audit)))
