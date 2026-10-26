"""External secret provider registry + interface.

A :class:`Provider` resolves a single
live secret value from an external backend (Vault/Infisical/Phase). It is built
from a secret's decrypted config map and consulted at deploy/run time by the
store's secret resolver. This mirrors the notify provider model: an interface
with compiled-in implementations selected by type, NOT a dynamically loaded
plugin.

"External" secrets (vault, infisical, …) store a connection+reference rather than
a value, and are fetched live each time they are referenced. Static secrets
(generic/token/env/…) are not providers — they hold their value directly and
never reach this package.

The concrete providers register themselves at import time; :func:`register_all`
(called from :mod:`kowan.secrets.__init__` via the ``vault``/``infisical``/``phase``
modules) is the seam that wires them in without an import cycle.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable, Dict, List, Protocol

from kowan.registry.fieldspec import FieldSpec


class Provider(Protocol):
    """Resolves the live value for one external secret.

    ``field_override`` is the optional FIELD part of a ``[[NAME.FIELD]]``
    reference; its meaning is provider-specific (for vault/infisical it overrides
    which key is read). Implementations are async (network I/O).
    """

    async def resolve(self, field_override: str) -> str:  # pragma: no cover - protocol
        ...


# Factory builds a Provider from a decrypted config map. It should be cheap and
# must not perform network I/O (that belongs in resolve).
Factory = Callable[[Dict[str, str]], Provider]


@dataclass
class _ProviderDef:
    factory: Factory
    fields: List[FieldSpec]


_lock = threading.RLock()
_registry: Dict[str, _ProviderDef] = {}


def register(provider_type: str, factory: Factory, fields: List[FieldSpec]) -> None:
    """Add an external secret provider type. Called at import time by each
    provider module (vault/infisical/phase)."""
    with _lock:
        _registry[provider_type] = _ProviderDef(factory=factory, fields=list(fields))


def is_external(t: str) -> bool:
    """Report whether ``t`` names a registered external secret backend (one
    resolved live), as opposed to a static stored secret."""
    with _lock:
        return t in _registry


def build(provider_type: str, config: Dict[str, str]) -> Provider:
    """Construct a :class:`Provider` of ``provider_type`` from ``config``.

   
    """
    with _lock:
        definition = _registry.get(provider_type)
    if definition is None:
        raise ValueError(f"secrets: unknown provider type {provider_type!r}")
    try:
        return definition.factory(config)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"secrets: build {provider_type} provider: {e}") from e


def fields(provider_type: str) -> List[FieldSpec]:
    """Return the config field schema for ``provider_type``, or [] if unknown.
   """
    with _lock:
        definition = _registry.get(provider_type)
    if definition is None:
        return []
    return list(definition.fields)


def sensitive_keys(provider_type: str) -> Dict[str, bool]:
    """Return the config keys marked sensitive for ``provider_type``, used by the
    API to mask values on reveal and preserve them on update."""
    return {f.key: True for f in fields(provider_type) if f.sensitive}


@dataclass
class ProviderType:
    """A registered external secret type bundled with its field schema, for the
    API."""

    type: str
    fields: List[FieldSpec]

    def to_dict(self) -> dict:
        return {"type": self.type, "fields": [f.to_dict() for f in self.fields]}


def types() -> List[ProviderType]:
    """Return every registered external secret provider type with its field
    schema, sorted by type name for a stable response."""
    with _lock:
        out = [ProviderType(type=t, fields=list(d.fields)) for t, d in _registry.items()]
    out.sort(key=lambda p: p.type)
    return out


__all__ = [
    "Provider",
    "Factory",
    "FieldSpec",
    "ProviderType",
    "register",
    "is_external",
    "build",
    "fields",
    "sensitive_keys",
    "types",
]
