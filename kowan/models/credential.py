"""Unified credential model.

One record type for every externally-facing credential the control plane manages
centrally, replacing the separate ``Registry`` / ``Secret`` / ``NotificationChannel``
storage models. A credential has:

  * ``kind``   — ``registry`` | ``secret`` | ``channel`` (selects the behaviour).
  * ``type``   — the subtype within the kind (e.g. ``ecr`` / ``vault`` / ``slack``).
  * ``name``   — unique within its kind.
  * ``meta``   — a non-sensitive attribute map, safe to serialize (kind-specific:
                 registry ``url``/``username``/``insecure``/``verified``/``lastError``/
                 ``hasPassword``; channel ``enabled``/``minLevel``; secret ``description``).
  * ``data``   — the sensitive field values (registry password + provider config,
                 channel config, secret fields). Encrypted at rest by the store and
                 NEVER serialized in the clear — only the field NAMES leak, via
                 ``config_keys``.

The typed ``meta`` values are exposed through read-only convenience properties so
callers read ``cred.enabled`` / ``cred.url`` instead of digging into the dict.
"""

from __future__ import annotations

from typing import Any, Dict, List

from pydantic import BaseModel, ConfigDict, Field

# Credential kinds — the top-level discriminator.
#
# ``connection`` is the reusable, typed auth (an AWS account, a Docker login, an
# SMTP server, a Vault instance) that implements one or more *capabilities* (see
# :mod:`kowan.capabilities`). ``registry`` / ``secret`` / ``channel`` are per-service
# configs; each may carry its own inline auth OR reference a
# shared ``connection`` by id via ``meta.connectionRef`` and let it provide the
# auth (see :mod:`kowan.connections`). This is the pragmatic realization of the
# connection model (docs/connections-design.md): services reference shared
# connections by capability instead of re-embedding the same secret everywhere.
CredentialKindConnection = "connection"
CredentialKindRegistry = "registry"
CredentialKindSecret = "secret"
CredentialKindChannel = "channel"

CREDENTIAL_KINDS = (
    CredentialKindConnection,
    CredentialKindRegistry,
    CredentialKindSecret,
    CredentialKindChannel,
)


def valid_credential_kind(kind: str) -> bool:
    """Report whether ``kind`` is one of the recognized credential kinds."""
    return kind in CREDENTIAL_KINDS


class Credential(BaseModel):
    """A centrally-managed, encrypted credential (registry / secret / channel).

    The sensitive ``data`` payload is not a field on this model — it lives in the
    encrypted store column and is surfaced only as field NAMES via
    :attr:`config_keys` (and, for the reveal/masked paths, through dedicated store
    methods). ``meta`` carries the non-sensitive, kind-specific attributes.
    """

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(default="")
    kind: str = Field(default="")  # registry|secret|channel
    type: str = Field(default="")  # subtype within the kind
    name: str = Field(default="")
    meta: Dict[str, Any] = Field(default_factory=dict)
    config_keys: List[str] = Field(default_factory=list, alias="configKeys")
    created_at: str = Field(default="", alias="createdAt")
    updated_at: str = Field(default="", alias="updatedAt")

    # -- convenience read-only views into meta (not extra serialized fields) ---
    @property
    def enabled(self) -> bool:  # channel
        return bool(self.meta.get("enabled", False))

    @property
    def min_level(self) -> str:  # channel
        return str(self.meta.get("minLevel", "") or "")

    @property
    def url(self) -> str:  # registry
        return str(self.meta.get("url", "") or "")

    @property
    def username(self) -> str:  # registry
        return str(self.meta.get("username", "") or "")

    @property
    def insecure(self) -> bool:  # registry
        return bool(self.meta.get("insecure", False))

    @property
    def verified(self) -> bool:  # registry
        return bool(self.meta.get("verified", False))

    @property
    def last_error(self) -> str:  # registry
        return str(self.meta.get("lastError", "") or "")

    @property
    def description(self) -> str:  # secret
        return str(self.meta.get("description", "") or "")

    @property
    def connection_ref(self) -> str:  # service configs (registry/secret/channel)
        """The id of the shared connection this service config delegates auth to,
        or "" when it embeds its own inline auth."""
        return str(self.meta.get("connectionRef", "") or "")
