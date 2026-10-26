"""Audit middleware: records mutating API actions to the audit store.

Runs AFTER the auth middleware so the
authenticated principal is present on the request. It records only mutating
methods (POST/PUT/PATCH/DELETE), captures the response status, and never blocks
or fails the response — recording errors are logged.

The GET /api/v1/audit endpoint is read-only and therefore never recorded (only
mutating methods are), avoiding self-referential noise.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Callable

from aiohttp import web

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .app import Services

log = logging.getLogger("kowan.audit")

# Request attribute set by the auth middleware carrying the authenticated
# principal. This is the
# single agreed key the whole api layer reads identity from — see app.py
# REQUEST_USER_KEY.
from .app import REQUEST_USER_KEY

_MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _is_mutating(method: str) -> bool:
    """Report whether ``method`` is a state-changing request worth auditing."""
    return method.upper() in _MUTATING


def client_ip(request: web.Request) -> str:
    """The client's address; proxy headers count only from trusted proxies
    (see kowan.web.netpolicy)."""
    from .netpolicy import client_ip as _client_ip

    return _client_ip(request)


def audit_middleware(svc: "Services") -> Callable:
    """Build the aiohttp audit middleware bound to ``svc``.

    When ``svc.audit`` (the AuditRecorder) is None the middleware is a no-op
    pass-through.
    """

    @web.middleware
    async def middleware(request: web.Request, handler):
        rec = getattr(svc, "audit", None)
        if rec is None or not _is_mutating(request.method):
            return await handler(request)

        status = 500
        try:
            response = await handler(request)
            status = response.status
            return response
        except web.HTTPException as exc:
            status = exc.status
            raise
        finally:
            entry = {
                "id": str(uuid.uuid4()),
                "action": request.method,
                "path": request.rel_url.path,
                "status": status,
                "ip": client_ip(request),
                "userId": "",
                "username": "",
            }
            user = request.get(REQUEST_USER_KEY)
            if user is not None:
                entry["userId"] = getattr(user, "id", "") or ""
                entry["username"] = getattr(user, "username", "") or ""
            try:
                # The store stamps the Time field itself. record() may be sync
                # (SQLite store) or async; support both.
                result = rec.record(entry)
                if result is not None and hasattr(result, "__await__"):
                    await result
            except Exception as err:  # never block/propagate on audit failure
                log.error(
                    "audit: record action: %s (method=%s path=%s)",
                    err,
                    request.method,
                    request.rel_url.path,
                )

    return middleware
