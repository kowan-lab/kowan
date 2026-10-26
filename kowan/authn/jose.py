"""Minimal JOSE: compact JWS parsing and signature verification.

Only what ID token validation needs. Asymmetric algorithms only: ``none`` and
the HMAC family are rejected outright (an attacker who can make us verify an
HS256 token with a public key as the HMAC secret would forge tokens).
"""

from __future__ import annotations

import base64
import json
from typing import Any, Dict, Tuple

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature


class JOSEError(Exception):
    """Malformed token / key, or a failed signature check."""


_HASHES = {"256": hashes.SHA256, "384": hashes.SHA384, "512": hashes.SHA512}
_EC_CURVES = {"P-256": (ec.SECP256R1, 32), "P-384": (ec.SECP384R1, 48), "P-521": (ec.SECP521R1, 66)}
_ALG_CURVE = {"ES256": "P-256", "ES384": "P-384", "ES512": "P-521"}

ALLOWED_ALGS = (
    "RS256", "RS384", "RS512",
    "PS256", "PS384", "PS512",
    "ES256", "ES384", "ES512",
    "EdDSA",
)


def b64url_decode(s: str) -> bytes:
    if not isinstance(s, str):
        raise JOSEError("expected base64url text")
    try:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    except (ValueError, TypeError) as err:
        raise JOSEError(f"bad base64url: {err}") from err


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64_int(s: Any) -> int:
    if not isinstance(s, str) or not s:
        raise JOSEError("missing key parameter")
    return int.from_bytes(b64url_decode(s), "big")


def parse_jws(token: str) -> Tuple[Dict[str, Any], Dict[str, Any], bytes, bytes]:
    """Split a compact JWS into ``(header, payload, signing_input, signature)``."""
    if not isinstance(token, str) or token.count(".") != 2:
        raise JOSEError("malformed token")
    h, p, sig = token.split(".")
    try:
        header = json.loads(b64url_decode(h))
        payload = json.loads(b64url_decode(p))
    except (ValueError, UnicodeDecodeError) as err:
        raise JOSEError(f"malformed token: {err}") from err
    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise JOSEError("malformed token")
    return header, payload, f"{h}.{p}".encode("ascii"), b64url_decode(sig)


def jwk_to_key(jwk: Dict[str, Any]) -> Any:
    """Build a public key object from a JWK (RSA, EC P-256/384/521, OKP Ed25519)."""
    kty = jwk.get("kty")
    if kty == "RSA":
        return rsa.RSAPublicNumbers(_b64_int(jwk.get("e")), _b64_int(jwk.get("n"))).public_key()
    if kty == "EC":
        crv = jwk.get("crv")
        if crv not in _EC_CURVES:
            raise JOSEError(f"unsupported curve {crv!r}")
        curve_cls, _ = _EC_CURVES[crv]
        return ec.EllipticCurvePublicNumbers(_b64_int(jwk.get("x")), _b64_int(jwk.get("y")), curve_cls()).public_key()
    if kty == "OKP" and jwk.get("crv") == "Ed25519":
        return ed25519.Ed25519PublicKey.from_public_bytes(b64url_decode(jwk.get("x") or ""))
    raise JOSEError(f"unsupported key type {kty!r}")


def key_fits_alg(jwk: Dict[str, Any], alg: str) -> bool:
    kty = jwk.get("kty")
    if alg.startswith(("RS", "PS")):
        return kty == "RSA"
    if alg.startswith("ES"):
        return kty == "EC" and jwk.get("crv") == _ALG_CURVE.get(alg)
    if alg == "EdDSA":
        return kty == "OKP"
    return False


def verify(alg: str, key: Any, signing_input: bytes, signature: bytes) -> bool:
    """Verify a JWS signature. Returns False on mismatch; raises on bad alg."""
    if alg not in ALLOWED_ALGS:
        raise JOSEError(f"algorithm {alg!r} not allowed")
    try:
        if alg.startswith("RS"):
            if not isinstance(key, rsa.RSAPublicKey):
                return False
            key.verify(signature, signing_input, padding.PKCS1v15(), _HASHES[alg[2:]]())
        elif alg.startswith("PS"):
            if not isinstance(key, rsa.RSAPublicKey):
                return False
            h = _HASHES[alg[2:]]()
            key.verify(signature, signing_input, padding.PSS(mgf=padding.MGF1(h), salt_length=h.digest_size), h)
        elif alg.startswith("ES"):
            if not isinstance(key, ec.EllipticCurvePublicKey):
                return False
            _, size = _EC_CURVES[_ALG_CURVE[alg]]
            if len(signature) != 2 * size:
                return False
            r = int.from_bytes(signature[:size], "big")
            s = int.from_bytes(signature[size:], "big")
            key.verify(encode_dss_signature(r, s), signing_input, ec.ECDSA(_HASHES[alg[2:]]()))
        elif alg == "EdDSA":
            if not isinstance(key, ed25519.Ed25519PublicKey):
                return False
            key.verify(signature, signing_input)
        else:  # pragma: no cover - guarded by ALLOWED_ALGS
            return False
    except InvalidSignature:
        return False
    return True
