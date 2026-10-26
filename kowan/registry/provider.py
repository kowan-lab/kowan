"""Per-type authentication providers for image registries.

Each provider knows how to (a) build
the base64 ``X-Registry-Auth`` token the Docker Engine expects for an
authenticated pull (:meth:`Provider.docker_auth`), (b) resolve the effective HTTP
credentials for talking to the Docker Registry HTTP API v2 directly
(:meth:`Provider.credentials`, used to browse repositories/tags), and (c)
validate connectivity/credentials while detecting insecure endpoints
(:meth:`Provider.validate`).

Providers are constructed from a :class:`kowan.models.Registry` whose ``config``
has already been decrypted by the store. The generic and ghcr providers use
static basic auth and are fully verifiable. The ecr, gcp and acr providers
perform cloud token exchange and are marked BETA: their flows are implemented but
cannot be live-tested here without real cloud credentials.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from kowan.models import Registry, normalize_registry_type
from kowan.models.registry import (
    RegistryTypeACR,
    RegistryTypeECR,
    RegistryTypeGCP,
    RegistryTypeGeneric,
    RegistryTypeGHCR,
)
from kowan.registry.fieldspec import FieldSpec


class Provider(abc.ABC):
    """A per-type registry authentication provider."""

    @abc.abstractmethod
    async def docker_auth(self) -> str:
        """Return the base64 ``X-Registry-Auth`` JSON token for an authenticated
        pull against this registry, or ``""`` for anonymous access."""
        raise NotImplementedError

    @abc.abstractmethod
    async def credentials(self) -> Tuple[str, str, str]:
        """Resolve the effective HTTP credentials for the v2 API.

        Returns ``(username, password, server_address)``. For static providers
        these are the stored username/password; for cloud providers they are the
        freshly exchanged short-lived credentials. ``server_address`` may be
        empty, in which case the caller falls back to the stored URL.
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def validate(self) -> Tuple[bool, Optional[str]]:  # pragma: no cover
        """Test connectivity/credentials.

        Returns ``(insecure, error)``:
        ``insecure`` is True for plain-HTTP / untrusted-TLS endpoints, and
        ``error`` is a message string when the registry is unreachable or rejects
        the credentials (``None`` on success). Both are meaningful at once — e.g.
        an http registry that fails to connect reports ``insecure=True`` with a
        non-None error — so, unlike a raised exception, the insecure flag is never
        lost on failure.
        """
        raise NotImplementedError

    # -- repository/tag browse ----------------------------------------------
    #
    # Concrete (non-abstract) so every provider gets a working default. The
    # default speaks the Docker Registry HTTP API v2 (``/v2/_catalog`` and
    # ``/v2/{repo}/tags/list``) using whatever credentials this provider resolves
    # — correct for generic/ghcr (static basic auth) and gcp (short-lived token +
    # Bearer challenge). Cloud registries that DON'T implement the v2 catalog, or
    # that need a non-standard token exchange, override these: ECR (no ``_catalog``
    # — uses ``DescribeRepositories``/``ListImages``) and ACR (per-scope Bearer
    # tokens from the refresh-token exchange).

    async def list_repositories(self) -> List[str]:
        """List repository names in this registry (``[]`` when none/unsupported)."""
        from .browse import v2_repositories

        username, password, server = await self.credentials()
        return await v2_repositories(
            username, password, server, self._browse_insecure()
        )

    async def list_tags(self, repository: str) -> List[str]:
        """List the tags of ``repository`` (``[]`` when none)."""
        from .browse import v2_tags

        username, password, server = await self.credentials()
        return await v2_tags(
            username, password, server, repository, self._browse_insecure()
        )

    def _browse_insecure(self) -> bool:
        """Whether v2 browse should skip TLS verification. Providers that carry an
        ``insecure`` registry flag set ``self._insecure``; cloud providers are
        always HTTPS and leave it False."""
        return bool(getattr(self, "_insecure", False))


@dataclass
class TypeSpec:
    """Public schema for one registry type, exposed via the API.

    ``beta`` marks cloud providers whose token exchange is implemented but not
    live-tested.
    """

    type: str
    label: str
    beta: bool = False
    fields: List[FieldSpec] = field(default_factory=list)

    def model_dump(self, by_alias: bool = True) -> dict:
        return {
            "type": self.type,
            "label": self.label,
            "beta": self.beta,
            "fields": [f.to_dict() for f in self.fields],
        }


