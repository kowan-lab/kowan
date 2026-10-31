"""Capability dispatch — build the right implementation from a typed connection.

Given a connection (its ``type`` + decrypted ``data`` + non-secret ``meta``) and a
requested capability, return an object that satisfies that capability's method,
reusing the existing provider packages:

  * ``dockerRegistryAuth`` -> ``kowan.registry.new_provider(...)`` (``docker_auth()``)
  * ``secretFetch``        -> ``kowan.secrets.build(type, data)``  (``resolve(field)``)
  * ``messageSend``        -> ``kowan.notify.provider.build(type, data)`` (``send(msg)``)

The caller asks for a *verb*, never branches on the connection type — which is the
whole point of the connection model: one ``aws`` connection answers
``dockerRegistryAuth`` (ECR) *and* ``secretFetch`` (Secrets Manager) *and*
``messageSend`` (SES) from one set of credentials. Cloud identity → facet
derivation (aws/gcp/azure) lands in Phase 5; the dispatch shape is fixed here.

This layer is additive: it reads the existing (credentials) store and builds from
the existing providers, so it changes no current behaviour. Phase 3 rewires the
consumers (deploy pull, notify, secret refs) to go through it.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from . import capabilities as cap


class CapabilityError(Exception):
    """Raised when a connection type cannot satisfy a requested capability."""


def build_capability(
    conn_type: str,
    data: Dict[str, str],
    capability: str,
    meta: Optional[Dict[str, Any]] = None,
) -> Any:
    """Build an implementation of ``capability`` from a connection's type + secret
    ``data`` (+ non-secret ``meta`` for the fields a provider needs that are not
    themselves secret, e.g. a registry url/username). Raises :class:`CapabilityError`
    when the type does not implement the capability.
    """
    meta = meta or {}
    if not cap.implements(conn_type, capability):
        raise CapabilityError(
            f"connection type {conn_type!r} does not implement {capability!r}"
        )

    if capability == cap.CAP_DOCKER_REGISTRY_AUTH:
        return _docker_registry_auth(conn_type, data, meta)
    if capability == cap.CAP_SECRET_FETCH:
        return _secret_fetch(conn_type, data)
    if capability == cap.CAP_MESSAGE_SEND:
        return _message_send(conn_type, data)
    if capability == cap.CAP_GIT_FETCH:
        return _git_fetch(conn_type, data, meta)
    if capability == cap.CAP_HTTP_BEARER:
        # Generic bearer/basic header — provided by the connection's own fields.
        return _http_bearer(conn_type, data, meta)

    # cloudIdentity-derived facets (aws/gcp/azure) — Phase 5.
    raise CapabilityError(
        f"capability {capability!r} has no builder yet (type {conn_type!r})"
    )


# Cloud identity → the registry provider that mints a docker token from it. gcp is
# already the registry type name; aws/azure map onto the ECR/ACR providers, so a
# shared cloud connection backs a container registry with no new AWS/Azure code.
_CLOUD_REGISTRY_TYPE = {"aws": "ecr", "azure": "acr", "gcp": "gcp"}


def _docker_registry_auth(conn_type: str, data: Dict[str, str], meta: Dict[str, Any]) -> Any:
    """A registry Provider (BasicProvider/ECRProvider/…) that exposes ``docker_auth``.

    A cloud connection (aws/azure/gcp) derives docker registry auth by delegating to
    the matching cloud registry provider (ECR/ACR/GCR)."""
    from kowan import registry
    from kowan.models import Registry

    reg_type = _CLOUD_REGISTRY_TYPE.get(conn_type, conn_type)
    pw = data.get("password", "")
    cfg = {k: v for k, v in data.items() if k != "password"}
    reg = Registry(
        type=reg_type,
        name=str(meta.get("name", "") or ""),
        url=str(meta.get("url", "") or ""),
        username=str(meta.get("username", "") or ""),
        password=pw,
        config=cfg,
        insecure=bool(meta.get("insecure", False)),
    )
    return registry.new_provider(reg)


def _secret_fetch(conn_type: str, data: Dict[str, str]) -> Any:
    """An external secret provider (vault/infisical/phase) exposing async ``resolve``."""
    from kowan import secrets

    return secrets.build(conn_type, data)


def _message_send(conn_type: str, data: Dict[str, str]) -> Any:
    """A notify provider (smtp/slack/…) exposing async ``send``."""
    from kowan.notify import provider as notify

    return notify.build(conn_type, data)


def _git_fetch(conn_type: str, data: Dict[str, str], meta: Dict[str, Any]) -> Any:
    """An object exposing ``git_auth(repo_url="") -> (username, token)`` for HTTP git
    authentication, from a git connection's non-secret ``username`` (meta) + secret
    ``token`` (data). Matches the (username, token) pair the git-commit trigger
    sends as HTTP Basic auth, so a connection can back a private clone."""
    # username is non-secret; depending on the create path it lands in data (the
    # connection form encrypts every field) or meta (a registry's identity) — accept
    # either so the builder is robust to both.
    username = str(data.get("username", meta.get("username", "")) or "")
    token = data.get("token") or data.get("password") or data.get("appPassword") or ""

    class _GitAuth:
        def git_auth(self, repo_url: str = "") -> Any:
            return (username, token)

    return _GitAuth()


def _http_bearer(conn_type: str, data: Dict[str, str], meta: Dict[str, Any]) -> Any:
    """A minimal object exposing ``auth_headers()`` from the connection's token/basic
    fields. Used by the workflow HTTP step and generic integrations."""
    import base64

    header = str(meta.get("header", "") or "Authorization")
    token = data.get("token") or data.get("apiToken") or data.get("appToken") or ""
    # username is non-secret (a basic-auth/git connection keeps it in meta); the
    # password is the secret half in data.
    username = data.get("username", meta.get("username", "")) or ""
    password = data.get("password")
    if token:
        scheme = str(meta.get("scheme", "") or "Bearer")
        value = f"{scheme} {token}".strip()
    elif username or password is not None:
        raw = f"{username}:{password or ''}".encode()
        value = "Basic " + base64.b64encode(raw).decode()
    else:
        value = ""

    class _Bearer:
        def auth_headers(self) -> Dict[str, str]:
            return {header: value} if value else {}

    return _Bearer()


async def resolve(store: Any, connection_id: str, capability: str) -> Any:
    """Load a connection from the store by id and build ``capability`` from it.

    ``store`` must expose ``get_credential(id)`` (type/name/meta) and
    ``credential_config(id)`` (decrypted data) — the current credentials store
    already does. Raises :class:`CapabilityError` if the connection cannot satisfy
    the capability.
    """
    cred = store.get_credential(connection_id)
    data = store.credential_config(connection_id)
    meta = dict(cred.meta)
    meta.setdefault("name", cred.name)
    return build_capability(cred.type, data, capability, meta)


async def resolve_service(store: Any, service_id: str, capability: str) -> Any:
    """Build ``capability`` for a service config (a registry/secret/channel row).

    If the service delegates auth to a shared connection (``meta.connectionRef``),
    the secret comes from that **connection** and the service-local, non-secret
    config (a registry url/region, etc.) overlays the connection's identity meta —
    so one connection's credentials back many services. Otherwise the service's own
    inline ``data`` is used. The builder is keyed by
    the *connection's* type when referenced (an ``aws`` connection backing an ECR
    registry derives docker auth from AWS), else the service's own type.
    """
    svc = store.get_credential(service_id)
    svc_meta = dict(svc.meta)
    svc_meta.setdefault("name", svc.name)
    data = {} if svc_meta.get("connectionRef") else store.credential_config(service_id)
    return build_service(store, svc.type, svc_meta, data, capability)


def build_service(store: Any, svc_type: str, svc_meta: Dict[str, Any], data: Dict[str, str],
                  capability: str) -> Any:
    """Build ``capability`` for a service config that may not be stored yet
    (``svc_meta`` with its ``connectionRef``, ``data`` its own secrets), as
    :func:`resolve_service` does for a stored one."""
    ref = svc_meta.get("connectionRef", "") or ""
    if ref:
        conn = store.get_credential(ref)
        conn_data = store.credential_config(ref)
        # Service config (url/region/insecure) overlays the connection identity;
        # drop the ref itself so it doesn't leak into provider construction.
        merged = {**conn.meta, **svc_meta}
        merged.pop("connectionRef", None)
        return build_capability(conn.type, conn_data, capability, merged)
    return build_capability(svc_type, data, capability, svc_meta)
