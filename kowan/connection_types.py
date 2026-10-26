"""Native connection types — the field schemas for connections that have no
pre-existing provider family.

Registry, external-secret and notify connection types borrow their create-form
field schema (and sensitive-key set) from their owning provider package. The git
hosts and generic HTTP-auth connections have no such package: they are
"connection-native", declared here as a small static catalog so the API can render
their create form and the store knows which fields to mask.

Field-key convention (kept aligned with the capability builders in
``kowan.connections``):

  * git-http/github/gitlab/bitbucket — non-secret ``username`` (-> ``meta``), secret
    ``token`` (-> ``data``). ``gitFetch`` returns ``(username, token)`` for HTTP
    basic auth; ``httpBearer`` presents the token as a bearer header.
  * api-token — secret ``token`` (-> ``data``), optional non-secret ``header`` /
    ``scheme`` (-> ``meta``) for the outbound header shape.
  * basic-auth — non-secret ``username`` (-> ``meta``), secret ``password``
    (-> ``data``).

The capability declarations for these types live in ``kowan.capabilities`` (the
single manifest); this module owns only the field shape and sensitivity, so the
two concerns stay separate.
"""

from __future__ import annotations

from typing import Dict, List

from kowan.registry.fieldspec import FieldSpec

# type -> ordered field schema. Sensitive fields are stored encrypted and masked.
_NATIVE: Dict[str, List[FieldSpec]] = {
    "git-http": [
        FieldSpec(key="username", label="Username", placeholder="(for private repos)"),
        FieldSpec(key="token", label="Token / password", sensitive=True, required=True),
    ],
    "github": [
        FieldSpec(key="username", label="Username", placeholder="(optional)"),
        FieldSpec(key="token", label="Personal access token", sensitive=True, required=True,
                  placeholder="ghp_…"),
    ],
    "gitlab": [
        FieldSpec(key="username", label="Username", placeholder="(optional)"),
        FieldSpec(key="token", label="Access token", sensitive=True, required=True,
                  placeholder="glpat-…"),
    ],
    "bitbucket": [
        FieldSpec(key="username", label="Username", required=True),
        FieldSpec(key="token", label="App password", sensitive=True, required=True),
    ],
    "api-token": [
        FieldSpec(key="token", label="Token", sensitive=True, required=True),
        FieldSpec(key="header", label="Header name", placeholder="Authorization"),
        FieldSpec(key="scheme", label="Scheme", placeholder="Bearer"),
    ],
    "basic-auth": [
        FieldSpec(key="username", label="Username", required=True),
        FieldSpec(key="password", label="Password", sensitive=True, required=True),
    ],
}


def is_native(conn_type: str) -> bool:
    """Whether ``conn_type`` is a connection-native type declared here."""
    return conn_type in _NATIVE


def fields(conn_type: str) -> List[FieldSpec]:
    """Return a copy of ``conn_type``'s field schema, or [] if not native."""
    return list(_NATIVE.get(conn_type, ()))


def sensitive_keys(conn_type: str) -> Dict[str, bool]:
    """The config keys marked sensitive for ``conn_type`` (masked on read /
    preserved on blank update)."""
    return {f.key: True for f in _NATIVE.get(conn_type, ()) if f.sensitive}


def types() -> List[Dict]:
    """Every native type with its serialised field schema, for the API's connection
    type catalog (capabilities are spliced in by the caller from the manifest)."""
    return [
        {"type": t, "fields": [f.to_dict() for f in specs]}
        for t, specs in _NATIVE.items()
    ]


__all__ = ["is_native", "fields", "sensitive_keys", "types"]
