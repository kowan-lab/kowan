"""OpenID Connect relying party: discovery, JWKS, Authorization Code + PKCE.

Works with any spec-compliant provider (Keycloak, Authentik, Authelia, Dex,
Google, Microsoft Entra ID, ...). Security properties:

* discovery is fetched from ``<issuer>/.well-known/openid-configuration`` and
  its ``issuer`` must equal the configured one (Entra's ``{tenantid}``
  template is resolved against the token's ``tid``);
* the JWKS is cached (TTL) and refreshed when a token names an unknown ``kid``
  (key rotation), at most once per :data:`JWKS_MIN_REFRESH` seconds so a flood
  of forged ``kid`` values cannot turn us into a request amplifier;
* ID tokens: asymmetric algorithms only, signature, ``iss``, ``aud`` (+ ``azp``
  when several audiences), ``exp`` / ``iat`` / ``nbf`` with
  :data:`CLOCK_SKEW` seconds of tolerance, and ``nonce`` (constant time).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlencode

from kowan import httpcap

from . import jose

log = logging.getLogger("kowan.authn.oidc")

CLOCK_SKEW = 60
DISCOVERY_TTL = 3600
JWKS_TTL = 3600
JWKS_MIN_REFRESH = 60
HTTP_TIMEOUT = 10.0
MAX_RESPONSE_BYTES = 1 << 20


class OIDCError(Exception):
    """Any failure in the OIDC flow. ``code`` is a short, log-safe reason."""

    def __init__(self, message: str, code: str = "oidc_error") -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# PKCE / state / nonce
# ---------------------------------------------------------------------------


def new_code_verifier() -> str:
    """RFC 7636 code verifier: 64 random bytes -> 86 url-safe characters."""
    return secrets.token_urlsafe(64)


def code_challenge(verifier: str) -> str:
    """S256 code challenge for ``verifier``."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def consteq(a: str, b: str) -> bool:
    try:
        return hmac.compare_digest((a or "").encode("utf-8"), (b or "").encode("utf-8"))
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

# Builds the client for one exchange (tests swap the network with httpcap.mock).
HttpFactory = Callable[[], httpcap.Client]


def default_http() -> httpcap.Client:
    return httpcap.Client(timeout=HTTP_TIMEOUT, follow_redirects=False,
                          headers={"User-Agent": "kowan-sso/1", "Accept": "application/json"})


async def get_json(http: HttpFactory, url: str, headers: Optional[dict] = None) -> Any:
    async with http() as client:
        try:
            resp = await client.request("GET", url, limit=MAX_RESPONSE_BYTES, headers=headers or {})
        except httpcap.HTTPError as err:
            raise OIDCError(f"GET {url}: {err}", "provider_unreachable") from err
    if resp.status_code != 200:
        raise OIDCError(f"GET {url}: HTTP {resp.status_code}", "provider_error")
    if resp.truncated:
        raise OIDCError(f"GET {url}: response too large", "provider_error")
    try:
        return resp.json()
    except ValueError as err:
        raise OIDCError(f"GET {url}: invalid JSON", "provider_error") from err


async def post_form(http: HttpFactory, url: str, data: dict, auth: Optional[tuple] = None) -> Any:
    async with http() as client:
        try:
            resp = await client.request("POST", url, limit=MAX_RESPONSE_BYTES, data=data, auth=auth,
                                        headers={"Accept": "application/json"})
        except httpcap.HTTPError as err:
            raise OIDCError(f"POST {url}: {err}", "provider_unreachable") from err
    try:
        body = resp.json()
    except ValueError:
        body = None
    if resp.status_code != 200 or not isinstance(body, dict):
        # Never echo the body (it can carry tokens); keep only the OAuth error code.
        err = body.get("error") if isinstance(body, dict) else ""
        raise OIDCError(f"token endpoint: HTTP {resp.status_code} {err or ''}".strip(), "token_exchange_failed")
    if body.get("error"):
        raise OIDCError(f"token endpoint: {body.get('error')}", "token_exchange_failed")
    return body


# ---------------------------------------------------------------------------
# discovery + JWKS
# ---------------------------------------------------------------------------


def _norm_issuer(s: str) -> str:
    return (s or "").strip().rstrip("/")


