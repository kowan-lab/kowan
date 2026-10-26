"""Hand-rolled HS256 JSON Web Tokens.

No pyjwt in the target stack, so signing/verification is built directly on
``hmac``/``hashlib`` with base64url encoding.

Claim names:

    sub       registered subject  -> the user's ID
    iat       registered issued-at (unix seconds)
    nbf       registered not-before (unix seconds)
    exp       registered expiry (unix seconds)
    username  custom
    role      custom

The default token TTL is 24h.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass

# The lifetime of an issued token: 24 hours, in seconds.
DEFAULT_TTL_SECONDS = 24 * 60 * 60


class JWTError(Exception):
    """Raised when a token fails to parse or validate."""


@dataclass
class Claims:
    """The decoded claims of a validated token.

    ``sub`` is the user ID (the registered subject claim); ``username`` and
    ``role`` are the custom claims. Times are unix seconds. ``jti`` identifies
    this token (for logout) and ``ver`` is the user's session version when it was
    issued (bumped to end all of a user's sessions); see kowan.auth.sessions.
    """

    sub: str
    username: str
    role: str
    iat: int
    nbf: int
    exp: int
    jti: str = ""
    ver: int = 0


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


class JWT:
    """Issues and validates HMAC-SHA256-signed tokens.

    Construct via :func:`new_jwt`.
    """

    def __init__(self, secret: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self._secret = secret.encode("utf-8")
        self._ttl = ttl_seconds

    def _sign(self, signing_input: bytes) -> str:
        sig = hmac.new(self._secret, signing_input, hashlib.sha256).digest()
        return _b64url_encode(sig)

    def issue(self, user_id: str, username: str, role: str, version: int = 0) -> str:
        """Create a signed token for a user.

        The user's id, username and role are passed directly. ``sub`` is set to the user ID
        and the token expires after the configured TTL. ``version`` is the user's
        current session version.
        """
        now = int(time.time())
        header = {"alg": "HS256", "typ": "JWT"}
        payload = {
            "sub": user_id,
            "iat": now,
            "nbf": now,
            "exp": now + self._ttl,
            "username": username,
            "role": role,
            "jti": secrets.token_hex(16),
            "ver": int(version),
        }
        # Compact, key-sorted JSON keeps the encoding deterministic.
        h = _b64url_encode(
            json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        p = _b64url_encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        signing_input = f"{h}.{p}".encode("ascii")
        return f"{h}.{p}.{self._sign(signing_input)}"

    def verify(self, token: str) -> Claims:
        """Validate the signature and standard claims of a token.

        Returns the parsed :class:`Claims` on success. Raises :class:`JWTError`
        on any malformed token, unexpected signing method, bad signature, or an
        expired / not-yet-valid token.
        """
        if not isinstance(token, str):
            raise JWTError("auth: parse token: not a string")
        parts = token.split(".")
        if len(parts) != 3:
            raise JWTError("auth: parse token: malformed token")
        h_b64, p_b64, sig_b64 = parts

        try:
            header = json.loads(_b64url_decode(h_b64))
        except (ValueError, json.JSONDecodeError) as exc:
            raise JWTError(f"auth: parse token: bad header: {exc}") from exc

        # Reject anything that is not HS256 (e.g. alg "none" or an asymmetric
        # algorithm used for key-confusion attacks).
        if not isinstance(header, dict) or header.get("alg") != "HS256":
            raise JWTError(
                f"auth: unexpected signing method: {header.get('alg') if isinstance(header, dict) else None}"
            )

        signing_input = f"{h_b64}.{p_b64}".encode("ascii")
        expected_sig = self._sign(signing_input)
        # Constant-time comparison of the base64url signature strings.
        # hmac.compare_digest raises TypeError on the str path when a segment
        # holds non-ASCII (the Authorization header is latin-1 decoded, so a
        # malformed signature can); treat that as a plain mismatch → 401, not a
        # 500 escaping the middleware (which only catches JWTError).
        try:
            sig_ok = hmac.compare_digest(expected_sig, sig_b64)
        except TypeError:
            sig_ok = False
        if not sig_ok:
            raise JWTError("auth: invalid token: signature mismatch")

        try:
            payload = json.loads(_b64url_decode(p_b64))
        except (ValueError, json.JSONDecodeError) as exc:
            raise JWTError(f"auth: parse token: bad payload: {exc}") from exc
        if not isinstance(payload, dict):
            raise JWTError("auth: parse token: bad payload")

        now = int(time.time())
        exp = payload.get("exp")
        nbf = payload.get("nbf")
        if not isinstance(exp, (int, float)) or now >= int(exp):
            raise JWTError("auth: invalid token: expired")
        if isinstance(nbf, (int, float)) and now < int(nbf):
            raise JWTError("auth: invalid token: not yet valid")

        iat = payload.get("iat")
        ver = payload.get("ver")
        return Claims(
            sub=str(payload.get("sub", "")),
            username=str(payload.get("username", "")),
            role=str(payload.get("role", "")),
            iat=int(iat) if isinstance(iat, (int, float)) else 0,
            nbf=int(nbf) if isinstance(nbf, (int, float)) else 0,
            exp=int(exp),
            jti=str(payload.get("jti", "")),
            ver=int(ver) if isinstance(ver, int) else 0,
        )


def new_jwt(secret: str) -> JWT:
    """Return a :class:`JWT` using ``secret`` and the default 24h TTL."""
    return JWT(secret, DEFAULT_TTL_SECONDS)
