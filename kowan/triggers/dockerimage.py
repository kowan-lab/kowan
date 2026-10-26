"""Docker image manifest-digest polling trigger.

Fires when a tag's manifest digest
changes (i.e. the tag was re-pushed / rebuilt), resolved via the Registry HTTP
API v2 WITHOUT ever pulling the image.

The digest resolution performs a HEAD on the manifest, the registry's
anonymous-or-Basic Bearer token handshake on a 401, and a GET + local sha256
fallback when the registry omits the ``Docker-Content-Digest`` header. HTTP uses
``httpx`` (redirects followed so a registry's storage backend redirect works).
"""

from __future__ import annotations

import hashlib
import json
from typing import Dict, Optional, Tuple

import httpx

from kowan.httpcap import request_capped

from . import FieldSpec, KindPoll, Poller, TypeDockerImage, register

# Bounds the live HTTP calls (HEAD + optional token exchange + optional GET
# fallback) of one digest resolution so a slow registry cannot stall the poller.
_DOCKER_IMAGE_TIMEOUT = 20.0

# Docker Hub's Registry HTTP API v2 host, used when an image reference carries no
# explicit registry (e.g. "nginx:latest").
_DEFAULT_DOCKER_REGISTRY = "registry-1.docker.io"

# Advertises every manifest media type we can resolve a digest for: multi-arch
# manifest lists / OCI indexes plus single v2/OCI manifests. Registries echo the
# chosen type's digest in Docker-Content-Digest.
_DOCKER_MANIFEST_ACCEPT = (
    "application/vnd.docker.distribution.manifest.list.v2+json,"
    "application/vnd.oci.image.index.v1+json,"
    "application/vnd.docker.distribution.manifest.v2+json,"
    "application/vnd.oci.image.manifest.v1+json"
)

_MANIFEST_BODY_LIMIT = 4 << 20


class _DockerImagePoller(Poller):
    """Parsed image reference plus optional credentials for one trigger."""

    def __init__(
        self,
        registry: str,
        repository: str,
        reference: str,
        username: str,
        password: str,
    ) -> None:
        self.registry = registry
        self.repository = repository
        self.reference = reference
        self.username = username
        self.password = password

    async def evaluate(self, config: Dict[str, str], state: str) -> Tuple[bool, str]:
        """Resolve the current manifest digest and report whether it changed.

        On first run (state == "") it records the digest and reports
        ``fired=False`` so attaching the trigger never fires for past history.
        """
        async with httpx.AsyncClient(
            timeout=_DOCKER_IMAGE_TIMEOUT, follow_redirects=True
        ) as client:
            digest = await self._resolve_digest(client)
        if state == "":
            return False, digest
        return digest != state, digest

    async def _resolve_digest(self, client: httpx.AsyncClient) -> str:
        manifest_url = (
            f"https://{self.registry}/v2/{self.repository}/manifests/{self.reference}"
        )

        resp = await self._do(client, "HEAD", manifest_url, "")
        # On a 401 carrying a Bearer challenge, obtain a token and retry once.
        if resp.status_code == 401:
            challenge = resp.headers.get("WWW-Authenticate", "")
            token = await self._fetch_token(client, challenge)
            resp = await self._do(client, "HEAD", manifest_url, token)

        if resp.status_code < 200 or resp.status_code >= 300:
            raise ValueError(
                f"docker-image: HEAD {manifest_url}: unexpected status {resp.status_code}"
            )

        digest = resp.headers.get("Docker-Content-Digest")
        if digest:
            return digest

        # Fallback: some registries omit the digest header on HEAD; GET the
        # manifest and hash the raw body (the digest is sha256 over the bytes).
        token = _auth_token(resp.request)
        get_resp = await self._do(client, "GET", manifest_url, token)
        raw = get_resp.content[:_MANIFEST_BODY_LIMIT]
        if get_resp.status_code < 200 or get_resp.status_code >= 300:
            snippet = raw.decode("utf-8", "replace").strip()
            raise ValueError(
                f"docker-image: GET {manifest_url}: unexpected status {get_resp.status_code}: {snippet}"
            )
        digest = get_resp.headers.get("Docker-Content-Digest")
        if digest:
            return digest
        return "sha256:" + hashlib.sha256(raw).hexdigest()

    async def _do(
        self, client: httpx.AsyncClient, method: str, manifest_url: str, token: str
    ) -> httpx.Response:
        """Issue one manifest request with the manifest Accept header and, when
        ``token`` is non-empty, a Bearer Authorization header."""
        headers = {"Accept": _DOCKER_MANIFEST_ACCEPT}
        if token:
            headers["Authorization"] = "Bearer " + token
        try:
            return await client.request(method, manifest_url, headers=headers)
        except httpx.HTTPError as err:
            raise ValueError(f"docker-image: {method} {manifest_url}: {err}") from err

    async def _fetch_token(self, client: httpx.AsyncClient, challenge: str) -> str:
        """Perform the registry token handshake for this poller's credentials.

        Thin wrapper over the shared :func:`_fetch_registry_token` so the browse
        API can reuse the identical Bearer-challenge flow."""
        return await _fetch_registry_token(
            client, challenge, self.username, self.password
        )


