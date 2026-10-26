"""The notification center: the top-bar bell feed.

It aggregates
rule-driven alerts together with other noteworthy system events (a host going
offline/online, an alert auto-resolving) into a single read/unread feed.

It is both a bus consumer and an HTTP module. :class:`NotificationCenter`
subscribes to "alert" and "notice" events on the in-process bus and persists each
as a :class:`kowan.models.Notification`; :func:`register` mounts the feed and
read-state routes under ``/api/v1``. Keeping it separate from ``kowan.notify``
(which delivers alerts to external channels) means the bell can surface events —
like host connectivity changes and resolutions — that we deliberately do not spam
to email/chat.

All feed routes are available to any authenticated user (no min role): the feed
is a shared, deployment-wide view, not per-user.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional
from uuid import uuid4

from aiohttp import web

from kowan.events import Event
from kowan.models import Notification
from kowan.models.alert import AlertLevelInfo
from kowan.models.notification import NotificationKindAlert, NotificationKindNotice
from kowan.web import api_route, error_response, json_response

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kowan.web import Services

log = logging.getLogger("kowan.notifications")

# defaultRetention bounds how long notifications are kept before pruning.
DEFAULT_RETENTION = timedelta(days=30)

# Prune once every this many recorded notifications, so pruning never runs on the
# hot path of every single event.
PRUNE_EVERY = 100


def _now_rfc3339() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _str(payload: dict, key: str) -> str:
    """Read a string value from an event payload, tolerating missing/non-string."""
    v = payload.get(key)
    return v if isinstance(v, str) else ""


def _first_non_empty(a: str, b: str) -> str:
    return a if a else b


class NotificationCenter:
    """Consumes bus events into the notification store and serves the feed."""

    def __init__(self, svc: "Services", retention: timedelta = DEFAULT_RETENTION) -> None:
        self._svc = svc
        self._store = svc.store
        self._bus = svc.bus
        self._retention = retention
        # Touched only by the single subscriber task, so it needs no locking.
        self._writes = 0
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Subscribe to alert+notice events and record each until cancelled."""
        if self._task is not None:
            return
        self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        """Cancel the subscriber task (used on shutdown)."""
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self) -> None:
        sub = self._bus.subscribe("alert", "notice")
        try:
            async for e in sub:
                self._record(e)
        except asyncio.CancelledError:
            raise
        finally:
            sub.cancel()

    def _record(self, e: Event) -> None:
        """Convert a bus event into a stored notification."""
        payload = e.payload or {}
        level = _str(payload, "level") or AlertLevelInfo
        n = Notification(
            id=str(uuid4()),
            level=level,
            title=_str(payload, "title"),
            hostId=_str(payload, "hostId"),
            createdAt=_now_rfc3339(),
        )

        if e.type == "alert":
            n.kind = NotificationKindAlert
            n.body = _str(payload, "message")
            n.source = "monitor"
            n.ref_id = _str(payload, "id")
        else:  # "notice"
            n.kind = NotificationKindNotice
            n.body = _str(payload, "body")
            n.source = _first_non_empty(_str(payload, "source"), e.source)
            n.ref_id = _str(payload, "refId")

        try:
            self._store.create_notification(n)
        except Exception as err:  # noqa: BLE001
            log.error("notifications: record error=%s eventId=%s", err, e.id)
            return

        self._writes += 1
        if self._writes % PRUNE_EVERY == 0:
            self._prune()

    def _prune(self) -> None:
        """Trim notifications older than the retention window."""
        before = (datetime.now(timezone.utc) - self._retention).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            removed = self._store.prune_notifications(before)
        except Exception as err:  # noqa: BLE001
            log.error("notifications: prune error=%s", err)
            return
        if removed > 0:
            log.debug("notifications: pruned removed=%s", removed)

    # -- HTTP handlers ------------------------------------------------------
    async def list(self, request: web.Request) -> web.Response:
        limit = 0
        v = request.query.get("limit", "")
        if v:
            try:
                n = int(v)
                if n > 0:
                    limit = n
            except ValueError:
                pass
        try:
            items = self._store.list_notifications(limit)
        except Exception as err:  # noqa: BLE001
            log.error("notifications: list error=%s", err)
            return error_response(500, "failed to list notifications")
        from kowan.auth import authorize as _authz  # team/grant scoping by host

        return json_response(_authz.filter_host_items(items or []))

    async def unread_count(self, request: web.Request) -> web.Response:
        from kowan.auth import authorize as _authz  # team/grant scoping by host

        try:
            ctx = _authz.current()
            principal = ctx.principal if ctx is not None else None
            if callable(principal):
                principal = principal()
            if principal is None or not principal.scoped or principal.is_admin:
                n = self._store.count_unread_notifications()
            else:
                # Scoped principals count only what the list would show them.
                items = _authz.filter_host_items(self._store.list_notifications(0) or [])
                n = sum(1 for i in items if not i.read)
        except Exception as err:  # noqa: BLE001
            log.error("notifications: unread count error=%s", err)
            return error_response(500, "failed to count notifications")
        return json_response({"count": n})

    async def read(self, request: web.Request) -> web.Response:
        nid = request.match_info.get("id", "")
        if not nid:
            return error_response(400, "missing notification id")
        try:
            self._store.mark_notification_read(nid)
        except Exception as err:  # noqa: BLE001
            log.error("notifications: mark read id=%s error=%s", nid, err)
            return error_response(500, "failed to update notification")
        return json_response({"status": "read"})

    async def read_all(self, request: web.Request) -> web.Response:
        try:
            self._store.mark_all_notifications_read()
        except Exception as err:  # noqa: BLE001
            log.error("notifications: mark all read error=%s", err)
            return error_response(500, "failed to update notifications")
        return json_response({"status": "read"})


def register(app: web.Application, svc: "Services") -> NotificationCenter:
    """Create the notification center and mount its routes under ``/api/v1``.

    Returns the :class:`NotificationCenter` so the caller can ``await center.start()``
    to begin consuming bus events. All routes are available to any authenticated
    user — the feed is a shared, deployment-wide view.
    """
    center = NotificationCenter(svc)
    api_route(app, "GET", "/notifications", center.list)
    api_route(app, "GET", "/notifications/unread-count", center.unread_count)
    api_route(app, "POST", "/notifications/read-all", center.read_all)
    api_route(app, "POST", "/notifications/{id}/read", center.read)

    # The center owns its subscriber task: start it with the app, stop it on
    # cleanup. (Previously the returned center was never started, so the bell
    # never received alerts or notices.)
    async def _start(_app: web.Application) -> None:
        await center.start()

    async def _stop(_app: web.Application) -> None:
        await center.stop()

    app.on_startup.append(_start)
    app.on_cleanup.append(_stop)
    return center


__all__ = ["NotificationCenter", "register", "DEFAULT_RETENTION", "PRUNE_EVERY"]
