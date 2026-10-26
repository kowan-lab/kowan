"""Google Artifact Registry / Container Registry provider (BETA).

Authenticates using a service-account JSON
key, exchanging it for a short-lived OAuth2 access token (cloud-platform scope)
and presenting it as the docker username ``oauth2accesstoken`` with the token as
the password.

There is no allowed Google auth dependency, so the two-legged service-account flow is
hand-rolled here: build and RS256-sign a JWT assertion with :mod:`cryptography`,
then POST it to the key's ``token_uri`` under the
``urn:ietf:params:oauth:grant-type:jwt-bearer`` grant. This matches what
``google.CredentialsFromJSON`` does under the hood for a service-account key.

BETA: implemented to spec but not live-tested without a real service-account key.
Treat results as provisional.
"""

from __future__ import annotations

import base64
import json
import time
from typing import Optional, Tuple

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from kowan.models import Registry

from .basic import build_registry_auth
from .provider import Provider

# The OAuth2 scope required to authenticate to Google's registries.
_GCP_SCOPE = "https://www.googleapis.com/auth/cloud-platform"

# Default token endpoint when the key omits ``token_uri``.
_DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"

# JWT assertion lifetime (seconds). Google caps this at one hour.
_ASSERTION_TTL = 3600

_REQUEST_TIMEOUT = 20.0


class GCPProvider(Provider):
    """Google Artifact/Container Registry provider."""

    def __init__(self, reg: Registry) -> None:
        cfg = reg.config or {}
        json_key = cfg.get("jsonKey", "")
        if not json_key:
            raise ValueError("gcp: jsonKey (service account JSON) is required")
        self._url = reg.url
        self._json_key = json_key

    async def docker_auth(self) -> str:
        """Fetch an access token from the service-account key and build the docker
        ``X-Registry-Auth`` token with username ``oauth2accesstoken``."""
        token = await self._token()
        return build_registry_auth("oauth2accesstoken", token, self._url)

    async def credentials(self) -> Tuple[str, str, str]:
        """Fetch an OAuth2 access token and return it as basic auth with the
        well-known username ``oauth2accesstoken``. Google's registries answer the
        v2 API with a Bearer challenge, which the browse client satisfies by
        presenting these basic credentials to the realm."""
        token = await self._token()
        return "oauth2accesstoken", token, self._url

    async def validate(self) -> Tuple[bool, Optional[str]]:
        """Attempt to fetch a token. Google's registries are always HTTPS so
        ``insecure`` is False; a non-None error means the key was rejected."""
        try:
            await self._token()
        except Exception as err:  # noqa: BLE001
            return False, str(err)
        return False, None

    async def _token(self) -> str:
        """Derive an OAuth2 access token from the service-account JSON key."""
        try:
            key = json.loads(self._json_key)
        except (ValueError, TypeError) as err:
            raise ValueError(f"gcp: parse service account key: {err}")
        if not isinstance(key, dict):
            raise ValueError("gcp: parse service account key: not an object")

        client_email = key.get("client_email", "")
        private_key = key.get("private_key", "")
        token_uri = key.get("token_uri", _DEFAULT_TOKEN_URI)
        if not client_email or not private_key:
            raise ValueError(
                "gcp: parse service account key: missing client_email or private_key"
            )

        assertion = _signed_assertion(client_email, private_key, token_uri)

        try:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                resp = await client.post(
                    token_uri,
                    data={
                        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                        "assertion": assertion,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
        except Exception as err:  # noqa: BLE001
            raise ValueError(f"gcp: fetch access token: {err}")

        if resp.status_code != 200:
            text = resp.text.strip()
            raise ValueError(f"gcp: fetch access token: {resp.status_code}: {text}")
        try:
            out = resp.json()
        except (ValueError, httpx.DecodingError) as err:
            raise ValueError(f"gcp: decode token: {err}")
        access = out.get("access_token", "") if isinstance(out, dict) else ""
        if not access:
            raise ValueError("gcp: empty access token")
        return str(access)


def _b64url(data: bytes) -> str:
    """base64url without padding, per the JWT spec."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _signed_assertion(client_email: str, private_key_pem: str, token_uri: str) -> str:
    """Build an RS256-signed JWT assertion for the two-legged service-account
    flow. ``aud`` is the token endpoint and the requested scope is the
    cloud-platform scope."""
    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    claims = {
        "iss": client_email,
        "scope": _GCP_SCOPE,
        "aud": token_uri,
        "iat": now,
        "exp": now + _ASSERTION_TTL,
    }
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
        + "."
        + _b64url(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    ).encode("ascii")

    try:
        key = serialization.load_pem_private_key(
            private_key_pem.encode("utf-8"), password=None
        )
    except Exception as err:  # noqa: BLE001
        raise ValueError(f"gcp: parse service account key: {err}")
    try:
        signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    except Exception as err:  # noqa: BLE001
        raise ValueError(f"gcp: sign assertion: {err}")

    return signing_input.decode("ascii") + "." + _b64url(signature)
