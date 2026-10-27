"""Built-in catalog of workflow TRIGGER types.

The registry plus the per-type modules. A trigger is what STARTS a workflow run; a workflow may carry several,
firing independently (OR semantics). Like the notify/secrets/registry packages
every trigger type is compiled/imported into the process (no dynamic loading).

Trigger types fall into three KINDS by how they fire:

  - ``KindPoll``: evaluated on an interval by the engine's poller
    (docker-image, git-commit). These implement :class:`Poller`.
  - ``KindCron``: scheduled by the workflow Scheduler from a cron expression.
  - ``KindWebhook``: fired by the public ``/hooks/workflow/{token}`` endpoint.

Cron and webhook carry no Poller (their firing lives in the scheduler/hook);
they are registered only so they appear in the catalog and the trigger editor
alongside the polling types.

Learner notes:

  - The built-in types register when this package is imported — see :func:`register_builtins` at the bottom, which imports every
    trigger module. Importing ``kowan.triggers`` therefore populates the catalog.
  - ``Poller`` is an abstract base class. A ``Factory`` (``config -> Poller``)
    is a plain callable.
  - Evaluate is ``async`` because the poll HTTP I/O goes through :mod:`kowan.httpcap`
    (aiohttp's client).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

# --- trigger type identifiers --------------------------------------------
TypeCron = "cron"
TypeWebhook = "webhook"
TypeDockerImage = "docker-image"
TypeGitCommit = "git-commit"
TypeRSS = "rss"

# --- trigger kinds: which subsystem fires the trigger ----------------------
KindPoll = "poll"
KindCron = "cron"
KindWebhook = "webhook"


class Poller:
    """Implemented by poll-kind trigger types.

    :meth:`evaluate` checks the external source and reports whether the workflow
    should fire now, returning the new opaque state to persist (last-seen RSS
    guid, image digest, git SHA, ...).

    Seeding contract: when ``state`` is empty (a freshly added trigger)
    ``evaluate`` MUST record the current value and return ``fired=False``, so
    attaching a trigger never fires a run for history that predates it. Raising
    leaves the stored state untouched and the error is surfaced on the trigger
    as ``lastError``.
    """

    async def evaluate(
        self, config: Dict[str, str], state: str
    ) -> Tuple[bool, str]:  # pragma: no cover - interface
        """Return ``(fired, new_state)``; raise on error."""
        raise NotImplementedError


# A Factory builds a Poller from a trigger's decrypted config map. It should be
# cheap and must NOT perform network I/O (that belongs in ``evaluate``). It is
# None for cron/webhook kinds.
Factory = Callable[[Dict[str, str]], Poller]


@dataclass
class FieldSpec:
    """One configuration field of a trigger type so the frontend can render the
    right input and the API can validate it. JSON shape matches the frontend
    PluginField (key/label/sensitive/required/placeholder).

    When ``connection`` is set the field is a CREDENTIAL PICKER, not a text input:
    its value (a connection id) lives in ``config[key]`` and the frontend renders a
    <ConnectionField> filtered to the given capability instead of a plain input. The
    descriptor shape is ``{"capability": str, "types"?: list[str], "optional": bool}``
    — auth is optional, so the picker offers a "None" choice.
    """

    key: str = ""
    label: str = ""
    sensitive: bool = False
    required: bool = False
    placeholder: str = ""
    connection: Optional[dict] = None
    # A fixed list of allowed string values. When set the frontend renders a
    # Select (dropdown) instead of a free-text input. None/empty = free text.
    options: Optional[List[str]] = None

    def to_dict(self) -> dict:
        out = {
            "key": self.key,
            "label": self.label,
            "sensitive": self.sensitive,
            "required": self.required,
            "placeholder": self.placeholder,
        }
        # Only emit the key for credential-picker fields so ordinary fields
        # serialize byte-for-byte as before.
        if self.connection is not None:
            out["connection"] = self.connection
        # Only emit options when a choice list is provided, so plain text fields
        # serialize exactly as before.
        if self.options:
            out["options"] = list(self.options)
        return out


@dataclass
class _Def:
    kind: str
    label: str
    description: str
    factory: Optional[Factory]
    fields: List[FieldSpec] = field(default_factory=list)


# The registry is a module-global map guarded by a lock.
_lock = threading.RLock()
_types: Dict[str, _Def] = {}


def register(
    ttype: str,
    kind: str,
    label: str,
    description: str,
    factory: Optional[Factory],
    fields: Optional[List[FieldSpec]],
) -> None:
    """Add a trigger type. ``factory`` may be None for non-poll kinds
    (cron/webhook)."""
    with _lock:
        _types[ttype] = _Def(
            kind=kind,
            label=label,
            description=description,
            factory=factory,
            fields=list(fields or []),
        )


def is_registered(ttype: str) -> bool:
    """Whether ``ttype`` names a known trigger type."""
    with _lock:
        return ttype in _types


def kind(ttype: str) -> str:
    """Kind of a trigger type ("" if unknown)."""
    with _lock:
        d = _types.get(ttype)
        return d.kind if d else ""


def build(ttype: str, config: Dict[str, str]) -> Poller:
    """Construct the Poller for a poll-kind trigger type from ``config``.

    Raises for cron/webhook kinds, which have no Poller, and for unknown types.
   
    """
    with _lock:
        d = _types.get(ttype)
    if d is None:
        raise ValueError(f"triggers: unknown type {ttype!r}")
    if d.factory is None:
        raise ValueError(f"triggers: type {ttype!r} is not a polling trigger")
    try:
        return d.factory(config)
    except Exception as err:  # noqa: BLE001 - wrap with the type name, chaining the cause
        raise ValueError(f"triggers: build {ttype}: {err}") from err


def fields(ttype: str) -> List[FieldSpec]:
    """Config field schema for a trigger type, or [] if unknown (returns a copy)."""
    with _lock:
        d = _types.get(ttype)
        if d is None:
            return []
        return list(d.fields)


def sensitive_keys(ttype: str) -> Dict[str, bool]:
    """Config keys marked Sensitive for a trigger type, used by the API to mask
    values on read and preserve them on update."""
    out: Dict[str, bool] = {}
    for f in fields(ttype):
        if f.sensitive:
            out[f.key] = True
    return out


@dataclass
class Spec:
    """A registered trigger type with its metadata + field schema for the API
    (the plugins catalog and the workflow trigger editor)."""

    type: str
    kind: str
    label: str
    description: str
    fields: List[FieldSpec]

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "kind": self.kind,
            "label": self.label,
            "description": self.description,
            "fields": [f.to_dict() for f in self.fields],
        }


def specs() -> List[Spec]:
    """Every registered trigger type with its schema, sorted by type name for a
    stable response."""
    with _lock:
        out = [
            Spec(
                type=t,
                kind=d.kind,
                label=d.label,
                description=d.description,
                fields=list(d.fields),
            )
            for t, d in _types.items()
        ]
    out.sort(key=lambda s: s.type)
    return out


_builtins_loaded = False


def register_builtins() -> None:
    """Import every built-in trigger module so its ``register(...)`` call runs.

    Idempotent. Importing the submodules executes their module-level ``register(...)`` calls, seeding
    the catalog. Called on package import (below) and safe to call again.
    """
    global _builtins_loaded
    if _builtins_loaded:
        return
    # Importing each module runs its top-level register() call.
    from . import builtin  # noqa: F401
    from . import dockerimage  # noqa: F401
    from . import gitcommit  # noqa: F401
    from . import rss  # noqa: F401

    _builtins_loaded = True


__all__ = [
    "TypeCron",
    "TypeWebhook",
    "TypeDockerImage",
    "TypeGitCommit",
    "TypeRSS",
    "KindPoll",
    "KindCron",
    "KindWebhook",
    "Poller",
    "Factory",
    "FieldSpec",
    "Spec",
    "register",
    "is_registered",
    "kind",
    "build",
    "fields",
    "sensitive_keys",
    "specs",
    "register_builtins",
]

# Seed the catalog on import.
register_builtins()
