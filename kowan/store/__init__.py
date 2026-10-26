"""Persistent storage for the Kowan control plane (SQLite via stdlib sqlite3).

The public surface is the
:class:`Store` class, which aggregates all CRUD via per-module mixins.
"""

from .apikeys import (
    API_KEY_PREFIX_LEN,
    API_KEY_SECRET_PREFIX,
    generate_api_key_secret,
    hash_api_key_secret,
)
from .store import (
    MASK_VALUE,
    CipherNotConfiguredError,
    NotFoundError,
    Store,
    StoreError,
    bool_to_int,
    open_store,
)

__all__ = [
    "Store",
    "open_store",
    "StoreError",
    "NotFoundError",
    "CipherNotConfiguredError",
    "MASK_VALUE",
    "bool_to_int",
    "generate_api_key_secret",
    "hash_api_key_secret",
    "API_KEY_SECRET_PREFIX",
    "API_KEY_PREFIX_LEN",
]
