"""Provider registry for notification channels.

A ``Provider`` delivers a single
:class:`Message` to an external destination (email, webhook, chat, ...). Built-in
providers register themselves with :func:`register` (via a module-level call in
each provider file); :func:`build` constructs one from
a decrypted config map, gated by the plugin enable state.

"Provider" here is a plain provider pattern — an interface plus a registry of
built-in implementations selected by a channel's ``type`` — NOT a dynamically
loaded plugin. Nothing is imported at runtime; every provider file is imported
into the process (see :func:`load_builtins`), and the ``plugins`` layer only
tracks an on/off flag per ``(system, type)`` pair.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from kowan.errors import Validation


@dataclass
class Message:
    """A provider-agnostic notification payload.

    ``level`` is one of "info", "warning" or "critical".
    """

    title: str = ""
    body: str = ""
    level: str = ""


class Provider:
    """Delivers a single :class:`Message` to an external destination.

    Implementations must be async and raise on failure (including non-2xx HTTP
    responses) so the notifier can log per-channel failures.
    """

    async def send(self, msg: Message) -> None:  # pragma: no cover - interface
        raise NotImplementedError


# A Factory builds a Provider from a decrypted config map, raising
# kowan.errors.Validation when required fields are missing/invalid.
Factory = Callable[[Dict[str, str]], Provider]


@dataclass
class FieldSpec:
    """Describes one config field of a provider so the frontend can render it.

    Sensitive fields are stored encrypted and masked on read; required fields
    must be present for the provider to build. Serialised as
    key/label/sensitive/required/placeholder.
    """

    key: str
    label: str = ""
    sensitive: bool = False
    required: bool = False
    placeholder: str = ""

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "sensitive": self.sensitive,
            "required": self.required,
            "placeholder": self.placeholder,
        }


@dataclass
class _ProviderDef:
    factory: Factory
    fields: List[FieldSpec] = field(default_factory=list)


# The built-in provider registry keyed by type. Populated by register() at import
# time and guarded by a lock for concurrent use.
_lock = threading.RLock()
_registry: Dict[str, _ProviderDef] = {}


def register(
    provider_type: str, factory: Factory, fields: List[FieldSpec]
) -> None:
    """Add a provider type with its factory and config field schema.

    Called at import time from each provider module. Re-registering a type
    overwrites the previous definition.
    """
    with _lock:
        _registry[provider_type] = _ProviderDef(factory=factory, fields=list(fields))


def build(provider_type: str, config: Dict[str, str]) -> Provider:
    """Construct a Provider of ``provider_type`` from ``config``.

    Raises :class:`kowan.errors.Validation` if the type is unknown or the factory
    rejects the config.
    """
    with _lock:
        d = _registry.get(provider_type)
    if d is None:
        raise Validation(f"notify: unknown provider type {provider_type!r}")
    try:
        return d.factory(config)
    except Validation as err:
        raise Validation(
            f"notify: build {provider_type} provider: {err.message or err}"
        ) from err


def fields(provider_type: str) -> Optional[List[FieldSpec]]:
    """Return the config field schema for a type, or ``None`` if unknown.

    The returned list is a copy that is safe to mutate.
    """
    with _lock:
        d = _registry.get(provider_type)
    if d is None:
        return None
    return list(d.fields)


def sensitive_keys(provider_type: str) -> Dict[str, bool]:
    """Return the set of config keys marked sensitive for a type.

    The store uses this (via ``set_channel_sensitivity``) to decide which values
    to mask on read and preserve on update when left blank.
    """
    out: Dict[str, bool] = {}
    fs = fields(provider_type)
    if fs is None:
        return out
    for f in fs:
        if f.sensitive:
            out[f.key] = True
    return out


def types() -> List[dict]:
    """Return every registered provider type with its field schema.

    Sorted by type name for a stable API response. Each entry is
    ``{"type": t, "fields": [FieldSpec-as-dict, ...]}``.
    """
    with _lock:
        items = [
            {"type": t, "fields": [f.to_dict() for f in d.fields]}
            for t, d in _registry.items()
        ]
    items.sort(key=lambda e: e["type"])
    return items


def require_field(config: Dict[str, str], key: str) -> str:
    """Return the value for ``key`` in ``config``, or raise Validation if empty.

    Small helper shared by provider factories.
    """
    v = config.get(key, "")
    if v == "":
        raise Validation(f"missing required field {key!r}")
    return v


def join_title_body(msg: Message) -> str:
    """Combine a message title and body into one text block.

    Omits empty parts. Used by the chat/telegram providers that carry a single
    text field.
    """
    if msg.title and msg.body:
        return msg.title + "\n" + msg.body
    if msg.title:
        return msg.title
    return msg.body.strip()


def load_builtins() -> None:
    """Import every built-in provider module so its register() call runs.

    Importing the notify package calls this, so all built-in providers are
    registered. Safe to call more than once.
    """
    from . import discord, ntfy, slack, smtp, telegram, webhook  # noqa: F401


__all__ = [
    "Message",
    "Provider",
    "Factory",
    "FieldSpec",
    "register",
    "build",
    "fields",
    "sensitive_keys",
    "types",
    "require_field",
    "join_title_body",
    "load_builtins",
]
