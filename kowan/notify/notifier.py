"""Alert -> external channel dispatch.

The :class:`Notifier` subscribes to "alert" events on the in-process event bus and
fans each alert out to every enabled channel whose minimum level is satisfied,
building the matching provider from the channel's decrypted config and sending.
Per-channel build/send failures are logged and never stop the other channels.

This is distinct from the notification center (``kowan.notifications``): the
notifier delivers alerts to *external* destinations (email/chat/webhook), while
the center records events for the in-app bell.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Optional

from kowan.events import Event
from kowan.models import Alert, Credential, CredentialKindChannel
from kowan.models.alert import AlertLevelCritical, AlertLevelInfo, AlertLevelWarning

from .provider import Message

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kowan.web import Services

log = logging.getLogger("kowan.notify")

# Severity ranking. Unknown levels rank below info so they are effectively never
# suppressed by a min-level gate.
_LEVEL_ORDER = {
    AlertLevelInfo: 1,
    AlertLevelWarning: 2,
    AlertLevelCritical: 3,
}


def _level_at_least(alert_level: str, min_level: str) -> bool:
    """Report whether ``alert_level`` is at or above ``min_level``.

    An empty ``min_level`` is treated as "info" (no gating).
    """
    if min_level == "":
        min_level = AlertLevelInfo
    return _LEVEL_ORDER.get(alert_level, 0) >= _LEVEL_ORDER.get(min_level, 0)


def _message_from_alert(a: Alert) -> Message:
    """Derive a provider :class:`Message` from an alert.

    Title and Level map directly; the Body folds in the human-readable message
    plus host/time context.
    """
    body = a.message
    if a.host_id:
        body = f"{body}\n\nHost: {a.host_id}"
    if a.created_at:
        body = f"{body}\nTime: {a.created_at}"
    return Message(title=a.title, body=body, level=a.level)


def _alert_from_payload(payload: dict) -> Alert:
    """Reconstruct an :class:`Alert` from a bus event payload.

    The monitor publishes the alert fields directly in the payload (camelCase
    aliases), so pydantic's alias-aware validation maps them onto the model.
    """
    return Alert.model_validate(payload)


class Notifier:
    """Subscribes to "alert" events and dispatches them to notification channels."""

    def __init__(self, svc: "Services") -> None:
        self._svc = svc
        self._store = svc.store
        self._bus = svc.bus
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Subscribe to "alert" events and dispatch until the task is cancelled.

        Runs its own background task and returns immediately.
        """
        if self._task is not None:
            return
        self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        """Cancel the dispatch task (used on shutdown)."""
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self) -> None:
        sub = self._bus.subscribe("alert")
        try:
            async for e in sub:
                await self._handle(e)
        except asyncio.CancelledError:
            raise
        finally:
            sub.cancel()

    async def _handle(self, e: Event) -> None:
        """Decode an alert event and dispatch it."""
        try:
            a = _alert_from_payload(e.payload)
        except Exception as err:  # noqa: BLE001
            log.warning("notify: failed to decode alert event error=%s eventId=%s", err, e.id)
            return
        await self._dispatch(a)

    async def _dispatch(self, a: Alert) -> None:
        """Send ``a`` to every enabled channel whose min level is satisfied."""
        try:
            channels = self._store.list_credentials(CredentialKindChannel)
        except Exception as err:  # noqa: BLE001
            log.error("notify: list channels error=%s alertId=%s", err, a.id)
            return

        msg = _message_from_alert(a)
        for ch in channels:
            if not ch.enabled:
                continue
            # Per-rule routing: when an alert names a specific channel, deliver
            # only through it. An empty channel_id keeps the broadcast behaviour.
            if a.channel_id and ch.id != a.channel_id:
                continue
            if not _level_at_least(a.level, ch.min_level):
                continue
            await self._send_to(ch, msg, alert_id=a.id)

    async def _send_to(
        self, ch: Credential, msg: Message, *, alert_id: str
    ) -> None:
        # Resolve the messageSend capability: an inline channel builds its own
        # transport; a channel that delegates to a shared connection (a reused
        # Slack/SMTP connection) mints it from that connection.
        from kowan import capabilities as caps
        from kowan import connections

        try:
            provider = await connections.resolve_service(
                self._store, ch.id, caps.CAP_MESSAGE_SEND
            )
        except Exception as err:  # noqa: BLE001
            log.error("notify: build provider error=%s channel=%s type=%s", err, ch.id, ch.type)
            return
        try:
            await provider.send(msg)
        except Exception as err:  # noqa: BLE001
            log.error(
                "notify: send via channel error=%s channel=%s name=%s type=%s",
                err, ch.id, ch.name, ch.type,
            )
            return
        log.info(
            "notify: dispatched alert channel=%s type=%s alertId=%s level=%s",
            ch.name, ch.type, alert_id, msg.level,
        )


__all__ = ["Notifier"]