@dataclass
class Discovery:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    userinfo_endpoint: str = ""
    token_auth_methods: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)


class JWKSCache:
    """Keys by ``kid`` with TTL and rate-limited refresh on unknown kid."""

    def __init__(self, http: HttpFactory, clock: Callable[[], float] = time.time) -> None:
        self._http = http
        self._clock = clock
        self._keys: Dict[str, List[Dict[str, Any]]] = {}
        self._fetched_at = 0.0
        self._last_attempt = -1e18
        self._lock = asyncio.Lock()
        self.fetches = 0

    async def _refresh(self, uri: str) -> None:
        self._last_attempt = self._clock()
        self.fetches += 1
        doc = await get_json(self._http, uri)
        keys: Dict[str, List[Dict[str, Any]]] = {}
        for k in (doc or {}).get("keys", []) if isinstance(doc, dict) else []:
            if not isinstance(k, dict) or k.get("use", "sig") != "sig":
                continue
            keys.setdefault(str(k.get("kid", "")), []).append(k)
        self._keys = keys
        self._fetched_at = self._clock()

    def _find(self, kid: str, alg: str) -> Optional[Dict[str, Any]]:
        if kid:
            cands = self._keys.get(kid, [])
        else:
            cands = [k for ks in self._keys.values() for k in ks]
        cands = [k for k in cands if jose.key_fits_alg(k, alg) and k.get("alg", alg) == alg]
        if not kid and len(cands) != 1:
            return None  # ambiguous without a kid
        return cands[0] if cands else None

    async def key_for(self, uri: str, kid: str, alg: str) -> Any:
        async with self._lock:
            now = self._clock()
            if not self._keys or now - self._fetched_at > JWKS_TTL:
                if now - self._last_attempt >= JWKS_MIN_REFRESH or not self._keys:
                    await self._refresh(uri)
            jwk = self._find(kid, alg)
            if jwk is None and self._clock() - self._last_attempt >= JWKS_MIN_REFRESH:
                # Unknown kid: the provider may have rotated its keys.
                await self._refresh(uri)
                jwk = self._find(kid, alg)
        if jwk is None:
            raise OIDCError("no matching signing key for token", "unknown_key")
        try:
            return jose.jwk_to_key(jwk)
        except jose.JOSEError as err:
            raise OIDCError(f"bad signing key: {err}", "unknown_key") from err


# ---------------------------------------------------------------------------
# client
# ---------------------------------------------------------------------------


