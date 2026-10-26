"""AWS Elastic Container Registry provider (BETA).

Exchanges long-lived IAM credentials for a
short-lived registry token via the ECR ``GetAuthorizationToken`` API. The request
is signed with AWS Signature Version 4 implemented by hand (:mod:`hmac` +
:mod:`hashlib`) so no AWS SDK is pulled into the dependency graph.

BETA: this flow is implemented to spec but cannot be live-tested here without
real AWS credentials. The SigV4 canonicalization and the request/response shapes
follow documented AWS behavior; treat results as provisional.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import json
from typing import Any, Dict, List, Optional, Tuple

import httpx

from kowan.models import Registry

from .basic import build_registry_auth
from .provider import Provider

_ECR_SERVICE = "ecr"
# Every ECR control-plane call shares this JSON-1.1 target prefix; the operation
# name is appended (GetAuthorizationToken / DescribeRepositories / ListImages).
_ECR_TARGET_PREFIX = "AmazonEC2ContainerRegistry_V20150921"

# Bounds a single ECR API call.
_REQUEST_TIMEOUT = 20.0

# Cap on the response body we read.
_MAX_BODY = 1 << 20

# Safety cap on paginated browse (DescribeRepositories/ListImages nextToken loop),
# so a huge registry or a misbehaving nextToken can't spin unbounded.
_MAX_BROWSE_PAGES = 50


class ECRProvider(Provider):
    """AWS ECR provider."""

    def __init__(self, reg: Registry) -> None:
        cfg = reg.config or {}
        self._region = cfg.get("region", "")
        self._access_key_id = cfg.get("accessKeyId", "")
        self._secret_access_key = cfg.get("secretAccessKey", "")
        if (
            not self._region
            or not self._access_key_id
            or not self._secret_access_key
        ):
            raise ValueError(
                "ecr: region, accessKeyId and secretAccessKey are required"
            )

    async def docker_auth(self) -> str:
        """Call ECR GetAuthorizationToken and convert the returned token
        (base64 ``AWS:<password>``) into the docker ``X-Registry-Auth`` token,
        scoped to the account's proxy endpoint host."""
        username, password, proxy_endpoint = await self._auth_token()
        server = proxy_endpoint
        if server.startswith("https://"):
            server = server[len("https://"):]
        return build_registry_auth(username, password, server)

    async def credentials(self) -> Tuple[str, str, str]:
        """Exchange the IAM credentials for a short-lived registry token and return
        it as HTTP basic auth (``AWS:<token>``) with the account's proxy endpoint.
        ECR answers the v2 API with direct basic auth (no Bearer challenge)."""
        return await self._auth_token()

    async def validate(self) -> Tuple[bool, Optional[str]]:
        """Attempt the token exchange. ECR is always HTTPS so ``insecure`` is
        False; a non-None error means the credentials or region were rejected."""
        try:
            await self._auth_token()
        except Exception as err:  # noqa: BLE001
            return False, str(err)
        return False, None

    async def list_repositories(self) -> List[str]:
        """List every repository in the account/region via the ECR API.

        ECR does NOT implement the Docker Registry ``/v2/_catalog`` endpoint, so
        the inherited v2 browse cannot see repositories — we call the ECR
        control-plane ``DescribeRepositories`` (SigV4-signed, same signer as
        GetAuthorizationToken) and page through ``nextToken``. Returns the bare
        ``repositoryName`` list."""
        repos: List[str] = []
        token: Optional[str] = None
        for _ in range(_MAX_BROWSE_PAGES):
            payload: Dict[str, Any] = {"maxResults": 100}
            if token:
                payload["nextToken"] = token
            out = await self._ecr_call("DescribeRepositories", payload)
            for r in out.get("repositories") or []:
                name = r.get("repositoryName") if isinstance(r, dict) else None
                if name:
                    repos.append(str(name))
            token = out.get("nextToken") or ""
            if not token:
                break
        return repos

    async def list_tags(self, repository: str) -> List[str]:
        """List the tags of ``repository`` via the ECR ``ListImages`` API.

        (ECR does serve ``/v2/{repo}/tags/list`` too, but we use the same signed
        ECR API as the catalog for one consistent auth path.) Untagged images
        (``imageTag`` absent) are skipped; paged through ``nextToken``."""
        repository = repository.strip().strip("/")
        if repository == "":
            raise ValueError("repository is required")
        tags: List[str] = []
        token: Optional[str] = None
        for _ in range(_MAX_BROWSE_PAGES):
            payload: Dict[str, Any] = {"repositoryName": repository, "maxResults": 100}
            if token:
                payload["nextToken"] = token
            out = await self._ecr_call("ListImages", payload)
            for img in out.get("imageIds") or []:
                tag = img.get("imageTag") if isinstance(img, dict) else None
                if tag:
                    tags.append(str(tag))
            token = out.get("nextToken") or ""
            if not token:
                break
        return tags

    async def _auth_token(self) -> Tuple[str, str, str]:
        """Perform the signed GetAuthorizationToken call and decode the returned
        token into a docker ``(username, password, proxy_endpoint)``."""
        # GetAuthorizationToken takes no parameters (the registry IDs field is
        # deprecated), so the body is an empty JSON object.
        out = await self._ecr_call("GetAuthorizationToken", {})
        data = out.get("authorizationData")
        if not data or not isinstance(data, list) or not data[0].get("authorizationToken"):
            raise ValueError("ecr: empty authorization data in response")

        try:
            raw = base64.b64decode(data[0]["authorizationToken"])
        except Exception as err:  # noqa: BLE001
            raise ValueError(f"ecr: decode authorization token: {err}")
        decoded = raw.decode("utf-8", "replace")
        if ":" not in decoded:
            raise ValueError("ecr: malformed authorization token")
        user, _, passwd = decoded.partition(":")
        return user, passwd, data[0].get("proxyEndpoint", "")

    async def _ecr_call(self, op: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST a SigV4-signed ECR API call (``AmazonEC2ContainerRegistry_V20150921.
        {op}``) with the JSON ``payload`` and return the decoded object. Raises
        ``ValueError`` on transport error or a non-200 status."""
        host = f"api.ecr.{self._region}.amazonaws.com"
        endpoint = "https://" + host + "/"
        target = f"{_ECR_TARGET_PREFIX}.{op}"
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")

        now = datetime.datetime.now(datetime.timezone.utc)
        headers = self._sign_v4(host, body, now, target)

        try:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                resp = await client.post(endpoint, content=body, headers=headers)
        except Exception as err:  # noqa: BLE001
            raise ValueError(f"ecr: call {op}: {err}")

        resp_body = resp.content[:_MAX_BODY]
        if resp.status_code != 200:
            text = resp_body.decode("utf-8", "replace").strip()
            raise ValueError(f"ecr: {op} returned {resp.status_code}: {text}")

        try:
            out = json.loads(resp_body)
        except (ValueError, TypeError) as err:
            raise ValueError(f"ecr: decode {op} response: {err}")
        if not isinstance(out, dict):
            raise ValueError(f"ecr: {op} response was not a JSON object")
        return out

    def _sign_v4(
        self, host: str, payload: bytes, now: datetime.datetime, target: str
    ) -> dict:
        """Sign the request with AWS Signature Version 4 (header-based), returning
        the headers to send. Follows the SigV4 spec: canonical request -> string
        to sign -> signing key derivation -> Authorization header."""
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")

        content_type = "application/x-amz-json-1.1"
        payload_hash = _hex_sha256(payload)

        # Canonical headers must be sorted by lowercased name. We sign a fixed,
        # sorted set covering the headers required by ECR.
        signed_headers = (
            "content-type;host;x-amz-content-sha256;x-amz-date;x-amz-target"
        )
        canonical_headers = (
            "\n".join(
                [
                    "content-type:" + content_type,
                    "host:" + host,
                    "x-amz-content-sha256:" + payload_hash,
                    "x-amz-date:" + amz_date,
                    "x-amz-target:" + target,
                ]
            )
            + "\n"
        )

        canonical_request = "\n".join(
            [
                "POST",
                "/",  # canonical URI
                "",  # canonical query string (none)
                canonical_headers,
                signed_headers,
                payload_hash,
            ]
        )

        scope = "/".join([date_stamp, self._region, _ECR_SERVICE, "aws4_request"])
        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                amz_date,
                scope,
                _hex_sha256(canonical_request.encode("utf-8")),
            ]
        )

        signing_key = self._derive_signing_key(date_stamp)
        signature = _hmac_sha256(
            signing_key, string_to_sign.encode("utf-8")
        ).hex()

        authorization = (
            f"AWS4-HMAC-SHA256 Credential={self._access_key_id}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        return {
            "Content-Type": content_type,
            "X-Amz-Target": target,
            "Host": host,
            "X-Amz-Date": amz_date,
            "X-Amz-Content-Sha256": payload_hash,
            "Authorization": authorization,
        }

    def _derive_signing_key(self, date_stamp: str) -> bytes:
        """Compute the SigV4 signing key:
        ``HMAC(HMAC(HMAC(HMAC("AWS4"+secret, date), region), service),
        "aws4_request")``."""
        k_date = _hmac_sha256(
            ("AWS4" + self._secret_access_key).encode("utf-8"),
            date_stamp.encode("utf-8"),
        )
        k_region = _hmac_sha256(k_date, self._region.encode("utf-8"))
        k_service = _hmac_sha256(k_region, _ECR_SERVICE.encode("utf-8"))
        return _hmac_sha256(k_service, b"aws4_request")


def _hmac_sha256(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def _hex_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
