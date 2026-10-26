"""Authenticated symmetric encryption (AES-256-GCM) for the Kowan control plane's
stored credentials, plus helpers to load or create the master encryption key.

The store attaches a :class:`Cipher` via
``Store.use_cipher``; it seals secret and auth-provider blobs at rest. This module
has no dependency on the store so it may be imported by the store without a cycle.

Uses ``cryptography`` (AESGCM). The wire format is ``nonce || ciphertext+tag``
with a fresh 12-byte nonce per call (the 16-byte GCM tag is appended to the
ciphertext).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import os
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_log = logging.getLogger("kowan.secrets")

# KEY_LEN is the required AES-256 key length in bytes.
KEY_LEN = 32

# _NONCE_LEN is the GCM standard nonce size.
_NONCE_LEN = 12

# KEY_FILE_NAME is the file under the data directory holding the raw 32-byte key
# when no key is provided via the environment.
KEY_FILE_NAME = "secret.key"


class CipherError(Exception):
    """Raised on cipher construction or crypto failures (messages are
    prefixed ``secrets:``)."""


class Cipher:
    """Encrypts and decrypts secret payloads with AES-256-GCM.

    The same :class:`Cipher` value is safe for concurrent use (AESGCM holds an
    immutable key; a fresh nonce is generated per call).
    """

    def __init__(self, key: bytes) -> None:
        if len(key) != KEY_LEN:
            raise CipherError(f"secrets: key must be {KEY_LEN} bytes, got {len(key)}")
        try:
            self._aead = AESGCM(key)
        except Exception as e:  # noqa: BLE001
            raise CipherError(f"secrets: new aes gcm: {e}") from e

    def encrypt(self, plaintext: bytes) -> bytes:
        """Seal ``plaintext`` and return ``nonce || ciphertext``.

        A fresh random nonce is generated for every call.
        """
        nonce = os.urandom(_NONCE_LEN)
        # AESGCM.encrypt returns ciphertext||tag; prepend the nonce so the stored
        # layout is nonce||ciphertext||tag.
        return nonce + self._aead.encrypt(nonce, plaintext, None)

    def decrypt(self, blob: bytes) -> bytes:
        """Open a ``nonce || ciphertext`` blob produced by :meth:`encrypt`."""
        if len(blob) < _NONCE_LEN:
            raise CipherError("secrets: ciphertext too short")
        nonce, ciphertext = blob[:_NONCE_LEN], blob[_NONCE_LEN:]
        try:
            return self._aead.decrypt(nonce, ciphertext, None)
        except InvalidTag as e:
            raise CipherError("secrets: decrypt: message authentication failed") from e
        except Exception as e:  # noqa: BLE001
            raise CipherError(f"secrets: decrypt: {e}") from e


def load_or_create_key(data_dir: str, env_key: str) -> bytes:
    """Resolve the master 32-byte encryption key, in priority order.

    Resolution order:

      * If ``env_key`` is non-empty it is std-base64 decoded. If the decoded bytes
        are exactly 32 long they are used directly; otherwise the decoded bytes are
        SHA-256'd to 32 bytes. (A value that is not valid base64 is itself
        SHA-256'd so any passphrase works.)
      * Otherwise, if ``{data_dir}/secret.key`` exists, its raw 32 bytes are read.
      * Otherwise, 32 fresh random bytes are generated, written to
        ``{data_dir}/secret.key`` with 0600 permissions, and a warning is logged
        because losing this file makes all stored secrets unrecoverable.
    """
    if env_key:
        try:
            decoded = base64.standard_b64decode(_pad_b64(env_key))
            valid_b64 = True
        except (binascii.Error, ValueError):
            decoded = b""
            valid_b64 = False
        if valid_b64 and _reencodes(decoded, env_key):
            if len(decoded) == KEY_LEN:
                return decoded
            # Valid base64 but not 32 bytes: derive a 32-byte key from it.
            return hashlib.sha256(decoded).digest()
        # Not valid base64: treat the raw string as a passphrase.
        return hashlib.sha256(env_key.encode("utf-8")).digest()

    key_path = Path(data_dir) / KEY_FILE_NAME
    if key_path.exists():
        data = key_path.read_bytes()
        if len(data) != KEY_LEN:
            raise CipherError(
                f"secrets: key file {key_path} must contain {KEY_LEN} bytes, got {len(data)}"
            )
        return data

    # No env key and no key file: generate and persist a new one.
    key = os.urandom(KEY_LEN)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    # Write with 0600 so only the owner can read the master key.
    fd = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, key)
    finally:
        os.close(fd)
    _log.warning(
        "secrets: generated a new encryption key at %s; "
        "losing this file makes all stored secrets unrecoverable — "
        "back it up or set KOWAN_SECRET_KEY",
        key_path,
    )
    return key


def _pad_b64(s: str) -> str:
    """Right-pad ``s`` with '=' to a multiple of 4 so Python's strict base64
    decoder accepts unpadded input (we normalize length here)."""
    rem = len(s) % 4
    if rem:
        return s + "=" * (4 - rem)
    return s


def _reencodes(decoded: bytes, original: str) -> bool:
    """Report whether ``decoded`` round-trips back to ``original`` (modulo
    padding). Python's base64 decoder is lenient about trailing garbage in some
    modes; requiring a clean round-trip treats only genuine base64 as base64 and
    everything else as a passphrase."""
    try:
        reenc = base64.standard_b64encode(decoded).decode("ascii")
    except Exception:  # noqa: BLE001
        return False
    return reenc.rstrip("=") == original.rstrip("=")


__all__ = [
    "Cipher",
    "CipherError",
    "KEY_LEN",
    "KEY_FILE_NAME",
    "load_or_create_key",
]