class OIDCClient:
    def __init__(
        self,
        issuer: str,
        client_id: str,
        client_secret: str = "",
        *,
        http: Optional[HttpFactory] = None,
        clock: Callable[[], float] = time.time,
        token_auth_method: str = "",
    ) -> None:
        self.issuer = _norm_issuer(issuer)
        self.client_id = client_id
        self.client_secret = client_secret
        self._http = http or default_http
        self._clock = clock
        self._token_auth_method = token_auth_method
        self._discovery: Optional[Discovery] = None
        self._discovery_at = 0.0
        self.jwks = JWKSCache(self._http, clock)

    async def discovery(self) -> Discovery:
        now = self._clock()
        if self._discovery is not None and now - self._discovery_at < DISCOVERY_TTL:
            return self._discovery
        if not self.issuer.startswith(("https://", "http://")):
            raise OIDCError("issuer must be an http(s) URL", "bad_config")
        doc = await get_json(self._http, self.issuer + "/.well-known/openid-configuration")
        if not isinstance(doc, dict):
            raise OIDCError("discovery document is not an object", "provider_error")
        disc_iss = _norm_issuer(str(doc.get("issuer", "")))
        if disc_iss != self.issuer and "{tenantid}" not in disc_iss:
            raise OIDCError(f"discovery issuer {disc_iss!r} does not match {self.issuer!r}", "issuer_mismatch")
        for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            if not doc.get(key):
                raise OIDCError(f"discovery document lacks {key}", "provider_error")
        self._discovery = Discovery(
            issuer=disc_iss,
            authorization_endpoint=str(doc["authorization_endpoint"]),
            token_endpoint=str(doc["token_endpoint"]),
            jwks_uri=str(doc["jwks_uri"]),
            userinfo_endpoint=str(doc.get("userinfo_endpoint") or ""),
            token_auth_methods=list(doc.get("token_endpoint_auth_methods_supported") or []),
            raw=doc,
        )
        self._discovery_at = now
        return self._discovery

    async def authorization_url(
        self, redirect_uri: str, scopes: List[str], state: str, nonce: str, challenge: str,
        extra: Optional[Dict[str, str]] = None,
    ) -> str:
        d = await self.discovery()
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(scopes),
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        params.update(extra or {})
        sep = "&" if "?" in d.authorization_endpoint else "?"
        return d.authorization_endpoint + sep + urlencode(params)

    async def exchange_code(self, code: str, redirect_uri: str, verifier: str) -> Dict[str, Any]:
        d = await self.discovery()
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        }
        method = self._token_auth_method
        if not method:
            methods = d.token_auth_methods
            method = "client_secret_basic" if (not methods or "client_secret_basic" in methods) else "client_secret_post"
        auth = None
        if self.client_secret and method == "client_secret_basic":
            from urllib.parse import quote

            # RFC 6749 2.3.1: form-urlencode id and secret before Basic encoding.
            auth = (quote(self.client_id, safe=""), quote(self.client_secret, safe=""))
        else:
            data["client_id"] = self.client_id
            if self.client_secret:
                data["client_secret"] = self.client_secret
        return await post_form(self._http, d.token_endpoint, data, auth)

    async def validate_id_token(self, id_token: str, nonce: str) -> Dict[str, Any]:
        d = await self.discovery()
        try:
            header, claims, signing_input, sig = jose.parse_jws(id_token)
        except jose.JOSEError as err:
            raise OIDCError(f"id token: {err}", "invalid_id_token") from err
        alg = header.get("alg")
        if alg not in jose.ALLOWED_ALGS:
            raise OIDCError(f"id token: algorithm {alg!r} not allowed", "invalid_id_token")
        if header.get("crit"):
            raise OIDCError("id token: unsupported critical header", "invalid_id_token")
        key = await self.jwks.key_for(d.jwks_uri, str(header.get("kid") or ""), alg)
        if not jose.verify(alg, key, signing_input, sig):
            raise OIDCError("id token: bad signature", "invalid_id_token")

        expected_iss = d.issuer
        if "{tenantid}" in expected_iss:
            expected_iss = expected_iss.replace("{tenantid}", str(claims.get("tid", "")))
        if _norm_issuer(str(claims.get("iss", ""))) != _norm_issuer(expected_iss):
            raise OIDCError("id token: issuer mismatch", "invalid_id_token")

        aud = claims.get("aud")
        auds = [aud] if isinstance(aud, str) else (aud if isinstance(aud, list) else [])
        if self.client_id not in auds:
            raise OIDCError("id token: audience mismatch", "invalid_id_token")
        if len(auds) > 1 and claims.get("azp") != self.client_id:
            raise OIDCError("id token: azp mismatch", "invalid_id_token")
        if "azp" in claims and claims.get("azp") != self.client_id:
            raise OIDCError("id token: azp mismatch", "invalid_id_token")

        now = self._clock()
        exp = claims.get("exp")
        if not isinstance(exp, (int, float)) or now > float(exp) + CLOCK_SKEW:
            raise OIDCError("id token: expired", "invalid_id_token")
        iat = claims.get("iat")
        if isinstance(iat, (int, float)) and float(iat) > now + CLOCK_SKEW:
            raise OIDCError("id token: issued in the future", "invalid_id_token")
        nbf = claims.get("nbf")
        if isinstance(nbf, (int, float)) and float(nbf) > now + CLOCK_SKEW:
            raise OIDCError("id token: not yet valid", "invalid_id_token")
        if not consteq(str(claims.get("nonce", "")), nonce) or not nonce:
            raise OIDCError("id token: nonce mismatch", "invalid_id_token")
        if not claims.get("sub"):
            raise OIDCError("id token: missing sub", "invalid_id_token")
        return claims

    async def userinfo(self, access_token: str) -> Dict[str, Any]:
        d = await self.discovery()
        if not d.userinfo_endpoint:
            return {}
        doc = await get_json(self._http, d.userinfo_endpoint, {"Authorization": "Bearer " + access_token})
        return doc if isinstance(doc, dict) else {}
