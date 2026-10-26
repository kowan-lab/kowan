"""API keys.

This module combines two pieces:

  * the *secret* helpers:
    :func:`generate_api_key_secret` and :func:`hash_api_key_secret`. A secret is
    ``"pk_"`` followed by 32 random bytes in hex; the display prefix is its first
    11 characters (``"pk_" + 8`` hex digits). Only the SHA-256 hex of the secret
    is ever persisted.

  * the verifier adapter: :func:`new_api_key_verifier` wraps a plain function
    into an :class:`APIKeyVerifier`, returning ``None`` (i.e. "no API key
    support") when the function is ``None``.

The verify callback returns ``(role, username, ok)``. On a match the server maps
a stored key to ``(key.role, "key:" + key.name, True)``. The callback may be
synchronous or ``async``; :meth:`APIKeyVerifier.verify` awaits it when needed so
callers (the async middleware) can always ``await`` the result.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets as _secrets
from typing import Awaitable, Callable, Optional, Tuple, Union

# The human-recognizable scheme prefix for generated
# secrets. The full secret is this prefix + 32 random bytes in hex.
API_KEY_SECRET_PREFIX = "pk_"

# Number of leading characters retained for display,
# e.g. "pk_1a2b3c4d" — the 3-char scheme prefix plus 8 hex digits.
API_KEY_PREFIX_LEN = 11

# A verify function returns (role, username, ok). It may return the tuple
# directly (sync) or a coroutine yielding it (async).
_VerifyResult = Tuple[str, str, bool]
VerifyFunc = Callable[[str], Union[_VerifyResult, Awaitable[_VerifyResult]]]


def generate_api_key_secret() -> Tuple[str, str]:
    """Return a fresh ``(secret, prefix)`` pair.

    The secret is
    ``"pk_" + <32 random bytes as hex>``; the prefix is its first
    ``API_KEY_PREFIX_LEN`` characters. The caller stores
    :func:`hash_api_key_secret` of the secret and shows the full secret to the
    user exactly once.
    """
    secret = API_KEY_SECRET_PREFIX + _secrets.token_bytes(32).hex()
    return secret, secret[:API_KEY_PREFIX_LEN]


def hash_api_key_secret(secret: str) -> str:
    """Return the lowercase hex SHA-256 of a presented secret.

    This is the value stored in the
    ``api_keys`` table and looked up on verification.
    """
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


class APIKeyVerifier:
    """Verifies an admin-managed API key secret.

    Wraps a verify callback (see :func:`new_api_key_verifier`). On a match
    :meth:`verify` returns ``(role, username, True)``; ``ok`` is ``False`` for
    any unknown or empty secret.
    """

    def __init__(self, verify: VerifyFunc) -> None:
        self._verify = verify

    async def verify(self, secret: str) -> _VerifyResult:
        """Return ``(role, username, ok)`` for ``secret``.

        Awaits the wrapped callback if it is a coroutine function so the async
        middleware can always ``await`` this method regardless of whether the
        underlying store lookup is sync or async.
        """
        result = self._verify(secret)
        if asyncio.iscoroutine(result):
            result = await result
        role, username, ok = result
        return role, username, ok


def new_api_key_verifier(verify: Optional[VerifyFunc]) -> Optional[APIKeyVerifier]:
    """Wrap a verify function into an :class:`APIKeyVerifier`.

    Passing ``None`` returns ``None`` (which
    the middleware treats as "no API key support"). The function returns the
    matched key's role and synthetic username and ``ok=False`` for any unknown or
    empty secret.
    """
    if verify is None:
        return None
    return APIKeyVerifier(verify)
