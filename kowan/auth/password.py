"""Password hashing.

bcrypt is not in the dependency stack, so we use the stdlib
:func:`hashlib.scrypt`. The whole thing needed for verification (algorithm, cost parameters,
salt, and the derived key) is packed into a single self-describing string, so a
stored hash carries everything ``verify_password`` needs:

    scrypt$<N>$<r>$<p>$<salt-b64>$<hash-b64>

Using base64url-without-padding for the salt/hash keeps the string free of the
``$`` separator. The format is deliberately stable: never change the field
order or the ``scrypt`` tag, only ever append a *new* algorithm tag if the KDF
changes, so old hashes keep verifying.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

# scrypt cost parameters. N must be a power of two; these are a reasonable
# interactive-login default (2^15 iterations). r and p follow the common
# scrypt recommendations.
_SCRYPT_N = 1 << 15
_SCRYPT_R = 8
_SCRYPT_P = 1
# scrypt requires maxmem >= 128 * N * r * p (plus overhead). Give it headroom.
_SCRYPT_MAXMEM = 128 * _SCRYPT_N * _SCRYPT_R * _SCRYPT_P * 2
_SALT_LEN = 16
_KEY_LEN = 32
_TAG = "scrypt"


def _b64(raw: bytes) -> str:
    """base64url-encode without padding (no ``=``, so no ``$`` collisions)."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _derive(pw: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        pw.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        maxmem=128 * n * r * p * 2,
        dklen=_KEY_LEN,
    )


def hash_password(pw: str) -> str:
    """Hash a plaintext password, returning a stable self-describing string.

    Each call uses a fresh random salt, so the
    same password hashes to a different string every time.
    """
    salt = secrets.token_bytes(_SALT_LEN)
    key = _derive(pw, salt, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P)
    return f"{_TAG}${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(key)}"


def verify_password(hash_str: str, pw: str) -> bool:
    """Report whether ``pw`` matches the stored hash.

    Note the argument order is ``(hash, pw)``. Never raises: a malformed hash string simply
    returns ``False``. The final comparison is constant-time.
    """
    try:
        tag, n_s, r_s, p_s, salt_s, key_s = hash_str.split("$")
    except (ValueError, AttributeError):
        return False
    if tag != _TAG:
        return False
    try:
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt = _unb64(salt_s)
        expected = _unb64(key_s)
    except Exception:  # noqa: BLE001 - any decode failure => no match
        return False
    try:
        got = _derive(pw, salt, n, r, p)
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(got, expected)
