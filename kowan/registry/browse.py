"""Docker Registry HTTP API v2 browse client (catalog + tags).

:class:`_V2Client` talks to a Docker
Registry v2 endpoint, handling both HTTP basic auth and the Bearer-token
challenge flow (auth.docker.io-style) used by Docker Hub, GHCR and most
registries. It is built from a stored registry's decrypted credentials — resolved
through the per-type :class:`~kowan.registry.provider.Provider` — and used to list
repositories and tags.

Resolving credentials through the provider is what makes cloud registries
browsable: for ECR/GCP/ACR the provider exchanges the stored cloud credentials
for a short-lived registry token (and may override the host, e.g. ECR's account
proxy endpoint), whereas reading ``reg.username``/``reg.password`` directly would
send no credentials (they live in the encrypted config).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from kowan import httpcap
from kowan.models import Registry

from .basic import scheme_and_host
from .provider import new_provider

# Bounds a single browse HTTP call (token exchange or list).
_BROWSE_TIMEOUT = 15.0

# Cap on a response body we will read/parse.
_MAX_BODY = 4 << 20


class _V2Client:
    """Authenticated Docker Registry v2 client."""

    def __init__(
        self,
        scheme: str,
        host: str,
        username: str,
        password: str,
        insecure: bool,
    ) -> None:
        self._scheme = scheme
        self._host = host
        self._username = username
        self._password = password
        # Honour the operator's "insecure" flag on the registry record.
        self._verify = not insecure

    def _basic_auth(self) -> Optional[tuple[str, str]]:
        if self._username != "" or self._password != "":
            return (self._username, self._password)
        return None

    async def get(
        self, path: str, query: Optional[Dict[str, str]], _client: httpcap.Client
    ) -> Any:
        """Authenticated GET of a ``/v2`` path, transparently satisfying a Bearer
        challenge when the registry returns 401 with ``WWW-Authenticate``.

        Returns the decoded JSON. Raises ``ValueError`` on failure / not-found.
        """
        endpoint = f"{self._scheme}://{self._host}{path}"

        resp = await self._do(endpoint, "", query, _client)
        if resp.status_code == 401:
            challenge = resp.headers.get("WWW-Authenticate", "")
            if challenge.lower().startswith("bearer "):
                token = await self._fetch_token(challenge, _client)
                resp = await self._do(endpoint, token, query, _client)
            else:
                raise ValueError(f"registry {self._host}: authentication required")

        body = resp.content
        if resp.status_code == 404:
            raise ValueError("not found")
        if resp.status_code < 200 or resp.status_code >= 300:
            text = body.decode("utf-8", "replace").strip()
            raise ValueError(
                f"registry {self._host}: status {resp.status_code}: {text}"
            )
        if resp.truncated:
            raise ValueError(f"registry {self._host}: response larger than {_MAX_BODY} bytes")
        try:
            return resp.json()
        except ValueError as err:
            raise ValueError(f"registry {self._host}: decode response: {err}")

    async def _do(
        self,
        endpoint: str,
        bearer: str,
        query: Optional[Dict[str, str]],
        client: httpcap.Client,
    ) -> httpcap.Response:
        """Issue one GET with either a Bearer token (when non-empty) or HTTP basic
        auth (when credentials are configured and no token is supplied)."""
        headers = {"Accept": "application/json"}
        auth = None
        if bearer != "":
            headers["Authorization"] = "Bearer " + bearer
        else:
            auth = self._basic_auth()
        try:
            return await client.request("GET", endpoint, limit=_MAX_BODY, headers=headers, params=query, auth=auth)
        except httpcap.HTTPError as err:
            raise ValueError(f"registry {self._host}: {err}") from err

    async def _fetch_token(self, challenge: str, client: httpcap.Client) -> str:
        """Satisfy a ``Bearer realm=...,service=...,scope=...`` challenge by
        requesting a token from the realm (with basic auth when credentials
        exist)."""
        params = _parse_challenge(challenge)
        realm = params.get("realm", "")
        if realm == "":
            raise ValueError(f"registry {self._host}: malformed auth challenge")
        q: Dict[str, str] = {}
        if params.get("service"):
            q["service"] = params["service"]
        if params.get("scope"):
            q["scope"] = params["scope"]

        try:
            resp = await client.request("GET", realm, limit=httpcap.JSON_LIMIT, params=q or None,
                                        auth=self._basic_auth())
        except Exception as err:  # noqa: BLE001
            raise ValueError(f"registry {self._host}: token request: {err}")
        if resp.status_code != 200:
            raise ValueError(
                f"registry {self._host}: token status {resp.status_code}"
            )
        try:
            parsed = resp.json()
        except ValueError as err:
            raise ValueError(f"registry {self._host}: decode token: {err}")
        if isinstance(parsed, dict):
            if parsed.get("token"):
                return str(parsed["token"])
            if parsed.get("access_token"):
                return str(parsed["access_token"])
        raise ValueError(f"registry {self._host}: token response had no token")


def _parse_challenge(challenge: str) -> Dict[str, str]:
    """Parse the comma-separated ``key="value"`` pairs of a WWW-Authenticate
    Bearer header into a map."""
    out: Dict[str, str] = {}
    rest = challenge.strip()
    i = rest.find(" ")
    if i >= 0:
        rest = rest[i + 1:]  # drop the "Bearer" scheme prefix
    for part in rest.split(","):
        kv = part.strip().split("=", 1)
        if len(kv) != 2:
            continue
        out[kv[0].strip()] = kv[1].strip().strip('"')
    return out


async def _new_v2(reg: Registry) -> _V2Client:
    """Build a browse client for ``reg``, resolving effective HTTP credentials
    through the registry's per-type provider."""
    provider = new_provider(reg)
    try:
        username, password, server = await provider.credentials()
    except Exception as err:  # noqa: BLE001
        raise ValueError(f"registry {reg.url}: resolve credentials: {err}")
    if server.strip() == "":
        server = reg.url
    scheme, host = scheme_and_host(server)
    return _V2Client(
        scheme=scheme,
        host=host,
        username=username,
        password=password,
        insecure=reg.insecure,
    )


