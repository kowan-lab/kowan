"""Alert delivery to external notification channels.

A :class:`~kowan.notify.provider.Provider`
delivers a :class:`~kowan.notify.provider.Message` to an external destination
(email/webhook/chat); the :class:`~kowan.notify.notifier.Notifier` subscribes to
"alert" events on the in-process bus and fans each alert out to every enabled
channel whose minimum level is satisfied.

Importing this package registers all built-in providers (smtp/webhook/ntfy/
telegram/slack/discord), so
``provider.types()`` / ``provider.build()`` / ``provider.sensitive_keys()`` are
usable immediately.
"""

from __future__ import annotations

from . import provider
from .notifier import Notifier
from .provider import (
    FieldSpec,
    Message,
    Provider,
    build,
    fields,
    sensitive_keys,
    types,
)

# Register every built-in provider on package import.
provider.load_builtins()

__all__ = [
    "provider",
    "Notifier",
    "Message",
    "Provider",
    "FieldSpec",
    "build",
    "fields",
    "sensitive_keys",
    "types",
]