async def _fetch_registry_token(
    client: httpx.AsyncClient, challenge: str, username: str, password: str
) -> str:
    """Perform the registry token handshake described by a
    "Bearer realm=..,service=..,scope=.." challenge, adding HTTP Basic auth
    when credentials are configured. Both registry-1.docker.io and ghcr.io
    use this flow even for public images.

    Module-level (not a method) so both the manifest poller and the registry
    browse API drive the exact same handshake with whatever credentials they
    hold."""
    if not challenge.strip().lower().startswith("bearer "):
        raise ValueError(
            f"docker-image: unauthorized without a Bearer challenge: {challenge!r}"
        )
    params = _parse_challenge(challenge.strip()[len("bearer ") :])
    realm = params.get("realm", "")
    if not realm:
        raise ValueError(
            f"docker-image: token challenge missing realm: {challenge!r}"
        )

    query: Dict[str, str] = {}
    if params.get("service"):
        query["service"] = params["service"]
    if params.get("scope"):
        query["scope"] = params["scope"]

    auth: Optional[Tuple[str, str]] = None
    if username or password:
        auth = (username, password)
    try:
        resp = await request_capped(
            client, "GET", realm, 1 << 20,
            params=query,
            headers={"Accept": "application/json"},
            auth=auth,
        )
    except httpx.HTTPError as err:
        raise ValueError(f"docker-image: token {realm}: {err}") from err
    raw = resp.content[: 1 << 20]
    if resp.status_code < 200 or resp.status_code >= 300:
        snippet = raw.decode("utf-8", "replace").strip()
        raise ValueError(
            f"docker-image: token {realm}: unexpected status {resp.status_code}: {snippet}"
        )
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as err:
        raise ValueError(f"docker-image: decode token response: {err}") from err
    token = parsed.get("token") or parsed.get("access_token") or ""
    if not token:
        raise ValueError("docker-image: token response carried no token")
    return token


def _auth_token(req: Optional[httpx.Request]) -> str:
    """Extract the Bearer token previously set on a request so the GET fallback
    can reuse the token already negotiated for the HEAD."""
    if req is None:
        return ""
    h = req.headers.get("Authorization", "")
    if h.startswith("Bearer "):
        return h[len("Bearer ") :]
    return ""


def _parse_challenge(s: str) -> Dict[str, str]:
    """Parse the comma-separated key="value" pairs of a WWW-Authenticate Bearer
    challenge into a dict. Values may be quoted and a scope can itself contain
    commas (handled by only splitting on commas outside quotes)."""
    out: Dict[str, str] = {}
    key_chars: list[str] = []
    val_chars: list[str] = []
    in_key = True
    in_quotes = False

    def flush() -> None:
        nonlocal in_key
        k = "".join(key_chars).strip()
        if k:
            out[k.lower()] = "".join(val_chars).strip().strip('"')
        key_chars.clear()
        val_chars.clear()
        in_key = True

    for r in s:
        if r == '"':
            in_quotes = not in_quotes
            if not in_key:
                val_chars.append(r)
        elif r == "=" and in_key and not in_quotes:
            in_key = False
        elif r == "," and not in_quotes:
            flush()
        else:
            if in_key:
                key_chars.append(r)
            else:
                val_chars.append(r)
    flush()
    return out


def _new_docker_image(config: Dict[str, str]) -> Poller:
    """Build the poller from an already-resolved config: a registry host, a
    repository and a tag, plus optional credentials.

    The credentials AND the registry host come from a referenced registry
    connection (``dockerRegistryAuth``); the poller in
    ``kowan/workflow/poller.py`` resolves that connection into ``config["registry"]``
    / ``["username"]`` / ``["password"]`` before calling ``triggers.build``. With
    no connection the registry defaults to Docker Hub (``registry-1.docker.io``)
    and access is anonymous. Parsing is cheap here; the network round trips live
    in evaluate."""
    repository = (config.get("repository") or "").strip().strip("/")
    if not repository:
        raise ValueError("docker-image: repository is required")
    registry = (config.get("registry") or "").strip() or _DEFAULT_DOCKER_REGISTRY
    reference = (config.get("tag") or "").strip() or "latest"
    # Docker Hub official images live under the implicit "library/" namespace.
    if registry == _DEFAULT_DOCKER_REGISTRY and "/" not in repository:
        repository = "library/" + repository
    return _DockerImagePoller(
        registry=registry,
        repository=repository,
        reference=reference,
        username=config.get("username", ""),
        password=config.get("password", ""),
    )


register(
    TypeDockerImage,
    KindPoll,
    "Docker image",
    "Fires when an image tag's manifest digest changes in its registry (a new push to the same tag), resolved via the Registry v2 API without pulling.",
    _new_docker_image,
    [
        # Credentials (and the registry host) come from a shared registry
        # connection (dockerRegistryAuth), not inline username/password. The
        # poller resolves the referenced connection into config registry/username/
        # password before building the poller. When absent, the registry defaults
        # to Docker Hub and access is anonymous (public images).
        FieldSpec(
            key="connectionRef",
            label="Registry",
            placeholder="(Docker Hub / public if unset)",
            connection={"capability": "dockerRegistryAuth", "optional": True},
        ),
        FieldSpec(key="repository", label="Repository", required=True, placeholder="library/nginx or myorg/api"),
        FieldSpec(key="tag", label="Tag", required=True, placeholder="latest"),
    ],
)