# The ordered field schema for every registry type. The order is intentional
# (generic first) so the UI lists them sensibly.
_TYPE_SPECS: List[TypeSpec] = [
    TypeSpec(
        type=RegistryTypeGeneric,
        label="Generic (basic auth)",
        fields=[
            FieldSpec(key="username", label="Username", placeholder="registry user"),
            FieldSpec(
                key="password",
                label="Password",
                sensitive=True,
                placeholder="registry password or token",
            ),
        ],
    ),
    TypeSpec(
        type=RegistryTypeGHCR,
        label="GitHub Container Registry",
        fields=[
            FieldSpec(
                key="username",
                label="Username",
                required=True,
                placeholder="github username",
            ),
            FieldSpec(
                key="password",
                label="Token (PAT)",
                sensitive=True,
                required=True,
                placeholder="ghp_...",
            ),
        ],
    ),
    TypeSpec(
        type=RegistryTypeECR,
        label="AWS ECR",
        beta=True,
        fields=[
            FieldSpec(
                key="region", label="Region", required=True, placeholder="us-east-1"
            ),
            FieldSpec(
                key="accessKeyId",
                label="Access Key ID",
                sensitive=True,
                required=True,
                placeholder="AKIA...",
            ),
            FieldSpec(
                key="secretAccessKey",
                label="Secret Access Key",
                sensitive=True,
                required=True,
            ),
        ],
    ),
    TypeSpec(
        type=RegistryTypeGCP,
        label="Google Artifact/Container Registry",
        beta=True,
        fields=[
            FieldSpec(
                key="jsonKey",
                label="Service Account JSON Key",
                sensitive=True,
                required=True,
                placeholder='{"type":"service_account",...}',
            ),
        ],
    ),
    TypeSpec(
        type=RegistryTypeACR,
        label="Azure Container Registry",
        beta=True,
        fields=[
            FieldSpec(key="tenantId", label="Tenant ID", required=True),
            FieldSpec(key="clientId", label="Client ID", required=True),
            FieldSpec(
                key="clientSecret",
                label="Client Secret",
                sensitive=True,
                required=True,
            ),
        ],
    ),
]


def types() -> List[TypeSpec]:
    """Return the field schema for every registry type, in display order."""
    return list(_TYPE_SPECS)


def fields(reg_type: str) -> List[FieldSpec]:
    """Return the field schema for a single registry type.

    Unknown types fall back to the generic schema so callers always get a usable
    shape.
    """
    for ts in _TYPE_SPECS:
        if ts.type == reg_type:
            return ts.fields
    if reg_type != RegistryTypeGeneric:
        return fields(RegistryTypeGeneric)
    return []


def sensitive_keys(reg_type: str) -> dict[str, bool]:
    """Return the set of Config keys marked sensitive for a registry type.

    The store uses this (via ``set_registry_sensitivity``) to mask config on read
    and preserve secrets on update when left blank. For generic/ghcr the password
    lives in the dedicated password column, not in Config, so the set is empty for
    those types.
    """
    out: dict[str, bool] = {}
    for f in fields(reg_type):
        if f.sensitive:
            out[f.key] = True
    return out


def new_provider(reg: Registry) -> Provider:
    """Build the :class:`Provider` for a registry.

    ``reg.type`` selects the implementation (defaulting to generic); ``reg.config``
    must already be decrypted by the caller. Raises ``ValueError`` for a disabled
    or unknown type.
    """
    # Local imports avoid an import cycle: the concrete providers import this
    # module for the shared Provider base / helpers.
    from .acr import ACRProvider
    from .basic import BasicProvider
    from .ecr import ECRProvider
    from .gcp import GCPProvider

    reg_type = normalize_registry_type(reg.type)
    if reg_type in (RegistryTypeGeneric, RegistryTypeGHCR):
        return BasicProvider(reg)
    if reg_type == RegistryTypeECR:
        return ECRProvider(reg)
    if reg_type == RegistryTypeGCP:
        return GCPProvider(reg)
    if reg_type == RegistryTypeACR:
        return ACRProvider(reg)
    raise ValueError(f"registry: unknown type {reg.type!r}")
