"""The transient SSO login state, sealed into an HttpOnly cookie.

``/auth/sso/{id}/start`` stores ``{provider, state, nonce, verifier, returnTo,
exp}`` AES-GCM-sealed with the server's secrets cipher (confidential and
tamper-proof) in a ``SameSite=Lax`` cookie scoped to ``/api/v1/auth/sso``. The
callback opens it, checks expiry and compares ``state`` in constant time, so
the code can only be redeemed by the browser that started the flow (no login
CSRF), and the PKCE verifier never leaves the server unencrypted.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, Optional

from .jose import b64url_decode, b64url_encode

STATE_COOKIE = "kowan_sso_state"
TICKET_COOKIE = "kowan_sso_ticket"
COOKIE_PATH = "/api/v1/auth/sso"
STATE_TTL = 600


def seal(cipher: Any, data: Dict[str, Any]) -> str:
    raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
    return b64url_encode(cipher.encrypt(raw))


def open_sealed(cipher: Any, value: str, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    if not value or len(value) > 4096:
        return None
    try:
        data = json.loads(cipher.decrypt(b64url_decode(value)))
    except Exception:  # noqa: BLE001 - tampered / foreign / old key
        return None
    if not isinstance(data, dict):
        return None
    if float(data.get("exp", 0)) < (time.time() if now is None else now):
        return None
    return data


def safe_return_to(value: Any) -> str:
    """Only same-origin absolute paths; anything else becomes ``/``."""
    v = str(value or "")
    if not v.startswith("/") or v.startswith("//") or v.startswith("/\\") or len(v) > 512:
        return "/"
    if any(c in v for c in ("\r", "\n", "\t")) or "://" in v.split("?", 1)[0]:
        return "/"
    if v.startswith("/login") or v.startswith("/api/"):
        return "/"
    return v
