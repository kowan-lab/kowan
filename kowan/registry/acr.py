"""Azure Container Registry provider (BETA).

Authenticates against ACR using an Azure AD
service principal (client-credentials grant): it first obtains an AAD access
token for the ARM/ACR audience, then exchanges it at the registry's
``/oauth2/exchange`` endpoint for an ACR refresh token, presented as the docker
password with the well-known username
``00000000-0000-0000-0000-000000000000``.

BETA: implemented to spec but not live-tested without a real Azure service
principal and registry. Treat results as provisional.
"""

from __future__ import annotations

from typing import Any, List, Optional, Tuple

import httpx

from kowan.models import Registry

from .basic import build_registry_auth, scheme_and_host
from .provider import Provider

# The constant username ACR expects when authenticating with a refresh/identity
# token.
_ACR_DOCKER_USERNAME = "00000000-0000-0000-0000-000000000000"

# The resource scope used for the client-credentials token; the container
# registry service accepts ARM-issued tokens for the exchange.
_ACR_AAD_SCOPE = "https://management.azure.com/.default"

# Bounds a single HTTP exchange.
_REQUEST_TIMEOUT = 20.0

# Cap on the body we read from a token response.
_MAX_BODY = 1 << 20


class ACRProvider(Provider):
    """Azure Container Registry provider."""

    def __init__(self, reg: Registry) -> None:
        cfg = reg.config or {}
        self._url = reg.url
        self._tenant_id = cfg.get("tenantId", "")
        self._client_id = cfg.get("clientId", "")
        self._client_secret = cfg.get("clientSecret", "")
        if not self._tenant_id or not self._client_id or not self._client_secret:
            raise ValueError(
                "acr: tenantId, clientId and clientSecret are required"
            )

    async def docker_auth(self) -> str:
        """Obtain an ACR refresh token and build the docker ``X-Registry-Auth``
        token with the constant ACR username."""
        refresh = await self._refresh_token()
        _, host = scheme_and_host(self._url)
        return build_registry_auth(_ACR_DOCKER_USERNAME, refresh, host)

    async def credentials(self) -> Tuple[str, str, str]:
        """Perform the AAD -> ACR exchange and return the identity (refresh) token
        as basic auth with the constant ACR username."""
        refresh = await self._refresh_token()
        return _ACR_DOCKER_USERNAME, refresh, self._url

    async def validate(self) -> Tuple[bool, Optional[str]]:
        """Attempt the full token exchange. ACR is always HTTPS so ``insecure`` is
        False; a non-None error means the principal or registry was rejected."""
        try:
            await self._refresh_token()
        except Exception as err:  # noqa: BLE001
            return False, str(err)
        return False, None

    async def list_repositories(self) -> List[str]:
        """List repositories via ACR's ``GET /v2/_catalog``.

        ACR *does* speak the Docker Registry v2 API, but with its own token flow:
        the refresh token from the AAD exchange is swapped at ``/oauth2/token`` for
        a short-lived ACCESS token scoped ``registry:catalog:*``, presented as a
        Bearer. (The inherited v2 browse can't do this — its Bearer handshake
        expects a Basic-auth realm, not ACR's refresh-token grant.)"""
        _, host = scheme_and_host(self._url)
        access = await self._access_token(host, "registry:catalog:*")
        out = await _bearer_get_json(f"https://{host}/v2/_catalog", access)
        repos = out.get("repositories") if isinstance(out, dict) else None
        return [str(r) for r in repos] if isinstance(repos, list) else []

    async def list_tags(self, repository: str) -> List[str]:
        """List a repository's tags via ACR's ``GET /v2/{repo}/tags/list`` with a
        Bearer access token scoped ``repository:{repo}:metadata_read``."""
        repository = repository.strip().strip("/")
        if repository == "":
            raise ValueError("repository is required")
        _, host = scheme_and_host(self._url)
        access = await self._access_token(
            host, f"repository:{repository}:metadata_read"
        )
        out = await _bearer_get_json(
            f"https://{host}/v2/{repository}/tags/list", access
        )
        tags = out.get("tags") if isinstance(out, dict) else None
        return [str(t) for t in tags] if isinstance(tags, list) else []

    async def _access_token(self, host: str, scope: str) -> str:
        """Swap the ACR refresh token for a short-lived ACCESS token scoped to
        ``scope`` at ``/oauth2/token`` (the ACR registry token endpoint). Returns
        the ``access_token`` used as a Bearer against the v2 API."""
        refresh = await self._refresh_token()
        token_url = f"https://{host}/oauth2/token"
        form = {
            "grant_type": "refresh_token",
            "service": host,
            "scope": scope,
            "refresh_token": refresh,
        }
        body, status = await _do_request(token_url, form)
        if status != 200:
            raise ValueError(
                f"acr: access token returned {status}: "
                f"{body.decode('utf-8', 'replace').strip()}"
            )
        out = _json(body)
        access = out.get("access_token", "") if isinstance(out, dict) else ""
        if not access:
            raise ValueError("acr: empty access token")
        return str(access)

    async def _refresh_token(self) -> str:
        """Two-step AAD -> ACR exchange, returning the ACR refresh token used as
        the docker password."""
        aad = await self._aad_token()
        return await self._exchange(aad)

    async def _aad_token(self) -> str:
        """OAuth2 client-credentials grant against Azure AD."""
        token_url = (
            f"https://login.microsoftonline.com/{self._tenant_id}"
            "/oauth2/v2.0/token"
        )
        form = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "scope": _ACR_AAD_SCOPE,
        }
        body, status = await _do_request(token_url, form)
        if status != 200:
            raise ValueError(
                f"acr: aad token returned {status}: {body.decode('utf-8', 'replace').strip()}"
            )
        out = _json(body)
        access = out.get("access_token", "") if isinstance(out, dict) else ""
        if not access:
            raise ValueError("acr: empty aad access token")
        return str(access)

    async def _exchange(self, aad_token: str) -> str:
        """Swap an AAD access token for an ACR refresh token at the registry's
        ``/oauth2/exchange`` endpoint."""
        _, host = scheme_and_host(self._url)
        exchange_url = f"https://{host}/oauth2/exchange"
        form = {
            "grant_type": "access_token",
            "service": host,
            "tenant": self._tenant_id,
            "access_token": aad_token,
        }
        body, status = await _do_request(exchange_url, form)
        if status != 200:
            raise ValueError(
                f"acr: exchange returned {status}: {body.decode('utf-8', 'replace').strip()}"
            )
        out = _json(body)
        refresh = out.get("refresh_token", "") if isinstance(out, dict) else ""
        if not refresh:
            raise ValueError("acr: empty refresh token")
        return str(refresh)


