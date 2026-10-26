"""Per-type registry authentication providers and v2 browse client.

Public surface:

- :func:`new_provider` builds the per-type :class:`Provider`.
- :func:`types` / :func:`fields` / :func:`sensitive_keys` expose the config
  field schema.
- :func:`catalog` / :func:`tags` browse repositories/tags.
- :func:`build_registry_auth` builds the docker ``X-Registry-Auth`` token.
"""

from .basic import build_registry_auth, scheme_and_host
from .browse import catalog, tags
from .provider import (
    FieldSpec,
    Provider,
    TypeSpec,
    fields,
    new_provider,
    sensitive_keys,
    types,
)
from .resolve import docker_auth_for, registry_host_from_ref

__all__ = [
    "Provider",
    "FieldSpec",
    "TypeSpec",
    "new_provider",
    "types",
    "fields",
    "sensitive_keys",
    "catalog",
    "tags",
    "build_registry_auth",
    "scheme_and_host",
    "docker_auth_for",
    "registry_host_from_ref",
]
