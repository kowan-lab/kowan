"""Image reference and digest helpers shared by the self-update orchestrator
(:mod:`kowan.updater`) and image update detection (:mod:`kowan.api.updates`).

``_split_ref`` parses an image reference, ``_running_digest`` picks the digest
a local image was pulled by, ``_remote_digest`` asks the registry for a tag's
current digest (the docker-image trigger's manifest HEAD + Bearer handshake),
and ``_registry_creds`` finds stored credentials for a registry host
(anonymous when none match).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Tuple


log = logging.getLogger("kowan.agentupdate")

# Docker Hub's registry API host, for references without a registry.
_DEFAULT_DOCKER_REGISTRY = "registry-1.docker.io"


def _agent_image_default() -> str:
    """The agent image name used when the running container doesn't report one."""
    return os.environ.get("KOWAN_AGENT_IMAGE", "").strip() or "ghcr.io/kowan-lab/kowan-agent"


def _split_ref(ref: str) -> Tuple[str, str, str]:
    """Split an image reference into ``(registry_host, repository, tag)``.

    A leading segment containing ``.``/``:`` (or ``localhost``) is the registry
    host; otherwise the image targets Docker Hub (host ``""``). A trailing
    ``@digest`` is ignored; the tag defaults to ``latest``.
    """
    at = ref.find("@")
    if at >= 0:
        ref = ref[:at]
    host = ""
    rest = ref
    slash = ref.find("/")
    if slash >= 0:
        first = ref[:slash]
        if first == "localhost" or "." in first or ":" in first:
            host = first
            rest = ref[slash + 1:]
    tag = "latest"
    colon = rest.rfind(":")
    if colon > rest.rfind("/"):
        tag = rest[colon + 1:]
        rest = rest[:colon]
    return host, rest, tag


def _running_digest(image_inspect: Dict[str, Any], repository: str) -> str:
    """Extract the running image's manifest digest from an inspect's RepoDigests.

    Prefers the entry whose repo matches ``repository`` (ignoring the registry host
    prefix), falling back to the first — a locally-built image with no RepoDigests
    yields ``""`` (nothing to compare against)."""
    repo_digests = image_inspect.get("RepoDigests") or []
    fallback = ""
    for rd in repo_digests:
        repo, sep, dig = str(rd).partition("@")
        if not sep:
            continue
        if not fallback:
            fallback = dig
        # Match on the repo suffix so "reg.io/team/x" matches "team/x".
        if repo == repository or repo.endswith("/" + repository):
            return dig
    return fallback


async def _remote_digest(store: Any, image_ref: str) -> str:
    """Resolve the registry's current manifest digest for ``image_ref``.

    Reuses the docker-image trigger's poller (manifest HEAD + Bearer handshake).
    Credentials come from a stored registry matching the image host, else anonymous.
    """
    from kowan.triggers.dockerimage import _new_docker_image

    host, repository, tag = _split_ref(image_ref)
    if host in ("", "docker.io", "index.docker.io"):
        api_host = _DEFAULT_DOCKER_REGISTRY
    else:
        api_host = host
    username, password = await _registry_creds(store, host or "docker.io")
    poller = _new_docker_image(
        {
            "registry": api_host,
            "repository": repository,
            "tag": tag,
            "username": username,
            "password": password,
        }
    )
    # evaluate(state="") records and returns the current digest (fired=False).
    _, digest = await poller.evaluate({}, "")
    return digest


async def _registry_creds(store: Any, host: str) -> Tuple[str, str]:
    """Best-effort ``(username, password)`` for pulling/reading a private agent
    image, from a stored registry whose URL matches ``host`` (else anonymous)."""
    try:
        reg = store.registry_view_by_host(host)
    except Exception:  # noqa: BLE001 - no matching registry -> anonymous
        return "", ""
    try:
        from kowan import capabilities as caps
        from kowan import connections

        provider = await connections.resolve_service(
            store, reg.id, caps.CAP_DOCKER_REGISTRY_AUTH
        )
        username, password, _ = await provider.credentials()
        return username, password
    except Exception as err:  # noqa: BLE001 - fall back to anonymous
        log.debug("agent update: registry creds for %s unavailable: %s", host, err)
        return "", ""
