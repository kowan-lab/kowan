"""Capability contracts — what a typed credential (connection) can *do*.

This is the foundation of the "connection" model (see ``docs/connections-design.md``).
A **connection** is a typed credential (aws, docker-registry-login, github, vault,
smtp, …); a **capability** is an interface it *implements* — a verb the platform
can perform *with* it (mint a docker auth token, fetch a secret, send a message,
authenticate a git fetch, attach a bearer header, …).

The point of capabilities is to split two facts that today's ``(kind, type)`` pair
conflates: *"what is this credential"* (its ``type``) from *"what am I doing with
it"* (the capability the call site asks for). One ``aws`` connection can implement
several capabilities (registry auth for ECR, secret fetch for Secrets Manager,
message send for SES, object store for S3) — one credential, many verbs — which is
impossible to express when a credential *is* "a registry".

Phase 0 introduces only the vocabulary: the capability names, their Protocol
signatures (for typing/docs), and a **manifest** declaring which capabilities each
currently-implemented connection type satisfies. It changes no behaviour — the
existing registry/notify/secret providers already implement these methods; here we
merely name and declare them so later phases can dispatch by capability.
"""

from __future__ import annotations

from typing import Any, Dict, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Capability names (stable string ids, used as manifest keys and in the API).
# ---------------------------------------------------------------------------

CAP_DOCKER_REGISTRY_AUTH = "dockerRegistryAuth"
CAP_SECRET_FETCH = "secretFetch"
CAP_MESSAGE_SEND = "messageSend"
CAP_GIT_FETCH = "gitFetch"
CAP_GIT_API = "gitApi"
CAP_HTTP_BEARER = "httpBearer"
CAP_OBJECT_STORE = "objectStore"
CAP_SSH_ACCESS = "sshAccess"
CAP_TLS_CLIENT_AUTH = "tlsClientAuth"
# Base capability: cloud connections (aws/gcp/azure) implement this and *derive*
# the leaf capabilities above from a short-lived provider session. Not consumed by
# a feature directly.
CAP_CLOUD_IDENTITY = "cloudIdentity"

ALL_CAPABILITIES = (
    CAP_DOCKER_REGISTRY_AUTH,
    CAP_SECRET_FETCH,
    CAP_MESSAGE_SEND,
    CAP_GIT_FETCH,
    CAP_GIT_API,
    CAP_HTTP_BEARER,
    CAP_OBJECT_STORE,
    CAP_SSH_ACCESS,
    CAP_TLS_CLIENT_AUTH,
    CAP_CLOUD_IDENTITY,
)

# Human-readable descriptions (surfaced by the API / UI capability badges).
CAPABILITY_LABELS: Dict[str, str] = {
    CAP_DOCKER_REGISTRY_AUTH: "Docker registry authentication",
    CAP_SECRET_FETCH: "External secret fetch",
    CAP_MESSAGE_SEND: "Message / notification delivery",
    CAP_GIT_FETCH: "Git clone/fetch authentication",
    CAP_GIT_API: "Git provider API (webhooks, status)",
    CAP_HTTP_BEARER: "Outbound HTTP authentication",
    CAP_OBJECT_STORE: "Object storage (blob get/put/list)",
    CAP_SSH_ACCESS: "SSH access",
    CAP_TLS_CLIENT_AUTH: "TLS client certificate (mTLS)",
    CAP_CLOUD_IDENTITY: "Cloud identity (derives other capabilities)",
}


# ---------------------------------------------------------------------------
# Capability Protocols — the method a connection exposes for each capability.
# Structural (Protocol) so an existing provider satisfies one without inheriting.
# ---------------------------------------------------------------------------

@runtime_checkable
class DockerRegistryAuth(Protocol):
    async def docker_auth(self, registry_url: str = "") -> str:
        """Return the base64 ``X-Registry-Auth`` token for pulling from a registry."""
        ...


@runtime_checkable
class SecretFetch(Protocol):
    async def fetch(self, field: str = "") -> str:
        """Fetch a secret value (optionally selecting a field/key) from the backend."""
        ...


@runtime_checkable
class MessageSend(Protocol):
    async def send(self, msg: Any) -> None:
        """Deliver a message (title/body/level) over the transport."""
        ...


@runtime_checkable
class GitFetch(Protocol):
    def git_auth(self, repo_url: str = "") -> Any:
        """Return the credentials/transport for authenticating a git clone/fetch."""
        ...


@runtime_checkable
class HttpBearer(Protocol):
    def auth_headers(self) -> Dict[str, str]:
        """Return the HTTP headers that authenticate an outbound request."""
        ...


@runtime_checkable
class CloudIdentity(Protocol):
    async def session(self) -> Any:
        """Mint a short-lived provider session used to derive leaf capabilities."""
        ...


# ---------------------------------------------------------------------------
# Manifest: which capabilities each currently-implemented connection type has.
#
# Grounded in the code as it exists today (Phase 0). Cloud registry types
# (ecr/gcp/acr) are declared with dockerRegistryAuth for now; when they become the
# first-class aws/gcp/azure *cloud* connections (Phase 5) they will implement
# cloudIdentity and DERIVE dockerRegistryAuth + the other facets. Static secret
# types (generic/git/env/token) are intentionally ABSENT — they are opaque values,
# not connections (see docs/connections-design.md §6.3).
# ---------------------------------------------------------------------------