async def v2_repositories(
    username: str, password: str, server: str, insecure: bool, limit: int = 200
) -> List[str]:
    """List repositories via ``GET /v2/_catalog`` from resolved credentials.

    The provider-agnostic engine behind :meth:`Provider.list_repositories`: takes
    the effective ``(username, password, server)`` a provider already resolved
    (static creds for generic/ghcr, a short-lived token for gcp) plus the
    ``insecure`` flag, and performs the standard v2 catalog call — Bearer challenge
    satisfied transparently. Registries that do not implement ``_catalog`` (ECR)
    override :meth:`Provider.list_repositories` instead of calling this.
    """
    scheme, host = scheme_and_host(server)
    client = _V2Client(scheme, host, username, password, insecure)
    q: Dict[str, str] = {"n": str(limit)} if limit > 0 else {}
    async with httpcap.Client(verify=client._verify, timeout=_BROWSE_TIMEOUT) as http:
        out = await client.get("/v2/_catalog", q or None, http)
    repos = out.get("repositories") if isinstance(out, dict) else None
    return [str(r) for r in repos] if isinstance(repos, list) else []


async def v2_tags(
    username: str, password: str, server: str, repository: str, insecure: bool
) -> List[str]:
    """List tags via ``GET /v2/{repository}/tags/list`` from resolved credentials.

    The engine behind the default :meth:`Provider.list_tags`; see
    :func:`v2_repositories`.
    """
    repository = repository.strip().strip("/")
    if repository == "":
        raise ValueError("repository is required")
    scheme, host = scheme_and_host(server)
    client = _V2Client(scheme, host, username, password, insecure)
    async with httpcap.Client(verify=client._verify, timeout=_BROWSE_TIMEOUT) as http:
        out = await client.get(f"/v2/{repository}/tags/list", None, http)
    tag_list = out.get("tags") if isinstance(out, dict) else None
    return [str(t) for t in tag_list] if isinstance(tag_list, list) else []


async def catalog(reg: Registry, limit: int) -> List[str]:
    """List repositories in a registry via ``GET /v2/_catalog``.

    Not every registry supports the catalog endpoint (notably Docker Hub), in
    which case the error surfaces to the caller. ``limit`` bounds the page size
    (0 = registry default).
    """
    client = await _new_v2(reg)
    q: Dict[str, str] = {}
    if limit > 0:
        q["n"] = str(limit)
    async with httpcap.Client(
        verify=client._verify, timeout=_BROWSE_TIMEOUT
    ) as http:
        out = await client.get("/v2/_catalog", q or None, http)
    repos = out.get("repositories") if isinstance(out, dict) else None
    return list(repos) if repos else []


async def tags(reg: Registry, repo: str) -> List[str]:
    """List the tags of a repository via ``GET /v2/{repo}/tags/list``."""
    repo = repo.strip().strip("/")
    if repo == "":
        raise ValueError("repository is required")
    client = await _new_v2(reg)
    async with httpcap.Client(
        verify=client._verify, timeout=_BROWSE_TIMEOUT
    ) as http:
        out = await client.get(f"/v2/{repo}/tags/list", None, http)
    tag_list = out.get("tags") if isinstance(out, dict) else None
    return list(tag_list) if tag_list else []
