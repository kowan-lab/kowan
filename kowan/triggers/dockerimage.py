"""Docker image manifest-digest polling trigger.

Fires when a tag's manifest digest
changes (i.e. the tag was re-pushed / rebuilt), resolved via the Registry HTTP
API v2 WITHOUT ever pulling the image.

The digest resolution performs a HEAD on the manifest, the registry's
anonymous-or-Basic Bearer token handshake on a 401 (or plain Basic auth when the
challenge asks for it, as ECR does), and a GET + local sha256
fallback when the registry omits the ``Docker-Content-Digest`` header. HTTP goes
through :mod:`kowan.httpcap` (redirects followed so a registry's storage
backend redirect works; bodies capped).
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Dict, Optional, Tuple

from kowan import httpcap

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
        async with httpcap.Client(
            timeout=_DOCKER_IMAGE_TIMEOUT, follow_redirects=True
        ) as client:
            digest = await self._resolve_digest(client)
        if state == "":
            return False, digest
        return digest != state, digest

    async def _resolve_digest(self, client: httpcap.Client) -> str:
        manifest_url = (
            f"https://{self.registry}/v2/{self.repository}/manifests/{self.reference}"
        )

        auth = ""
        resp = await self._do(client, "HEAD", manifest_url, auth)
        # On a 401, answer the challenge and retry once.
        if resp.status_code == 401:
            challenge = resp.headers.get("WWW-Authenticate", "")
            auth = await self._authorization(client, challenge, manifest_url)
            resp = await self._do(client, "HEAD", manifest_url, auth)

        if resp.status_code < 200 or resp.status_code >= 300:
            raise ValueError(
                f"docker-image: HEAD {manifest_url}: unexpected status {resp.status_code}"
            )

        digest = resp.headers.get("Docker-Content-Digest")
        if digest:
            return digest

        # Fallback: some registries omit the digest header on HEAD; GET the
        # manifest and hash the raw body (the digest is sha256 over the bytes),
        # reusing the authorization already negotiated for the HEAD.
        get_resp = await self._do(client, "GET", manifest_url, auth)
        raw = get_resp.content
        if get_resp.status_code < 200 or get_resp.status_code >= 300:
            snippet = raw[:512].decode("utf-8", "replace").strip()
            raise ValueError(
                f"docker-image: GET {manifest_url}: unexpected status {get_resp.status_code}: {snippet}"
            )
        digest = get_resp.headers.get("Docker-Content-Digest")
        if digest:
            return digest
        if get_resp.truncated:
            # A hash of a cut manifest would be a wrong digest.
            raise ValueError(f"docker-image: GET {manifest_url}: manifest larger than {_MANIFEST_BODY_LIMIT} bytes")
        return "sha256:" + hashlib.sha256(raw).hexdigest()

    async def _do(
        self, client: httpcap.Client, method: str, manifest_url: str, auth: str
    ) -> httpcap.Response:
        """Issue one manifest request with the manifest Accept header and, when
        ``auth`` is non-empty, that Authorization header."""
        headers = {"Accept": _DOCKER_MANIFEST_ACCEPT}
        if auth:
            headers["Authorization"] = auth
        try:
            return await client.request(method, manifest_url, limit=_MANIFEST_BODY_LIMIT, headers=headers)
        except httpcap.HTTPError as err:
            raise ValueError(f"docker-image: {method} {manifest_url}: {err}") from err

    async def _authorization(self, client: httpcap.Client, challenge: str, manifest_url: str) -> str:
        """The Authorization header answering a 401's ``challenge``.

        A Basic challenge (ECR, some self-hosted registries) takes the
        credentials as they are: for ECR that is the ``AWS:<token>`` pair its
        API hands out. Without credentials there is nothing to answer with, so
        it is reported as the 401 it is. Any other challenge goes through the
        shared Bearer handshake (:func:`_fetch_registry_token`), the same flow
        the browse API uses."""
        if challenge.strip().lower().startswith("basic"):
            if not (self.username or self.password):
                raise ValueError(f"docker-image: HEAD {manifest_url}: unexpected status 401 (the registry wants credentials)")
            pair = f"{self.username}:{self.password}".encode("utf-8")
            return "Basic " + base64.b64encode(pair).decode("ascii")
        token = await _fetch_registry_token(client, challenge, self.username, self.password)
        return "Bearer " + token


async def _fetch_registry_token(
    client: httpcap.Client, challenge: str, username: str, password: str
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
        resp = await client.request(
            "GET", realm, limit=1 << 20,
            params=query,
            headers={"Accept": "application/json"},
            auth=auth,
        )
    except httpcap.HTTPError as err:
        raise ValueError(f"docker-image: token {realm}: {err}") from err
    raw = resp.content
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
