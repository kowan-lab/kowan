"""Leaf constants, helpers and exceptions shared by the store mixins.

These symbols have no dependency on any mixin or on ``_StoreBase``, so they live
in this tiny leaf module that both :mod:`kowan.store.store` and every mixin can
import at module top level. This avoids the import cycle that would otherwise
force the mixins into per-method deferred imports (the mixins are imported *by*
``store.py``, so they cannot import back from it at module scope).

``store.py`` re-exports these names, so ``from kowan.store.store import ...`` and
``kowan.store.NotFoundError`` keep resolving to the exact same objects.
"""

from __future__ import annotations


# MASK_VALUE is the placeholder the API returns for a sensitive config value and
# accepts back to mean "leave unchanged".
MASK_VALUE = "********"


def bool_to_int(b: bool) -> int:
    """Convert a bool to the 0/1 integer representation stored in SQLite."""
    return 1 if b else 0


def config_keys(config: dict[str, str]) -> list[str]:
    """Sorted key names of a config map."""
    return sorted(config.keys())


def mask_config(sens: dict[str, bool], config: dict[str, str]) -> dict[str, str]:
    """Replace each sensitive non-empty value with :data:`MASK_VALUE`.

    ``sens`` maps a config key to whether it is sensitive (as resolved by each
    mixin's own sensitivity resolver). Shared by channels/registries/auth
    providers, which differ only in how they resolve ``sens``.
    """
    out: dict[str, str] = {}
    for k, v in config.items():
        if sens.get(k) and v != "":
            out[k] = MASK_VALUE
            continue
        out[k] = v
    return out


def merge_config(
    sens: dict[str, bool], existing: dict[str, str], incoming: dict[str, str]
) -> dict[str, str]:
    """Merge ``incoming`` over ``existing`` keeping sensitive blank/masked values.

    Starts from the stored map and overlays ``incoming``, so a key the caller does
    NOT resend keeps its existing value — the edit UI omits untouched sensitive
    fields entirely (blank inputs are dropped from the request, not sent as the
    mask), and rebuilding the map from ``incoming`` alone would silently erase
    them. A sensitive key that IS resent blank or as the mask placeholder likewise
    keeps its stored value. Shared by channels/registries/auth providers, which
    differ only in how they resolve ``sens``.
    """
    out: dict[str, str] = dict(existing)
    for k, v in incoming.items():
        if sens.get(k) and (v == "" or v == MASK_VALUE):
            # Untouched secret: keep the stored value (already in ``out``).
            continue
        out[k] = v
    return out


class StoreError(Exception):
    """Raised for store-layer failures (the message carries the operation context)."""


class NotFoundError(StoreError):
    """Raised when a lookup finds no matching row."""


class CipherNotConfiguredError(StoreError):
    """Raised by secret/auth-provider methods when no cipher is attached.

    A programming/configuration error — the
    server must attach a cipher via :meth:`Store.use_cipher` before serving
    secret operations.
    """
