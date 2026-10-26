"""Single sign-on providers and external identity links.

Provides :class:`_SSOMixin` (tables live in ``_SCHEMA``, kowan/store/store.py).

* ``sso_providers``   one row per configured identity provider (OIDC or the
                      special ``github`` OAuth2 type). The client secret lives in
                      ``secret``, sealed with the store's AES-GCM secrets cipher;
                      ``config`` is JSON for the non-secret knobs (claim mapping,
                      default role, JIT provisioning, ...).
* ``user_identities`` (provider, subject) -> user: the stable link created on
                      first login (JIT provisioning or verified-email linking).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from ._common import MASK_VALUE, NotFoundError

if TYPE_CHECKING:  # pragma: no cover
    from .store import _StoreBase as _Base
else:
    _Base = object



@dataclass
class SSOProvider:
    id: str
    name: str
    type: str  # "oidc" | "github"
    label: str = ""
    icon: str = ""
    issuer: str = ""
    client_id: str = ""
    client_secret: str = ""
    scopes: str = ""
    config: Dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    managed: str = ""  # "env" when bootstrapped from KOWAN_SSO_PROVIDERS
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self, *, mask: bool = True) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "label": self.label,
            "icon": self.icon,
            "issuer": self.issuer,
            "clientId": self.client_id,
            "clientSecret": (MASK_VALUE if self.client_secret else "") if mask else self.client_secret,
            "scopes": self.scopes,
            "config": self.config,
            "enabled": self.enabled,
            "managed": self.managed,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }


_COLS = "id, name, type, label, icon, issuer, client_id, secret, scopes, config, enabled, managed, created_at, updated_at"


class _SSOMixin(_Base):
    def _row_to_provider(self, r: sqlite3.Row, with_secret: bool) -> SSOProvider:
        try:
            cfg = json.loads(r["config"] or "{}")
        except ValueError:
            cfg = {}
        secret = ""
        if r["secret"]:
            if with_secret:
                secret = self._decrypt_data(r["secret"]).get("clientSecret", "")
            else:
                secret = MASK_VALUE  # presence marker only
        return SSOProvider(
            id=r["id"], name=r["name"], type=r["type"], label=r["label"], icon=r["icon"],
            issuer=r["issuer"], client_id=r["client_id"], client_secret=secret, scopes=r["scopes"],
            config=cfg if isinstance(cfg, dict) else {}, enabled=bool(r["enabled"]),
            managed=r["managed"], created_at=r["created_at"], updated_at=r["updated_at"],
        )

    def _seal_secret(self, secret: str) -> Optional[bytes]:
        if not secret:
            return None
        return self._encrypt_data({"clientSecret": secret})

    def create_sso_provider(self, p: SSOProvider) -> None:
        self._execute(
            f"INSERT INTO sso_providers ({_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (p.id, p.name, p.type, p.label, p.icon, p.issuer, p.client_id, self._seal_secret(p.client_secret),
             p.scopes, json.dumps(p.config or {}, separators=(",", ":")), 1 if p.enabled else 0,
             p.managed, p.created_at, p.updated_at),
        )

    def update_sso_provider(self, p: SSOProvider, *, keep_secret: bool) -> None:
        """Update a provider. ``keep_secret`` leaves the stored client secret as
        is (the UI resends the mask / omits the field when untouched)."""
        params: list = [p.name, p.type, p.label, p.icon, p.issuer, p.client_id, p.scopes,
                        json.dumps(p.config or {}, separators=(",", ":")), 1 if p.enabled else 0,
                        p.managed, p.updated_at]
        sql = ("UPDATE sso_providers SET name = ?, type = ?, label = ?, icon = ?, issuer = ?, client_id = ?, "
               "scopes = ?, config = ?, enabled = ?, managed = ?, updated_at = ?")
        if not keep_secret:
            sql += ", secret = ?"
            params.append(self._seal_secret(p.client_secret))
        sql += " WHERE id = ?"
        params.append(p.id)
        self._execute(sql, params)

    def get_sso_provider(self, provider_id: str, *, with_secret: bool = False) -> SSOProvider:
        row = self._one(f"SELECT {_COLS} FROM sso_providers WHERE id = ?", (provider_id,))
        if row is None:
            raise NotFoundError("get sso provider: no rows in result set")
        return self._row_to_provider(row, with_secret)

    def get_sso_provider_by_name(self, name: str, *, with_secret: bool = False) -> SSOProvider:
        row = self._one(f"SELECT {_COLS} FROM sso_providers WHERE name = ?", (name,))
        if row is None:
            raise NotFoundError("get sso provider: no rows in result set")
        return self._row_to_provider(row, with_secret)

    def list_sso_providers(self) -> List[SSOProvider]:
        rows = self._query(f"SELECT {_COLS} FROM sso_providers ORDER BY created_at")
        return [self._row_to_provider(r, False) for r in rows]

    def delete_sso_provider(self, provider_id: str) -> None:
        with self._lock:
            self._execute("DELETE FROM user_identities WHERE provider_id = ?", (provider_id,))
            self._execute("DELETE FROM sso_providers WHERE id = ?", (provider_id,))

    # -- identities ----------------------------------------------------------

    def get_identity_user_id(self, provider_id: str, subject: str) -> str:
        row = self._one(
            "SELECT user_id FROM user_identities WHERE provider_id = ? AND subject = ?", (provider_id, subject)
        )
        if row is None:
            raise NotFoundError("get identity: no rows in result set")
        return row["user_id"]

    def link_identity(self, provider_id: str, subject: str, user_id: str, email: str, now: str) -> None:
        self._execute(
            "INSERT OR REPLACE INTO user_identities (provider_id, subject, user_id, email, created_at, last_login) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (provider_id, subject, user_id, email, now, now),
        )

    def touch_identity(self, provider_id: str, subject: str, email: str, now: str) -> None:
        self._execute(
            "UPDATE user_identities SET last_login = ?, email = ? WHERE provider_id = ? AND subject = ?",
            (now, email, provider_id, subject),
        )

    def list_user_identities(self, user_id: str) -> List[dict]:
        rows = self._query(
            "SELECT i.provider_id, i.subject, i.email, i.created_at, i.last_login, COALESCE(p.label, p.name, '') AS label "
            "FROM user_identities i LEFT JOIN sso_providers p ON p.id = i.provider_id WHERE i.user_id = ?",
            (user_id,),
        )
        return [
            {"providerId": r["provider_id"], "provider": r["label"], "subject": r["subject"], "email": r["email"],
             "createdAt": r["created_at"], "lastLogin": r["last_login"]}
            for r in rows
        ]

    def delete_user_identities(self, user_id: str) -> None:
        self._execute("DELETE FROM user_identities WHERE user_id = ?", (user_id,))