_MANIFEST: Dict[str, frozenset] = {
    # registry family -> docker registry auth
    "generic": frozenset({CAP_DOCKER_REGISTRY_AUTH, CAP_HTTP_BEARER}),
    "ghcr": frozenset({CAP_DOCKER_REGISTRY_AUTH, CAP_HTTP_BEARER}),
    "ecr": frozenset({CAP_DOCKER_REGISTRY_AUTH}),
    "gcp": frozenset({CAP_DOCKER_REGISTRY_AUTH}),
    "acr": frozenset({CAP_DOCKER_REGISTRY_AUTH}),
    # external secret backends -> secret fetch (+ they are http-token bearing)
    "vault": frozenset({CAP_SECRET_FETCH, CAP_HTTP_BEARER}),
    "infisical": frozenset({CAP_SECRET_FETCH, CAP_HTTP_BEARER}),
    "phase": frozenset({CAP_SECRET_FETCH, CAP_HTTP_BEARER}),
    # native connection types (no pre-existing provider family): git hosts and
    # generic HTTP auth. Their field schemas live in kowan.connection_types. gitFetch
    # authenticates a git clone/fetch (consumed today by the git-commit trigger via a
    # [[secret]] that delegates to one of these); httpBearer attaches an auth header.
    # gitApi (webhooks/status) is intentionally ABSENT — no builder/consumer exists
    # yet, so declaring it would be a lie (see docs/connections-design.md §6.5).
    "git-http": frozenset({CAP_GIT_FETCH, CAP_HTTP_BEARER}),
    "github": frozenset({CAP_GIT_FETCH, CAP_HTTP_BEARER}),
    "gitlab": frozenset({CAP_GIT_FETCH, CAP_HTTP_BEARER}),
    "bitbucket": frozenset({CAP_GIT_FETCH, CAP_HTTP_BEARER}),
    "api-token": frozenset({CAP_HTTP_BEARER}),
    "basic-auth": frozenset({CAP_HTTP_BEARER}),
    # notification transports -> message send
    "smtp": frozenset({CAP_MESSAGE_SEND}),
    "webhook": frozenset({CAP_MESSAGE_SEND, CAP_HTTP_BEARER}),
    "ntfy": frozenset({CAP_MESSAGE_SEND, CAP_HTTP_BEARER}),
    "telegram": frozenset({CAP_MESSAGE_SEND}),
    "slack": frozenset({CAP_MESSAGE_SEND}),
    "discord": frozenset({CAP_MESSAGE_SEND}),
    # cloud identities -> implement cloudIdentity and DERIVE the leaf capabilities.
    # dockerRegistryAuth works today (reuses the ECR/ACR providers); secretFetch /
    # messageSend / objectStore (Secrets Manager / SES / S3 …) are Phase-5 facets.
    "aws": frozenset({CAP_CLOUD_IDENTITY}),
    "azure": frozenset({CAP_CLOUD_IDENTITY}),
    # gcp is both the registry type name and the cloud identity — upgrade it to a
    # full cloud identity (still implements dockerRegistryAuth, now via derivation).
    "gcp": frozenset({CAP_CLOUD_IDENTITY}),
}


def capabilities_of(conn_type: str) -> frozenset:
    """Return the set of capability names a connection type implements (empty for
    an unknown/static type)."""
    return _MANIFEST.get(conn_type, frozenset())


def implements(conn_type: str, capability: str) -> bool:
    """Report whether ``conn_type`` implements ``capability`` (directly, or — once
    cloud derivation lands — via its cloudIdentity base)."""
    caps = _MANIFEST.get(conn_type, frozenset())
    if capability in caps:
        return True
    # A cloud connection projects the leaf capabilities from its cloudIdentity.
    if CAP_CLOUD_IDENTITY in caps and capability in _CLOUD_DERIVED:
        return True
    return False


# Leaf capabilities a cloud connection can ACTUALLY derive today. Only docker
# registry auth is wired (it reuses the ECR/ACR/GCR providers). Secrets Manager
# (secretFetch), SES (messageSend) and S3 (objectStore) are future cloud facets —
# deliberately NOT listed, so a cloud connection is never offered for a capability
# it cannot yet serve. This keeps the capability pickers honest: they only surface
# connections that will really work.
_CLOUD_DERIVED = frozenset({CAP_DOCKER_REGISTRY_AUTH})


def effective_capabilities(conn_type: str) -> frozenset:
    """The full set of capabilities a type can serve, INCLUDING the leaf ones a
    cloud identity derives (aws/azure/gcp → dockerRegistryAuth). Use this — not the
    raw manifest set — for the API catalog and capability filters, so a cloud
    connection is offered wherever it actually works."""
    caps = _MANIFEST.get(conn_type, frozenset())
    if CAP_CLOUD_IDENTITY in caps:
        return caps | _CLOUD_DERIVED
    return caps
