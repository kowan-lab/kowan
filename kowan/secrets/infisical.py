"""Infisical external secret provider.

Authenticates with Machine Identity
"Universal Auth" (clientId/clientSecret -> short-lived access token), then reads a
single secret value via the raw secrets API. Two live HTTP calls (login + read)
each bounded by an 8s timeout and a 1 MiB body cap via :mod:`kowan.httpcap`.
"""

from __future__ import annotations

from typing import Dict
from urllib.parse import quote

from kowan import httpcap

from .provider import FieldSpec, Provider, register

# infisical_timeout bounds the two live HTTP calls of one resolution (seconds).
INFISICAL_TIMEOUT = 8.0

# Cap on each answer's body.
_MAX_BODY = 1 << 20

# InfisicalType is the registered provider/secret type for Infisical.
InfisicalType = "infisical"

# Infisical Cloud base URL, used when a secret omits an explicit address.
INFISICAL_DEFAULT_ADDRESS = "https://app.infisical.com"


class _InfisicalProvider:
    """Holds the connection + reference for one Infisical secret."""

    def __init__(
        self,
        address: str,
        client_id: str,
        client_secret: str,
        project_id: str,
        environment: str,
        secret_path: str,
        key: str,
    ) -> None:
        self._address = address
        self._client_id = client_id
        self._client_secret = client_secret
        self._project_id = project_id
        self._environment = environment
        self._secret_path = secret_path
        self._key = key

    async def resolve(self, field_override: str) -> str:
        """Log in with the machine identity and read the secret value. A non-empty
        ``field_override`` (from ``[[NAME.FIELD]]``) selects a different secret
        name."""
        if not self._client_id or not self._client_secret:
            raise ValueError("infisical: clientId and clientSecret are required")
        if not self._project_id or not self._environment:
            raise ValueError("infisical: projectId and environment are required")
        name = field_override.strip() or self._key.strip()
        if not name:
            raise ValueError("infisical: secret name (key) is required")

        async with httpcap.Client(timeout=INFISICAL_TIMEOUT) as client:
            token = await self._login(client)
            return await self._read_secret(client, token, name)

    async def _login(self, client: httpcap.Client) -> str:
        """Perform the Universal Auth exchange and return a short-lived token."""
        url = self._address + "/api/v1/auth/universal-auth/login"
        try:
            resp = await client.request(
                "POST", url, limit=_MAX_BODY,
                json={"clientId": self._client_id, "clientSecret": self._client_secret},
                headers={"Accept": "application/json"},
            )
        except httpcap.HTTPError as e:
            raise ValueError(f"infisical: login {url}: {e}") from e
        if resp.status_code != 200:
            raise ValueError(
                f"infisical: login: unexpected status {resp.status_code}: {resp.text.strip()}"
            )
        try:
            parsed = resp.json()
        except ValueError as e:
            raise ValueError(f"infisical: decode login response: {e}") from e
        token = (parsed or {}).get("accessToken") or ""
        if not token:
            raise ValueError("infisical: login returned no access token")
        return token

    async def _read_secret(self, client: httpcap.Client, token: str, name: str) -> str:
        """Read a single secret value by name from the raw secrets API."""
        params = {
            "workspaceId": self._project_id,
            "environment": self._environment,
            "secretPath": self._secret_path,
        }
        # Path-escape the secret name so a name with special chars stays a single
        # path segment.
        endpoint = f"{self._address}/api/v3/secrets/raw/{quote(name, safe='')}"
        try:
            resp = await client.request(
                "GET", endpoint, limit=_MAX_BODY,
                params=params,
                headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
            )
        except httpcap.HTTPError as e:
            raise ValueError(f"infisical: read {endpoint}: {e}") from e
        if resp.status_code != 200:
            raise ValueError(
                f"infisical: read {name!r}: unexpected status {resp.status_code}: {resp.text.strip()}"
            )
        try:
            parsed = resp.json()
        except ValueError as e:
            raise ValueError(f"infisical: decode read response: {e}") from e
        return ((parsed or {}).get("secret") or {}).get("secretValue", "") or ""


def _new_infisical_provider(config: Dict[str, str]) -> Provider:
    address = config.get("address", "").strip().rstrip("/") or INFISICAL_DEFAULT_ADDRESS
    secret_path = config.get("secretPath", "").strip() or "/"
    return _InfisicalProvider(
        address=address,
        client_id=config.get("clientId", ""),
        client_secret=config.get("clientSecret", ""),
        project_id=config.get("projectId", ""),
        environment=config.get("environment", ""),
        secret_path=secret_path,
        key=config.get("key", ""),
    )


def register_infisical() -> None:
    """Register Infisical as an external secret provider. Idempotent."""
    register(
        InfisicalType,
        _new_infisical_provider,
        [
            FieldSpec(key="address", label="Address", placeholder=INFISICAL_DEFAULT_ADDRESS),
            FieldSpec(key="clientId", label="Client ID", required=True,
                      placeholder="machine identity client id"),
            FieldSpec(key="clientSecret", label="Client Secret", sensitive=True, required=True),
            FieldSpec(key="projectId", label="Project ID", required=True, placeholder="workspace id"),
            FieldSpec(key="environment", label="Environment", required=True, placeholder="dev"),
            FieldSpec(key="secretPath", label="Secret path", placeholder="/"),
            FieldSpec(key="key", label="Secret name", required=True, placeholder="DATABASE_URL"),
        ],
    )


__all__ = ["InfisicalType", "register_infisical"]