async def _bearer_get_json(url: str, access_token: str) -> Any:
    """GET ``url`` with a Bearer access token and return the decoded JSON body.

    Bounded like the token exchanges (timeout + body cap). Raises ``ValueError``
    on transport error or a non-2xx status."""
    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            resp = await client.get(
                url,
                headers={
                    "Authorization": "Bearer " + access_token,
                    "Accept": "application/json",
                },
            )
    except Exception as err:  # noqa: BLE001
        raise ValueError(str(err))
    body = resp.content[:_MAX_BODY]
    if resp.status_code < 200 or resp.status_code >= 300:
        raise ValueError(
            f"acr: {url} returned {resp.status_code}: "
            f"{body.decode('utf-8', 'replace').strip()}"
        )
    return _json(body)


async def _do_request(url: str, form: dict[str, str]) -> Tuple[bytes, int]:
    """Execute a POST form request with a bounded timeout, returning the (capped)
    body and status code. Used for the ACR form-encoded POSTs.

    A transport error raises ``ValueError`` with the underlying message.
    """
    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            resp = await client.post(
                url,
                data=form,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
    except Exception as err:  # noqa: BLE001
        raise ValueError(str(err))
    return resp.content[:_MAX_BODY], resp.status_code


def _json(body: bytes):
    import json

    try:
        return json.loads(body)
    except (ValueError, TypeError) as err:
        raise ValueError(f"acr: decode response: {err}")
