"""The single entry point for resolving a Docker ``X-Registry-Auth`` token.

Every place that pulls an image (manual pull, container create, deployment,
build/push) needs the same answer: "what auth token do I send for this image?".
Historically each call site reimplemented it — and the deploy path even skipped
the per-type Provider, so ecr/gcp/acr deployments authenticated with an empty
token. This module is that one canonical resolver.

The flow is always: pick the registry record (explicit id, else by the image
reference's host) -> build its per-type :class:`~kowan.registry.Provider` via
:func:`new_provider` -> ``await provider.docker_auth()``. Because it goes through
the Provider, cloud registries exchange their stored secrets for a short-lived
token exactly like static registries return their basic-auth token. Anything
unresolvable (no matching registry, unknown type, provider/network error) yields
``""`` so the pull falls back to anonymous — the historical behaviour.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("kowan.registry")


def registry_host_from_ref(ref: str) -> str:
    """Extract the registry host from an image reference.

    The host is the portion before the
    first slash when that portion contains a "." or ":" (or is "localhost");
    otherwise the reference targets Docker Hub and "docker.io" is returned. A
    trailing ``@digest`` is stripped first so it is not mistaken for a
    ``host:port`` separator.

        "ghcr.io/org/app:tag" -> "ghcr.io"
        "nginx"               -> "docker.io"
        "host:5000/x"         -> "host:5000"
        "localhost/x"         -> "localhost"
    """
    docker_hub = "docker.io"
    at = ref.find("@")
    if at >= 0:
        ref = ref[:at]
    slash = ref.find("/")
    if slash < 0:
        return docker_hub
    candidate = ref[:slash]
    if candidate == "localhost" or "." in candidate or ":" in candidate:
        return candidate
    return docker_hub


async def docker_auth_for(store: Any, ref: str = "", registry_id: str = "") -> str:
    """Resolve the base64 ``X-Registry-Auth`` token for pulling ``ref``.

    When ``registry_id`` is set that registry is used; otherwise the store is
    consulted for a registry whose host matches ``ref``. Resolution goes through
    the capability dispatch (:func:`kowan.connections.resolve_service`): if the
    registry delegates to a shared **connection** (``meta.connectionRef``) the
    ``dockerRegistryAuth`` token is minted from that connection's credentials;
    otherwise the registry's own inline auth is used — so ecr/gcp/acr registries
    mint a fresh short-lived token instead of authenticating anonymously. Returns
    ``""`` (anonymous pull) when no registry matches or on any error.
    """
    from kowan import capabilities as caps
    from kowan import connections

    cred_id = registry_id
    if not cred_id and ref:
        try:
            cred_id = store.registry_view_by_host(registry_host_from_ref(ref)).id
        except Exception:  # noqa: BLE001 - NotFound et al. -> anonymous
            return ""
    if not cred_id:
        return ""
    try:
        impl = await connections.resolve_service(
            store, cred_id, caps.CAP_DOCKER_REGISTRY_AUTH
        )
        return (await impl.docker_auth()) or ""
    except Exception as err:  # noqa: BLE001 - any provider/network failure -> anonymous
        log.warning(
            "registry auth failed; falling back to anonymous pull registry=%s error=%s",
            cred_id, err,
        )
        return ""
