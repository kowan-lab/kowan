"""API key CRUD + secret helpers.

Provides :class:`_APIKeyMixin` (folded into :class:`kowan.store.store.Store`) and
the module-level ``generate_api_key_secret`` / ``hash_api_key_secret`` helpers.

The stored hash is the lowercase-hex SHA-256 of the full secret; the secret
itself is never persisted or serialized — only the display Prefix is kept.
"""

from __future__ import annotations

import hashlib
import secrets as _secrets
import sqlite3
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from kowan.models import APIKey

if TYPE_CHECKING:  # pragma: no cover
    from .store import _StoreBase as _Base
else:
    _Base = object


# Human-recognizable scheme prefix for generated API key secrets: the full
# secret is API_KEY_SECRET_PREFIX + 32 random bytes in hex.
API_KEY_SECRET_PREFIX = "pk_"

# Number of leading characters of the secret retained for display, e.g.
# "pk_1a2b3c4d": the 3-char scheme prefix plus 8 hex digits.
API_KEY_PREFIX_LEN = 11


def generate_api_key_secret() -> tuple[str, str]:
    """Return a fresh API key secret and its display prefix.

    The secret is "pk_" followed by 32 random bytes in hex; the prefix is its
    first API_KEY_PREFIX_LEN characters. The caller stores
    :func:`hash_api_key_secret` of the secret and shows the full secret once.
    """
    secret = API_KEY_SECRET_PREFIX + _secrets.token_hex(32)
    return secret, secret[:API_KEY_PREFIX_LEN]


def hash_api_key_secret(secret: str) -> str:
    """Return the lowercase-hex SHA-256 of a presented secret. This is the value
    stored in api_keys and looked up on verification."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _row_to_api_key(row: sqlite3.Row) -> APIKey:
    """Build an APIKey from a select of the DISPLAY columns (never the hash).

    Column order: id, name, prefix, role, created_at, last_used, team_id.
    """
    return APIKey(
        id=row["id"],
        name=row["name"],
        prefix=row["prefix"],
        role=row["role"],
        createdAt=row["created_at"],
        lastUsed=row["last_used"],
        teamId=row["team_id"],
    )


class _APIKeyMixin(_Base):
    def create_api_key(self, key: APIKey, hash: str) -> None:
        """Insert a new API key. ``hash`` is the SHA-256 hex of the full secret
        (see :func:`hash_api_key_secret`); the secret itself is never persisted."""
        self._execute(
            "INSERT INTO api_keys (id, name, hash, prefix, role, created_at, last_used, team_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (key.id, key.name, hash, key.prefix, key.role, key.created_at, key.last_used or "",
             key.team_id or ""),
        )

    def list_api_keys(self) -> list[APIKey]:
        """Return all API keys ordered by creation time. The stored hash is never
        returned; only display fields (including Prefix) are populated."""
        rows = self._query(
            "SELECT id, name, prefix, role, created_at, last_used, team_id FROM api_keys ORDER BY created_at"
        )
        return [_row_to_api_key(r) for r in rows]

    def get_api_key(self, id: str) -> APIKey:
        """Return one key's display fields by id (never the hash)."""
        from ._common import NotFoundError

        row = self._one(
            "SELECT id, name, prefix, role, created_at, last_used, team_id FROM api_keys WHERE id = ?", (id,)
        )
        if row is None:
            raise NotFoundError("get api key: no rows in result set")
        return _row_to_api_key(row)

    def get_api_key_by_hash(self, hash: str) -> Optional[APIKey]:
        """Return the key whose stored SHA-256 equals ``hash`` (None if absent).

        Used by kowan.auth.authorize to load a key's team/scopes; ``hash`` is the
        value the auth middleware already computed, never the secret."""
        if not hash:
            return None
        row = self._one(
            "SELECT id, name, prefix, role, created_at, last_used, team_id FROM api_keys WHERE hash = ?", (hash,)
        )
        return _row_to_api_key(row) if row is not None else None

    def delete_api_key(self, id: str) -> None:
        """Remove an API key by id (and its narrowed-scope grants, if any)."""
        self._execute("DELETE FROM api_keys WHERE id = ?", (id,))
        self.delete_subject_grants("apikey", id)

    def verify_api_key(self, presented: str) -> tuple[Optional[APIKey], bool]:
        """Hash the presented secret and look up the matching row.

        On a match returns ``(key, True)`` and best-effort stamps last_used with
        the current time; a no-match returns ``(None, False)``. An empty presented
        secret never matches. Failures raise StoreError; a clean no-match is
        ``(None, False)``.
        """
        if presented == "":
            return None, False
        h = hash_api_key_secret(presented)
        row = self._one(
            "SELECT id, name, prefix, role, created_at, last_used, team_id FROM api_keys WHERE hash = ?",
            (h,),
        )
        if row is None:
            return None, False
        key = _row_to_api_key(row)

        # Best-effort last_used stamp: a failure here must not reject an otherwise
        # valid key, so the error is swallowed.
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Throttle the stamp to once per minute per key: it used to be a write +
        # commit on EVERY api-key-authenticated request (polling clients, CI).
        # The minute prefix of the RFC3339 string is enough to compare.
        if (key.last_used or "")[:16] == now[:16]:
            return key, True
        try:
            self._execute("UPDATE api_keys SET last_used = ? WHERE id = ?", (now, key.id))
            key.last_used = now
        except Exception:  # noqa: BLE001 — a stamp failure is ignored
            pass
        return key, True
